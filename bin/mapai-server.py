#!/usr/bin/env python3
"""mapai-server — serves LifeOS on localhost and gives it a way to act on the vault.

Replaces `python3 -m http.server` in the launcher. Same job — serve the vault
directory over http://localhost so Ollama will talk to the page — plus a small
API for the things a browser fundamentally cannot do itself:

  GET  /api/vault/status      is it open, what icon is applied, where is the NAS
  POST /api/vault/password    change the vault password (hdiutil chpass + Keychain)
  POST /api/vault/icon        set the Desktop icon from an uploaded image
  POST /api/vault/icon/reset  restore the map.ca logo

Security, deliberately:
  · Binds 127.0.0.1 only. Never reachable from the LAN or the tailnet.
  · Changing the password REQUIRES the current one, and it is verified against
    the image by hdiutil, not against the Keychain copy — so a stale or wrong
    Keychain entry cannot be used to take the vault over.
  · The new password is written to the Keychain only AFTER hdiutil confirms the
    change. If that write fails you are told loudly, because at that point the
    vault has a password no machine knows.
  · Uploads are capped and sniffed for a real image header before touching disk.
"""
import base64
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HOME = os.path.expanduser("~")
VAULT_IMG = os.path.join(HOME, "Desktop", "MapAi.sparsebundle")
VAULT_MP = "/Volumes/MapAi"
BIN = os.path.join(HOME, "LifeOS", "bin")
BRAND_LOGO = os.path.join(HOME, "map-ca", "public", "images", "logo.png")
ICON_PNG = os.path.join(HOME, ".mapai", "vault-icon.png")
PORT = int(os.environ.get("MAPAI_WEB_PORT", "8765"))
ROOT = os.environ.get("MAPAI_WEB_ROOT", VAULT_MP)
MAX_UPLOAD = 8 * 1024 * 1024
# A 48 MP HEIC off a recent iPhone is routinely 12-25 MB, and base64 inflates
# it by a third. Photo tools get their own ceiling.
PHOTO_MAX = 64 * 1024 * 1024
TOOLS_DIR = os.path.join(HOME, "LifeOS", "tools")

# The only Host / Origin values that may reach this server. It binds
# 127.0.0.1, so a real request can only come from this machine — these
# lists exist to stop a *browser* on this machine being used as the
# attacker's proxy (see Handler._local_only).
ALLOWED_HOSTS = frozenset(
    "%s:%d" % (h, PORT) for h in ("localhost", "127.0.0.1", "[::1]")
)
ALLOWED_ORIGINS = frozenset(
    "http://%s:%d" % (h, PORT) for h in ("localhost", "127.0.0.1", "[::1]")
)

# Version of the map.ca read contract this build speaks (the resource shapes
# and tolerance rules in docs/lifeos/exec-plan/32-api-contract-v1.md). Pinned
# in profile/mapca.json so a breaking change on either side is a clear
# message rather than a mystery empty mirror.
MAPCA_CONTRACT = 1

# How many sync attempts the local log remembers.
SYNC_LOG_KEEP = 50

# A ceiling on the record core, so a runaway client cannot fill the disk.
# Far above any plausible personal archive: the seeded install has 14.
MAX_RECORDS = 50000


# ------------------------------------------------------- profile schema 2
#
# These caps mirror `src/lib/lifeos/schema.ts` in the map.ca repo, which is
# what actually reads this file when a member clicks Connect. Writing a card
# this side would accept but that side refuses is the failure mode worth
# preventing — it surfaces at the very end of the journey, as "not a LifeOS
# profile card" about the member's own profile. A cross-language test pins
# the two tables together.
PROFILE_SCHEMA = 2
PROFILE_CAP = {
    "name": 120, "handle": 39, "pronouns": 40, "city": 120, "bio": 500,
    "member_since": 40, "email": 254, "phone": 30,
    "business_name": 120, "business_line": 200,
    "tag": 40, "pillar": 60, "link_label": 60,
}
PROFILE_MAX_TAGS = 10
PROFILE_MAX_PILLARS = 10
PROFILE_MAX_LINKS = 10
# The reader rejects the WHOLE file above this budget before parsing
# (schema.ts LIFEOS_PROFILE_MAX_BYTES) — a card this writer accepts must
# always fit it, or the member's own card fails at Connect. Pinned to the
# reader by the cross-language test.
PROFILE_MAX_FILE_BYTES = 256 * 1024
# An avatar may be a small inline image (LifeOS resizes locally for members
# with nowhere to host one), so this field alone is allowed to be large —
# but its allowance must leave the rest of a maximal card inside the file
# budget above (also pinned by the cross-language test).
PROFILE_MAX_AVATAR = 192 * 1024


def _clean_str(value, cap):
    return value.strip()[:cap] if isinstance(value, str) else ""


def _clean_str_list(value, max_items, cap):
    if not isinstance(value, list):
        return []
    out = []
    for entry in value:
        text = _clean_str(entry, cap)
        if text:
            out.append(text)
        if len(out) == max_items:
            break
    return out


def _clean_links(value):
    if not isinstance(value, list):
        return []
    out = []
    for entry in value:
        if isinstance(entry, str):
            url, label = entry.strip(), ""
        elif isinstance(entry, dict):
            url = _clean_str(entry.get("url"), 2048)
            label = _clean_str(entry.get("label"), PROFILE_CAP["link_label"])
        else:
            continue
        if url:
            out.append({"label": label, "url": url[:2048]})
        if len(out) == PROFILE_MAX_LINKS:
            break
    return out


def sanitize_profile(raw):
    """(ok, cleaned, dropped_keys, error) for an incoming profile card.

    Field-by-field into a fresh dict — never a copy of the request — so no
    key the caller invented can survive into the file on disk.
    """
    if not isinstance(raw, dict):
        return (False, None, [], "not a profile object")
    schema = raw.get("profile_schema", PROFILE_SCHEMA)
    if not isinstance(schema, (int, float)) or isinstance(schema, bool):
        return (False, None, [], "profile_schema must be a number")
    if schema > PROFILE_SCHEMA:
        return (False, None, [],
                "profile_schema %s is newer than this LifeOS understands (%d)"
                % (schema, PROFILE_SCHEMA))

    contact = raw.get("contact") if isinstance(raw.get("contact"), dict) else {}
    business = raw.get("business") if isinstance(raw.get("business"), dict) else {}
    avatar = raw.get("avatar")
    cleaned = {
        "profile_schema": int(schema),
        "public": raw.get("public") is not False,
        "draft": raw.get("draft") is True,
        "name": _clean_str(raw.get("name"), PROFILE_CAP["name"]),
        "handle": _clean_str(raw.get("handle"), PROFILE_CAP["handle"]).lower(),
        "pronouns": _clean_str(raw.get("pronouns"), PROFILE_CAP["pronouns"]),
        "city": _clean_str(raw.get("city"), PROFILE_CAP["city"]),
        "bio": _clean_str(raw.get("bio"), PROFILE_CAP["bio"]),
        "pillars": _clean_str_list(raw.get("pillars"), PROFILE_MAX_PILLARS,
                                   PROFILE_CAP["pillar"]),
        "tags": _clean_str_list(raw.get("tags"), PROFILE_MAX_TAGS,
                                PROFILE_CAP["tag"]),
        "avatar": avatar[:PROFILE_MAX_AVATAR] if isinstance(avatar, str) else "",
        "intro_video": _clean_str(raw.get("intro_video"), 2048),
        "member_since": _clean_str(raw.get("member_since"),
                                   PROFILE_CAP["member_since"]),
        "links": _clean_links(raw.get("links")),
        "contact": {
            "email": _clean_str(contact.get("email"), PROFILE_CAP["email"]),
            "phone": _clean_str(contact.get("phone"), PROFILE_CAP["phone"]),
        },
        "business": {
            "name": _clean_str(business.get("name"),
                               PROFILE_CAP["business_name"]),
            "line": _clean_str(business.get("line"),
                               PROFILE_CAP["business_line"]),
        },
        "tabs": _clean_str_list(raw.get("tabs"), PROFILE_MAX_TAGS,
                                PROFILE_CAP["tag"]),
    }
    dropped = sorted(k for k in raw if k not in cleaned and k != "ok")
    return (True, cleaned, dropped, None)


def utc_now():
    """Timestamp with an explicit offset.

    The old format was naive local time, which is ambiguous the moment a
    member travels or a reader is in another zone — and unsortable across
    both.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )

# The native photo tools, and the ONLY flags each may be given. The client says
# "mapcolor, exposure 0.4"; this table decides that means `--exposure 0.4` and
# that 40 would be clamped. Values are validated by type and range here, so no
# request can invent a flag or smuggle a path.
PHOTO_TOOLS = {
    "mapshot": {
        "dir": "mapshot",
        "args": {
            "preset":  {"type": "choice", "flag": "--preset",
                        "choices": ("thumb", "card", "full")},
            "keepGps": {"type": "bool", "flag": "--keep-gps"},
        },
        "outExt": lambda o: ".jpg",
        "suffix": lambda o: "--map-%s" % (o.get("preset") or "card"),
    },
    "mapcolor": {
        "dir": "mapcolor",
        "args": {
            "auto":       {"type": "bool",   "flag": "--auto"},
            "temp":       {"type": "number", "flag": "--temp",       "range": (-100, 100)},
            "tint":       {"type": "number", "flag": "--tint",       "range": (-100, 100)},
            "exposure":   {"type": "number", "flag": "--exposure",   "range": (-4, 4)},
            "contrast":   {"type": "number", "flag": "--contrast",   "range": (0.25, 3)},
            "saturation": {"type": "number", "flag": "--saturation", "range": (0, 3)},
            "vibrance":   {"type": "number", "flag": "--vibrance",   "range": (-1, 1)},
            "shadows":    {"type": "number", "flag": "--shadows",    "range": (0, 1)},
            "highlights": {"type": "number", "flag": "--highlights", "range": (0, 1)},
        },
        "outExt": lambda o: ".jpg",
        "suffix": lambda o: "--graded",
    },
    "maphdr": {
        "dir": "maphdr",
        # The only tool that takes a SET of frames rather than one photo.
        "multi": True,
        "args": {
            "strength":   {"type": "number", "flag": "--strength",   "range": (0, 1)},
            "shadows":    {"type": "number", "flag": "--shadows",    "range": (0, 1)},
            "highlights": {"type": "number", "flag": "--highlights", "range": (0, 1)},
            "smooth":     {"type": "number", "flag": "--smooth",     "range": (0, 200)},
            "max":        {"type": "number", "flag": "--max",        "range": (0, 6000)},
            "noAlign":    {"type": "bool",   "flag": "--no-align"},
        },
        "outExt": lambda o: ".jpg",
        "suffix": lambda o: "--hdr",
    },
    "mapmatte": {
        "dir": "mapmatte",
        "args": {
            "all":        {"type": "bool",   "flag": "--all"},
            "background": {"type": "color",  "flag": "--background"},
            "feather":    {"type": "number", "flag": "--feather", "range": (0, 40)},
            "edge":       {"type": "number", "flag": "--edge",    "range": (-20, 20)},
            "max":        {"type": "number", "flag": "--max",     "range": (0, 6000)},
        },
        # Transparency needs PNG; anything flattened may as well stay JPEG.
        "outExt": lambda o: ".png" if (o.get("background") or "transparent") == "transparent" else ".jpg",
        "suffix": lambda o: "--cutout",
    },
}

# PNG, JPEG, GIF, WEBP, ICNS. Anything else is not an image we will hand to sips.
MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"icns")


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def keychain_get(name):
    r = sh(["security", "find-generic-password", "-a", "mapsec",
            "-s", "mapsec." + name, "-w"])
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def keychain_set(name, value):
    return sh(["security", "add-generic-password", "-U", "-a", "mapsec",
               "-s", "mapsec." + name, "-D", "map.ca secret",
               "-l", "mapsec." + name, "-w", value]).returncode == 0


# ------------------------------------------------------------------ the fleet
#
# Why any of this is server-side at all: the Fleet view used to ping each
# machine with fetch() straight from the page, which can only ever see what a
# browser can see. That rules out most of the fleet —
#
#   · the DGX publishes every inference container on 127.0.0.1 (docker
#     `127.0.0.1:4000->4000`), so over the tailnet it answers on port 22 and
#     nothing else. No fetch() will ever reach it.
#   · the NAS and the DSM UI send no CORS headers, so a cross-origin read is
#     rejected even though the host is plainly up.
#   · an iPhone runs no server at all, and never will.
#
# A socket does not care about any of that. Tailscale already knows which
# machines are up — it is a mesh with a coordination server — so presence comes
# from `tailscale status --json` and the per-service checks layer on top.
TAILSCALE_BINS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "tailscale",
)
PROBE_TIMEOUT = 4.0
MAX_PROBE_NODES = 32


def tailscale_bin():
    for path in TAILSCALE_BINS:
        if os.path.sep in path:
            if os.path.exists(path):
                return path
        elif sh(["which", path]).returncode == 0:
            return path
    return None


def tailnet_status():
    """Every device on the tailnet, as Tailscale itself sees it.

    This is the authoritative presence signal — it comes from the coordination
    server plus live WireGuard state, so it is right even for a machine that
    listens on nothing (the iPhone) or only on ssh (the DGX).
    """
    exe = tailscale_bin()
    if not exe:
        return {"ok": False, "error": "tailscale CLI not found", "devices": []}
    try:
        r = sh([exe, "status", "--json"], timeout=10)
    except Exception as exc:                       # a wedged CLI must not 500
        return {"ok": False, "error": "tailscale: %s" % exc, "devices": []}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "tailscale status failed").strip(),
                "devices": []}
    try:
        data = json.loads(r.stdout)
    except ValueError as exc:
        return {"ok": False, "error": "unreadable status: %s" % exc, "devices": []}

    def dev(peer, is_self=False):
        name = (peer.get("DNSName") or "").split(".")[0] or peer.get("HostName") or "?"
        return {
            "name": name,
            "dns": (peer.get("DNSName") or "").rstrip("."),
            "host": peer.get("HostName") or "",
            "os": peer.get("OS") or "",
            "ips": peer.get("TailscaleIPs") or [],
            # Self is always "online" — Tailscale reports Online=false for the
            # node doing the asking, which would otherwise show this Mac as down.
            "online": True if is_self else bool(peer.get("Online")),
            "lastSeen": peer.get("LastSeen") or "",
            "self": is_self,
        }

    devices = []
    me = data.get("Self")
    if me:
        devices.append(dev(me, True))
    for peer in (data.get("Peer") or {}).values():
        devices.append(dev(peer))
    devices.sort(key=lambda d: (not d["self"], not d["online"], d["name"]))
    return {"ok": True, "backend": data.get("BackendState") or "",
            "tailnet": (data.get("CurrentTailnet") or {}).get("Name") or "",
            "devices": devices}


def tcp_open(host, port, timeout=PROBE_TIMEOUT):
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def http_json(url, headers=None, timeout=PROBE_TIMEOUT):
    """GET a URL and parse JSON. Returns (status, obj_or_None, error_or_None)."""
    import urllib.error
    import urllib.request
    if not re.match(r"^https?://", url or ""):
        return None, None, "only http(s) URLs are probed"
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(256 * 1024)
            try:
                return resp.status, json.loads(raw), None
            except ValueError:
                return resp.status, None, None
    except urllib.error.HTTPError as exc:
        return exc.code, None, None
    except Exception as exc:                       # DNS, refused, TLS, timeout
        return None, None, type(exc).__name__


def probe_node(node, mesh=None):
    """Check one node. Returns a status dict — never the token used to get it.

    `mesh` maps tailnet IP -> device, so a node that serves nothing still gets
    a truthful up/down from Tailscale instead of an unhelpful "no URL".
    """
    import time
    kind = (node.get("kind") or "generic").strip()
    url = (node.get("url") or "").strip().rstrip("/")
    ts = (node.get("ts") or "").strip()
    lan = (node.get("lan") or "").strip()
    ports = [p for p in (node.get("ports") or []) if str(p).isdigit()][:8]
    out = {"up": None, "note": "", "models": None, "ports": {},
           "ms": None, "mesh": None}
    started = time.time()

    seen = (mesh or {}).get(ts) if ts else None
    if seen:
        out["mesh"] = bool(seen.get("online"))

    # TCP reachability on the tailnet — this is what proves an ssh-only box
    # like the DGX is actually powered on and on the mesh. Fall back to the LAN
    # address when there is no tailnet name or the mesh route is not answering:
    # the security cameras never join the tailnet and are only ever reachable
    # at 192.168.x.x, so without this they could not be probed at all.
    for port in ports:
        ok = tcp_open(ts, port) if ts else False
        if not ok and lan:
            ok = tcp_open(lan, port)
        out["ports"][str(port)] = ok

    if kind == "host":
        out.update(up=True, note="this device")
    elif not url:
        live = [p for p, ok in out["ports"].items() if ok]
        if live:
            out.update(up=True, note="port %s open" % ", ".join(live))
        elif out["ports"]:
            out.update(up=False, note="no open port")
        elif out["mesh"] is not None:
            # Nothing to call, but Tailscale still knows. This is the iPhone.
            out.update(up=out["mesh"],
                       note="on the tailnet" if out["mesh"] else "off the tailnet")
        else:
            out.update(up=None, note="no URL — add one")
    else:
        headers, path = {}, url
        if kind == "dgx":
            token = node.get("token") or keychain_get("DGX_API_TOKEN") or ""
            if token:
                headers["Authorization"] = "Bearer " + token
            path = url + "/models"
        elif kind == "vllm":
            path = url + "/v1/models"
        elif kind == "ollama":
            path = url + "/api/tags"
        elif kind == "comms":
            if node.get("token"):
                headers["X-M-Token"] = node["token"]
            path = url + "/health"

        status, body, err = http_json(path, headers)
        if status is None:
            out.update(up=False, note=err or "unreachable")
        elif status == 401 or status == 403:
            # The host answered — it is up, it just would not tell us more.
            out.update(up=True, note="up · needs token")
        elif 200 <= status < 400:
            out.update(up=True, note="reachable")
            if isinstance(body, dict):
                if isinstance(body.get("data"), list):
                    out["models"] = [m.get("id") for m in body["data"]
                                     if isinstance(m, dict) and m.get("id")]
                elif isinstance(body.get("models"), list):
                    out["models"] = [m.get("name") for m in body["models"]
                                     if isinstance(m, dict) and m.get("name")]
                elif isinstance(body.get("peers"), list):
                    out["note"] = "%d peer(s) connected" % len(body["peers"])
            if out["models"] is not None:
                out["note"] = ""
        else:
            # An HTTP status — any status — means something answered, so the
            # machine is on. 404 on a generic node just means "/" isn't a page.
            out.update(up=True, note="HTTP %d" % status)

    # A box can be on the mesh while the service it serves is down. Say so
    # rather than reporting one of the two facts and hiding the other.
    if out["up"] is False:
        live = [p for p, ok in out["ports"].items() if ok]
        if live:
            out["note"] = (out["note"] or "service down") + " · host up"
        elif out["mesh"]:
            out["note"] = (out["note"] or "service down") + " · on the tailnet"

    out["ms"] = int((time.time() - started) * 1000)
    return out


def probe_fleet(nodes):
    from concurrent.futures import ThreadPoolExecutor
    nodes = [n for n in nodes if isinstance(n, dict)][:MAX_PROBE_NODES]
    if not nodes:
        return {}
    mesh = {}
    if any((n.get("ts") or "").strip() for n in nodes):
        for d in tailnet_status().get("devices") or []:
            for ip in d.get("ips") or []:
                mesh[ip] = d
    with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as pool:
        results = list(pool.map(lambda n: probe_node(n, mesh), nodes))
    return {n.get("id") or str(i): r for i, (n, r) in enumerate(zip(nodes, results))}


# ------------------------------------------------------------------- rag
# rag.py is a CLI, but importing it beats shelling out per request: the module
# is loaded once and the sqlite handle work stays in-process.
_RAG = []


def load_rag():
    if _RAG:
        return _RAG[0]
    path = os.path.join(BIN, "rag.py")
    if not os.path.exists(path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("rag", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _RAG.append(mod)
        return mod
    except Exception:
        return None


def rag_status():
    rag = load_rag()
    if not rag:
        return {"ok": False, "error": "rag.py not available"}
    if not os.path.exists(rag.DB):
        return {"ok": True, "indexed": False, "chunks": 0,
                "hint": "run: python3 ~/LifeOS/bin/rag.py index"}
    try:
        cx = rag.connect()
        n = cx.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        f = cx.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
        by = [{"source": s, "chunks": c} for s, c in cx.execute(
            "SELECT source, COUNT(*) FROM chunks GROUP BY source ORDER BY 2 DESC")]
        return {"ok": True, "indexed": n > 0, "chunks": n, "files": f,
                "model": rag.meta_get(cx, "model"), "built": rag.meta_get(cx, "built"),
                "sources": by}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("%s %s\n" % (self.command, self.path))

    # ---------------------------------------------------------- local guard
    def _local_only(self):
        """Refuse anything that is not this machine talking to itself.

        Binding 127.0.0.1 keeps the LAN out, but it does NOT keep a web page
        out: any site the owner has open can POST here from their browser.
        A cross-origin POST of `Content-Type: text/plain` carrying JSON is a
        "simple request" — no preflight, so the browser sends it and only the
        RESPONSE is withheld from the attacker. That is enough to overwrite
        profile.json or spend the photo tools. And a page on a domain whose
        DNS answers 127.0.0.1 (rebinding) reaches us with its own Host.

        Two checks close both doors:
          · Host must name this loopback server  -> rebinding fails
          · Origin, when present, must be this server -> cross-site fails
        Then requiring application/json on POST turns every remaining
        cross-origin attempt into a preflight, which the Origin rule rejects
        before the request body is ever delivered.

        Requests with no Origin at all (curl, the launcher, cron) are local
        by construction and still allowed — the browser is the threat here.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() not in ALLOWED_ORIGINS:
            return False
        return True

    # NOTE: there is deliberately NO do_OPTIONS here. A CORS preflight for a
    # cross-origin JSON POST therefore hits SimpleHTTPRequestHandler, which
    # answers 501 and — the part that matters — emits no
    # Access-Control-Allow-* headers, so the browser blocks the real request
    # before it is sent. The absent method is load-bearing: implementing
    # OPTIONS "for completeness" would re-open what _local_only closes,
    # unless it repeats the same origin check.
    #
    # The GET side is guarded by Host/Origin above, and additionally by what
    # this server returns: JSON, never JSONP or anything that parses as
    # standalone JavaScript. A future endpoint that served callback-wrapped
    # or executable responses would become readable cross-origin via
    # <script src> regardless of these guards.

    def _deny_foreign(self):
        """403 + True when the request is not local; False when it may proceed."""
        if self._local_only():
            return False
        self._json({"ok": False, "error": "forbidden: this API is local-only"}, 403)
        return True

    # -------------------------------------------------------------- helpers
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit=None):
        n = int(self.headers.get("Content-Length") or 0)
        if n > (limit or MAX_UPLOAD):
            return None
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # -------------------------------------------------------------- routing
    def do_HEAD(self):
        if self._deny_foreign():
            return
        return super().do_HEAD()

    def do_GET(self):
        if self._deny_foreign():
            return
        if self.path.startswith("/api/vault/status"):
            return self._json(self.status())
        if self.path.startswith("/api/prompts"):
            return self._json(self.prompts())
        if self.path.startswith("/api/docs"):
            return self._json(self.vault_docs())
        if self.path.startswith("/api/md"):
            return self.md_view()
        if self.path.startswith("/api/reviews"):
            return self._json(self.reviews())
        if self.path.startswith("/api/profile"):
            return self._json(self.profile_get())
        if self.path.startswith("/api/records"):
            return self._json(self.records_get())
        if self.path.startswith("/api/pins"):
            return self._json(self.pins_get())
        if self.path.startswith("/api/fleet/tailnet"):
            return self._json(tailnet_status())
        if self.path.startswith("/api/rag/search"):
            return self._json(self.rag_search())
        if self.path.startswith("/api/rag/status"):
            return self._json(rag_status())
        if self.path.startswith("/api/mail/"):
            return self.mail_get()
        return super().do_GET()

    # -------------------------------------------------------------- mail
    def mail_get(self):
        from urllib.parse import parse_qs, urlparse
        import mailcore
        q = parse_qs(urlparse(self.path).query)
        one = lambda k, d=None: (q.get(k) or [d])[0]
        route = urlparse(self.path).path
        if route == "/api/mail/status":
            return self._json(mailcore.status())
        if route == "/api/mail/list":
            return self._json(mailcore.list_messages(
                lane=one("lane"), q=one("q"),
                limit=int(one("limit", "50")), unseen=one("unseen") == "1"))
        if route == "/api/mail/msg":
            m = mailcore.get_message(one("id", "0"))
            return self._json(m if m else {"ok": False, "error": "not found"},
                              200 if m else 404)
        if route == "/api/mail/drafts":
            return self._json(mailcore.list_drafts())
        return self._json({"ok": False, "error": "no such endpoint"}, 404)

    def mail_sync(self, data):
        import mailcore
        return self._json(mailcore.sync())

    def mail_send(self, data):
        import mailcore
        return self._json(mailcore.send(data.get("to", ""), data.get("subject", ""),
                                        data.get("body", ""), data.get("from") or None,
                                        data.get("cc") or None))

    def mail_agent(self, data):
        import mailcore
        task = (data.get("task") or "").strip()
        if not task:
            return self._json({"ok": False, "error": "no task"})
        return self._json(mailcore.run_agent(task,
                                             allow_send=bool(data.get("allow_send"))))

    def do_POST(self):
        if self._deny_foreign():
            return
        # Every POST here carries a JSON body. Demanding the matching
        # Content-Type is not pedantry: `text/plain` is what a cross-origin
        # "simple request" must use to skip the preflight, so refusing it
        # forces any foreign caller into a preflight that _local_only then
        # rejects. Belt to the Origin check's braces.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(
                {"ok": False, "error": "expected Content-Type: application/json"}, 415
            )
        routes = {
            "/api/vault/password": self.set_password,
            "/api/vault/icon": self.set_icon,
            "/api/vault/icon/reset": self.reset_icon,
            "/api/doc/extract": self.doc_extract,
            "/api/profile": self.profile_save,
            "/api/records": self.records_save,
            "/api/pins": self.pins_add,
            "/api/pins/sync": self.pins_sync,
            "/api/reviews/feedback": self.reviews_feedback,
            "/api/feedback": self.feedback_submit,
            "/api/fleet/probe": self.fleet_probe,
            "/api/photo/run": self.photo_run,
            "/api/photo/mapshot": self.photo_run,   # original name, kept working
            "/api/mail/sync": self.mail_sync,
            "/api/mail/send": self.mail_send,
            "/api/mail/agent": self.mail_agent,
        }
        route = self.path.split("?")[0]
        fn = routes.get(route)
        if not fn:
            return self._json({"ok": False, "error": "no such endpoint"}, 404)
        # Photos arrive at full original size on purpose — the whole point of
        # mapshot is measuring what the ORIGINAL cost, so the page must not
        # pre-shrink it and the 8 MB body cap would reject most phone photos.
        data = self._body(PHOTO_MAX if route.startswith("/api/photo/") else None)
        if data is None:
            return self._json({"ok": False, "error": "payload too large"}, 413)
        try:
            return fn(data)
        except Exception as exc:                      # never 500 silently
            return self._json({"ok": False, "error": str(exc)}, 500)

    # -------------------------------------------------------------- actions
    # ---------------------------------------------------------- photo tools
    def photo_run(self, data):
        """Run one of the native photo tools on an uploaded image.

        The browser cannot do this itself: it has no access to EXIF beyond what
        it re-encodes away, and a canvas round-trip destroys exactly the
        metadata (capture time, compass heading) that makes a map photo useful.
        So the original bytes come here untouched and a native tool does it.

        Arguments are built from PHOTO_TOOLS, never from the request: the client
        names a tool and supplies values, and this side decides which flags may
        exist and how each value is spelled. A request cannot introduce a flag.
        """
        spec = PHOTO_TOOLS.get(data.get("tool") or "mapshot")
        if not spec:
            return self._json({"ok": False, "error": "unknown tool"}, 400)
        tool = os.path.join(TOOLS_DIR, spec["dir"], spec["dir"])
        if not os.path.exists(tool):
            return self._json({"ok": False, "error": "%s not built — cd %s && "
                               "swiftc -O -o %s %s.swift"
                               % (spec["dir"], spec["dir"], spec["dir"], spec["dir"])}, 500)
        blobs = data.get("images") if spec.get("multi") else [data.get("data")]
        blobs = [b for b in (blobs or []) if b]
        if not blobs:
            return self._json({"ok": False, "error": "no image supplied"}, 400)
        if spec.get("multi") and len(blobs) < 2:
            return self._json({"ok": False, "error":
                               "give at least two bracketed frames of the same scene"}, 400)
        raws = []
        for b in blobs[:8]:
            if "," in b:
                b = b.split(",", 1)[1]
            try:
                raws.append(base64.b64decode(b))
            except Exception:
                return self._json({"ok": False, "error": "unreadable image data"}, 400)

        name = os.path.basename(data.get("name") or "photo")
        suffix = os.path.splitext(name)[1] or ".jpg"
        opts = data.get("args") or {}

        tmp = tempfile.mkdtemp(prefix="phototool-")
        try:
            srcs = []
            for i, raw in enumerate(raws):
                p = os.path.join(tmp, "in%d%s" % (i, suffix))
                with open(p, "wb") as fh:
                    fh.write(raw)
                srcs.append(p)
            ext = spec["outExt"](opts)
            out = os.path.join(tmp, "out" + ext)
            cmd = [tool] + srcs + ["--out", out, "--json"]
            for key, rule in spec["args"].items():
                v = opts.get(key)
                if v in (None, "", False):
                    continue
                if rule["type"] == "bool":
                    cmd.append(rule["flag"])
                elif rule["type"] == "choice":
                    if str(v) in rule["choices"]:
                        cmd += [rule["flag"], str(v)]
                elif rule["type"] == "number":
                    try:
                        f = float(v)
                    except (TypeError, ValueError):
                        continue
                    lo, hi = rule["range"]
                    cmd += [rule["flag"], str(max(lo, min(hi, f)))]
                elif rule["type"] == "color":
                    s = str(v).strip().lower()
                    if s in ("transparent", "white", "black") or re.match(r"^#[0-9a-f]{6}$", s):
                        cmd += [rule["flag"], s]
            r = sh(cmd, timeout=180)
            if r.returncode != 0:
                return self._json({"ok": False,
                                   "error": (r.stderr or "tool failed").strip()}, 500)
            report = json.loads(r.stdout)
            with open(out, "rb") as fh:
                packed = fh.read()
            stem = os.path.splitext(name)[0]
            report["input"] = name          # never echo the server's temp path
            report["output"] = stem + spec["suffix"](opts) + ext
            mime = "image/png" if ext == ".png" else "image/jpeg"
            return self._json({
                "ok": True, "tool": spec["dir"], "report": report,
                "image": "data:%s;base64," % mime + base64.b64encode(packed).decode(),
            })
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, 500)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def rag_search(self):
        """Semantic search over the spine, for grounding the dashboard's Chat.

        Returns passages only — the caller decides what to do with them. Keeps
        the retrieval in one place (rag.py) rather than reimplementing cosine
        in JavaScript against an index the browser cannot hold anyway.
        """
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        query = (q.get("q") or [""])[0].strip()
        if not query:
            return {"ok": False, "error": "missing q"}
        try:
            k = max(1, min(12, int((q.get("k") or ["6"])[0])))
        except ValueError:
            k = 6
        rag = load_rag()
        if not rag:
            return {"ok": False, "error": "rag.py not available"}
        try:
            cx = rag.connect()
            hits = rag.search(cx, query, k)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "query": query, "hits": [
            {"score": round(h[0], 4), "rel": h[1], "source": h[2],
             "ord": h[4], "text": h[5]} for h in hits]}

    def fleet_probe(self, data):
        """Check every node in the posted roster, concurrently, from here.

        The roster lives in the browser (localStorage) so it travels with the
        page; the checking happens here because only this side has sockets.
        """
        return self._json({"ok": True, "status": probe_fleet(data.get("nodes") or [])})

    # A library whose deliverable is an HTML page in the vault carries its
    # doc_url here — the dashboard attaches it to the Project record so a
    # click fills the body window with the document (paths are vault-root
    # relative: served at / on :8765 and /lifeos behind :3001).
    LIB_DOCS = {
        "maplaunch-library": {"url": "/map-model-explainer.html",
                              "title": "The Map — AI, delivered like power"},
        "aiharness-library": {"url": "/aiharness/index.html",
                              "title": "aiharness.ca — landing"},
    }

    def prompts(self):
        """Every prompt library (~/LifeOS/vault/projects/*-library) as JSON.

        Read-only: the markdown cards on disk are the source of truth; the
        dashboard mirrors them into CORE so builds are visible as records.
        """
        import glob
        base = os.path.join(HOME, "LifeOS", "vault", "projects")
        libs, cards = [], []
        try:
            names = sorted(d for d in os.listdir(base)
                           if d.endswith("-library")
                           and os.path.isdir(os.path.join(base, d)))
        except OSError:
            names = []
        from urllib.parse import quote
        md_url = lambda p: "/api/md?f=" + quote(p)
        for name in names:
            doc = self.LIB_DOCS.get(name, {})
            # every library opens as a document: its HTML deliverable when one
            # exists, its rendered README otherwise — a click never dead-ends
            # in the editor drawer.
            url, title = doc.get("url", ""), doc.get("title", "")
            if not url:
                readme = os.path.join(base, name, "README.md")
                if os.path.exists(readme):
                    url, title = md_url(readme), name
            libs.append({"name": name, "doc_url": url, "doc_title": title})
            for path in sorted(glob.glob(os.path.join(base, name, "[0-9][0-9]-*.md"))):
                try:
                    with open(path) as f:
                        text = f.read()
                except OSError:
                    continue
                m = re.match(r"---\n(.*?)\n---\n", text, re.S)
                meta = {}
                if m:
                    for line in m.group(1).splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip().strip('"')
                cards.append({"id": meta.get("id", ""), "slug": meta.get("slug", ""),
                              "title": meta.get("title", ""), "area": meta.get("area", ""),
                              "priority": meta.get("priority", ""),
                              "status": meta.get("status", ""), "path": path,
                              "lib": name, "doc": md_url(path)})
        return {"ok": True, "count": len(cards), "libs": libs, "cards": cards}

    # ---- My Map: reviews · profile · pins (offline-first, live sync hook)
    SPINE = os.path.join(HOME, "LifeOS")

    def _read_json(self, path, default):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _write_json(self, path, obj, keep_backup=False):
        """Write JSON atomically, optionally keeping one previous generation.

        `keep_backup` is for the files a bad write would actually cost you —
        the pins mirror, where a truncated file means re-syncing and losing
        every local-only pin. One generation, not a chain: the point is to
        survive the write, not to be a version-control system. Backups live
        beside the file so a member can see and restore them without tooling.
        """
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if keep_backup and os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except OSError:
                # A failed backup must not block the write itself; the
                # atomic replace below is still safe on its own.
                pass
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)

    def reviews(self):
        """the owner-action items harvested from the vault's HTML deliverables."""
        j = self._read_json(os.path.join(self.SPINE, "vault", "reviews", "reviews.json"),
                            {"items": []})
        j["ok"] = True
        return j

    def reviews_feedback(self, d):
        """The doc-review sidebar writes status/priority/feedback back to the
        feed on disk — reviews.json stays the source of truth (disk wins)."""
        import time
        rid = str(d.get("id", "")).strip()
        path = os.path.join(self.SPINE, "vault", "reviews", "reviews.json")
        j = self._read_json(path, None)
        if not isinstance(j, dict) or not isinstance(j.get("items"), list):
            return self._json({"ok": False, "error": "reviews.json unreadable"}, 500)
        it = next((x for x in j["items"] if x.get("id") == rid), None)
        if it is None:
            return self._json({"ok": False, "error": "no item '%s'" % rid}, 404)
        if str(d.get("status", "")) in ("open", "done"):
            it["status"] = str(d["status"])
        if re.match(r"^P[0-5]$", str(d.get("priority", ""))):
            it["priority"] = str(d["priority"])
        if "feedback" in d:
            it["feedback"] = str(d["feedback"])[:4000]
        it["feedback_date"] = time.strftime("%Y-%m-%d")
        self._write_json(path, j)
        return self._json({"ok": True})

    def md_view(self):
        """Render a markdown file (library card, README, doctrine) as a styled
        HTML page so it can fill the body window like any deliverable.
        ?f= must resolve inside ~/LifeOS or the vault — nothing else serves."""
        import html as H
        from urllib.parse import urlparse, parse_qs, unquote
        q = parse_qs(urlparse(self.path).query)
        f = unquote((q.get("f") or [""])[0])
        real = os.path.realpath(f)
        roots = (os.path.realpath(os.path.join(HOME, "LifeOS")),
                 os.path.realpath(VAULT_MP))
        if not f.endswith(".md") or not any(
                real == r or real.startswith(r + os.sep) for r in roots):
            return self._html_resp("<h1>Not available</h1>", 404)
        try:
            with open(real) as fh:
                text = fh.read()
        except OSError:
            return self._html_resp("<h1>Not found</h1>", 404)
        body = self._md2html(text)
        m = re.search(r"<h1>(.*?)</h1>", body)
        title = re.sub(r"<[^>]+>", "", m.group(1)) if m else os.path.basename(real)
        page = ("<!doctype html><html lang='en-CA'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>%s</title><style>"
                ":root{--paper:#fbfaf7;--ink:#0e1116;--ink2:#4a4a44;--line:#e5e1d8;"
                "--gold:#e8b64c;--gold-dk:#a97c1b}"
                "*{box-sizing:border-box}body{margin:0;background:var(--paper);"
                "color:var(--ink);font:16px/1.6 Charter,Georgia,serif}"
                "main{max-width:680px;margin:0 auto;padding:2.5rem 1.25rem 5rem}"
                "h1{font-size:1.9rem;line-height:1.2;border-bottom:3px double var(--line);"
                "padding-bottom:.7rem}h2{font-size:1.25rem;margin-top:2rem;"
                "color:var(--ink)}h2::before{content:'— ';color:var(--gold-dk)}"
                "h3{font-size:1.05rem}code{font:.85em ui-monospace,Menlo,monospace;"
                "background:#f2efe7;padding:.1em .35em;border-radius:3px}"
                "pre{background:#f2efe7;border:1px solid var(--line);padding:.9rem 1rem;"
                "overflow-x:auto;font:.82rem/1.5 ui-monospace,Menlo,monospace}"
                "pre code{background:none;padding:0}pre.fm{border-left:4px solid var(--gold);"
                "color:var(--ink2)}table{border-collapse:collapse;width:100%%;margin:1rem 0;"
                "font-size:.92em}th,td{border:1px solid var(--line);padding:.4rem .6rem;"
                "text-align:left}th{background:#f2efe7}ul{padding-left:1.3rem}"
                "li{margin:.3rem 0}a{color:var(--gold-dk)}b{font-weight:700}"
                "blockquote{border-left:4px solid var(--gold);margin:1rem 0;"
                "padding:.2rem 1rem;color:var(--ink2);font-style:italic}"
                "</style></head><body><main>%s</main></body></html>"
                % (H.escape(title), body))
        return self._html_resp(page)

    @staticmethod
    def _md2html(text):
        """Small, dependency-free markdown → HTML for the vault's own files.
        Covers what the libraries actually use: frontmatter, #–#### headers,
        fenced code, tables, bullet/numbered lists, quotes, bold/italic/
        inline code/links. Escapes first, transforms second."""
        import html as H
        out, para = [], []
        in_code = in_list = in_table = False
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if fm:
            out.append("<pre class='fm'>%s</pre>" % H.escape(fm.group(1)))
            text = text[fm.end():]

        def inline(s):
            s = H.escape(s)
            s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
            s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
            s = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*", r"<i>\1</i>", s)
            s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"<a href='\2'>\1</a>", s)
            return s

        def flush_para():
            if para:
                out.append("<p>%s</p>" % inline(" ".join(para)))
                del para[:]

        def close_list():
            nonlocal in_list
            if in_list:
                out.append("</ul>")
                in_list = False

        def close_table():
            nonlocal in_table
            if in_table:
                out.append("</table>")
                in_table = False

        for ln in text.split("\n"):
            if ln.startswith("```"):
                flush_para(); close_list(); close_table()
                out.append("</pre>" if in_code else "<pre>")
                in_code = not in_code
                continue
            if in_code:
                out.append(H.escape(ln))
                continue
            if re.match(r"^\s*\|.*\|\s*$", ln):
                flush_para(); close_list()
                if re.match(r"^[\s|:\-]+$", ln):
                    continue
                cells = [c.strip() for c in ln.strip().strip("|").split("|")]
                tag = "td" if in_table else "th"
                if not in_table:
                    out.append("<table>")
                    in_table = True
                out.append("<tr>%s</tr>" % "".join(
                    "<%s>%s</%s>" % (tag, inline(c), tag) for c in cells))
                continue
            close_table()
            m = re.match(r"^(#{1,4})\s+(.*)$", ln)
            if m:
                flush_para(); close_list()
                n = len(m.group(1))
                out.append("<h%d>%s</h%d>" % (n, inline(m.group(2)), n))
                continue
            m = re.match(r"^\s*[-*]\s+(.*)$", ln)
            if m:
                flush_para()
                if not in_list:
                    out.append("<ul>")
                    in_list = True
                out.append("<li>%s</li>" % inline(m.group(1)))
                continue
            m = re.match(r"^\s*\d+\.\s+(.*)$", ln)
            if m:
                flush_para()
                if not in_list:
                    out.append("<ul>")
                    in_list = True
                out.append("<li>%s</li>" % inline(m.group(1)))
                continue
            if ln.startswith(">"):
                flush_para(); close_list()
                out.append("<blockquote>%s</blockquote>" % inline(ln.lstrip("> ")))
                continue
            if not ln.strip():
                flush_para(); close_list()
                continue
            if in_list and ln.startswith("  "):
                # hard-wrapped continuation of the last bullet
                out[-1] = out[-1][:-5] + " " + inline(ln.strip()) + "</li>"
                continue
            close_list()
            para.append(ln.strip())
        flush_para()
        if in_code:
            out.append("</pre>")
        close_list()
        close_table()
        return "\n".join(out)

    def _html_resp(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        return None

    def vault_docs(self):
        """Every reviewable HTML deliverable in the vault, as body-window
        URLs — the Edit drawer's document picker. Excludes the app itself
        (opening lifeos.html inside lifeos.html would recurse)."""
        skip = {"archive", "vendor", "assets", "node_modules",
                "Storage", "Storage (LifeOS share)", "Spine", "mapsec"}
        docs = []
        for dp, dns, fns in os.walk(ROOT):
            rel = os.path.relpath(dp, ROOT)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            dns[:] = [d for d in dns
                      if d not in skip and not d.startswith(".") and depth < 2]
            for fn in fns:
                if not fn.endswith(".html"):
                    continue
                url = "/" + (fn if rel == "." else rel.replace(os.sep, "/") + "/" + fn)
                if url == "/lifeos.html":
                    continue
                docs.append(url)
        return {"ok": True, "docs": sorted(docs)}

    def feedback_submit(self, d):
        """The doc-review rail's 📮 Submit button. The whole pen — prose,
        quoted selections, status — lands as one markdown card in
        ~/LifeOS/inbox/, which the next Claude session reads to begin the
        prompts. Local only; nothing leaves the machine."""
        import time
        label = str(d.get("label", ""))[:200]
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "record"
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.SPINE, "inbox", "feedback-%s-%s.md" % (ts, slug))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines = ["---",
                 "kind: doc-feedback",
                 "record: %s" % str(d.get("id", ""))[:120],
                 "label: %s" % label,
                 "doc: %s" % str(d.get("doc_url", ""))[:300],
                 "status: %s" % str(d.get("status", ""))[:40],
                 "priority: %s" % str(d.get("priority", ""))[:8],
                 "submitted: %s" % time.strftime("%Y-%m-%d %H:%M"),
                 "---", "",
                 "# Feedback from My Map — %s" % (label or "record"), "",
                 str(d.get("feedback", "")).strip()[:8000]
                 or "(no prose — see the quoted selections)", ""]
        sugg = d.get("suggestions")
        if isinstance(sugg, list) and sugg:
            lines += ["## Quoted selections", ""]
            for s in sugg[:40]:
                if not isinstance(s, dict):
                    continue
                q = str(s.get("quote", ""))[:1000].strip()
                n = str(s.get("note", ""))[:1000].strip()
                if q:
                    lines.append("> " + q.replace("\n", "\n> "))
                    if n:
                        lines.append("")
                        lines.append("— " + n)
                    lines.append("")
        lines += ["## For the session that picks this up", "",
                  "This file IS the prompt: open the record's document and its "
                  "library card, apply the feedback above, and reply through the "
                  "normal channel. Delete this file once acted on.", ""]
        with open(path, "w") as f:
            f.write("\n".join(lines))
        return self._json({"ok": True, "path": path.replace(HOME, "~")})

    def profile_get(self):
        j = self._read_json(os.path.join(self.SPINE, "profile", "profile.json"), {})
        j["ok"] = True
        return j

    def profile_save(self, d):
        """Write the member's public card, validated against schema 2.

        This endpoint used to accept any object with a `name` key and write
        it verbatim — so anything that reached it could put arbitrary keys
        and unbounded strings into the ONE file map.ca later reads.

        H1 already stops a web page reaching this endpoint at all. This is
        the second layer: even a caller that gets through cannot leave a
        malformed card behind. The caps mirror `src/lib/lifeos/schema.ts`
        exactly, so the file this writes is one the Connect tile accepts —
        the two are pinned together by a cross-language test.

        Unknown keys are dropped rather than rejected (a newer LifeOS may
        send fields this build predates), but they are named in the
        response so the caller can see what did not survive.
        """
        ok, cleaned, dropped, err = sanitize_profile(d)
        if not ok:
            return self._json({"ok": False, "error": err}, 400)
        self._write_json(
            os.path.join(self.SPINE, "profile", "profile.json"), cleaned
        )
        return self._json({"ok": True, "dropped": dropped})

    def _pins_path(self):
        return os.path.join(self.SPINE, "profile", "pins.json")

    # ------------------------------------------------------------- records
    #
    # The record core — people, projects, money, properties, everything the
    # member actually keeps here — lived only in the browser's localStorage.
    # Clearing site data, or the browser evicting it under pressure, took
    # the lot: these records exist nowhere else, and no backup covered them
    # because there was no file to back up.
    #
    # They now live in the vault, which is zone Z1: never synced, never
    # reachable by map.ca, and already covered by backup.sh. The browser
    # keeps its copy as a cache so the dashboard still works with the
    # server down — offline-first is the product, and a durability fix that
    # made the app require a server would break it.
    def _records_path(self):
        return os.path.join(self.SPINE, "vault", "records.json")

    def records_get(self):
        """The stored record core, or an empty one on a fresh install."""
        empty = {"seq": 0, "records": {}}
        path = self._records_path()
        stored = self._read_json(path, None)
        recovered = False
        if stored is None and os.path.exists(path):
            # Same reasoning as the pins mirror: silently starting from
            # empty would discard everything. Try the last good copy.
            stored = self._read_json(path + ".bak", None)
            recovered = stored is not None
            if recovered:
                sys.stderr.write(
                    "mapai-server: records.json was unreadable; restored the "
                    "backup written by the previous save.\n")
        if not isinstance(stored, dict) or not isinstance(
            stored.get("records"), dict
        ):
            stored = dict(empty)
        out = {"ok": True, "seq": stored.get("seq", 0),
               "records": stored["records"], "count": len(stored["records"])}
        if recovered:
            out["recovered_from_backup"] = True
        return out

    def records_save(self, d):
        """Replace the stored record core.

        Deliberately NOT field-validated the way the profile card is. That
        card is read by map.ca, so narrowing it to a known shape protects
        the reader. These records are private, never leave the machine, and
        their shape is the member's own — dropping a key this build did not
        recognise would destroy their data to satisfy a schema. So the
        checks here are structural only: it must be a record map, and it
        must not be absurdly large.
        """
        if not isinstance(d, dict) or not isinstance(d.get("records"), dict):
            return self._json(
                {"ok": False, "error": "expected {records: {...}}"}, 400
            )
        records = d["records"]
        if len(records) > MAX_RECORDS:
            return self._json({"ok": False, "error":
                               "too many records (%d, limit %d)"
                               % (len(records), MAX_RECORDS)}, 413)
        for key, value in records.items():
            if not isinstance(value, dict):
                return self._json({"ok": False, "error":
                                   "record %r is not an object" % key}, 400)
        payload = {"seq": d.get("seq", 0), "records": records,
                   "saved_at": utc_now()}
        self._write_json(self._records_path(), payload, keep_backup=True)
        return self._json({"ok": True, "count": len(records),
                           "saved_at": payload["saved_at"]})

    def pins_get(self):
        empty = {"schema": 1, "handle": "owner", "source": "offline",
                 "fetched_at": None, "pins": []}
        path = self._pins_path()
        j = self._read_json(path, None)
        recovered = False
        if j is None and os.path.exists(path):
            # The file is there but unreadable. Silently starting from empty
            # would throw away every local-only pin — the records that exist
            # nowhere else. Try the generation kept by the last sync first.
            j = self._read_json(path + ".bak", None)
            recovered = j is not None
            if recovered:
                # Deliberately no write-back here: a read must not have
                # write side-effects. The corrupt file stays on disk until
                # the next add/sync persists the recovered data over it.
                sys.stderr.write(
                    "mapai-server: pins.json was unreadable; restored the "
                    "backup written by the last sync.\n")
        if j is None:
            j = dict(empty)
        j["ok"] = True
        if recovered:
            j["recovered_from_backup"] = True
        return j

    def pins_add(self, d):
        """Drop a local pin — the offline half of the pins API."""
        import time
        title = str(d.get("title", "")).strip()
        if not title:
            return self._json({"ok": False, "error": "a pin needs a title"}, 400)
        j = self.pins_get()
        pin = {"id": "local-%d" % int(time.time() * 1000), "origin": "local",
               "title": title[:120],
               "category": str(d.get("category", "personal"))[:40],
               "location": str(d.get("location", ""))[:120],
               "note": str(d.get("note", ""))[:500],
               "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        for k in ("lat", "lng"):
            try:
                pin[k] = float(d[k])
            except (KeyError, TypeError, ValueError):
                pin[k] = None
        # Response-only keys must not persist into the mirror: pins_get()
        # may have set recovered_from_backup on this same dict.
        j.pop("ok", None)
        j.pop("recovered_from_backup", None)
        j["pins"].append(pin)
        self._write_json(self._pins_path(), j)
        return self._json({"ok": True, "pin": pin, "count": len(j["pins"])})

    def _mapca_config_path(self):
        return os.path.join(self.SPINE, "profile", "mapca.json")

    def _mapca_connection(self):
        """Read the map.ca connection settings.

        Returns (base_url, anon_key, handle, error). `error` is a member-
        readable string when the connection is unusable, else None.

        These values are public by design — the anon key is the same one
        shipped in every visitor's browser bundle, and it reads only
        already-published pins through the same RLS-guarded doors the
        website uses. LifeOS holds nothing else: no session token, no
        service key (ADR 0081 §2).

        Until 0.2.0 this scraped `~/map-ca/.env.local`, which only ever
        worked on a machine that happened to have the website checked out —
        an accident of the author's laptop, not a design. That path is kept
        for one release as a deprecated fallback so existing installs keep
        syncing while they migrate.
        """
        cfg = self._read_json(self._mapca_config_path(), None)
        if isinstance(cfg, dict) and cfg.get("base_url") and cfg.get("anon_key"):
            contract = cfg.get("contract")
            if contract != MAPCA_CONTRACT:
                return (None, None, None,
                        "profile/mapca.json declares contract %r; this LifeOS "
                        "speaks contract %d. Update LifeOS, or fix the file."
                        % (contract, MAPCA_CONTRACT))
            return (str(cfg["base_url"]), str(cfg["anon_key"]),
                    str(cfg.get("handle") or ""), None)

        url, key = self._legacy_env_scrape()
        if url and key:
            sys.stderr.write(
                "mapai-server: reading map.ca settings from ~/map-ca/.env.local "
                "is DEPRECATED and will be removed. Write profile/mapca.json "
                "instead — see SETUP.md.\n")
            return (url, key, "", None)
        return (None, None, None,
                "not connected to map.ca. Create profile/mapca.json with "
                "{\"contract\": %d, \"base_url\": \"...\", \"anon_key\": \"...\", "
                "\"handle\": \"...\"} — see SETUP.md." % MAPCA_CONTRACT)

    def _legacy_env_scrape(self):
        """DEPRECATED (removal after 0.3.0): settings from a map-ca checkout."""
        env = {}
        for name in (".env.local", ".env"):
            p = os.path.join(HOME, "map-ca", name)
            try:
                with open(p) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            env.setdefault(k.strip(), v.strip())
            except OSError:
                continue
        return (env.get("NEXT_PUBLIC_SUPABASE_URL"),
                env.get("NEXT_PUBLIC_SUPABASE_ANON_KEY"))

    def _sync_log(self, entry):
        """Append one line to the local sync log and trim it to SYNC_LOG_KEEP.

        Diagnostics stay on this machine: plain JSONL a member can `tail`,
        indexed by nothing, transmitted nowhere. It exists so "why is my
        mirror stale?" has an answer that does not require a support ticket.
        """
        path = os.path.join(self.SPINE, "profile", "sync-log.jsonl")
        entry = dict(entry, ts=utc_now())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            lines = []
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            lines.append(json.dumps(entry))
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[-SYNC_LOG_KEEP:]) + "\n")
        except OSError:
            pass  # never fail a sync because its diary could not be written

    def pins_sync(self, d):
        """Pull live pins from map.ca (Supabase) into the offline store.

        Mirrors CommunityDataService.getPublicPinsByAuthor: published,
        publicly-visible posts by the profile's author. Local pins
        (origin:'local') always survive. Degrades to a clear error when
        the Supabase env isn't configured — offline store stays canonical.
        """
        import urllib.request
        import urllib.parse
        url, key, cfg_handle, err = self._mapca_connection()
        if err:
            # Not an error state for the member: the offline store is
            # complete and canonical. Say so plainly.
            self._sync_log({"trigger": d.get("trigger") or "user",
                            "outcome": "offline"})
            return self._json({"ok": False, "offline": True, "error": err})
        j = self.pins_get()
        j.pop("ok", None)
        handle = str(d.get("handle") or cfg_handle or j.get("handle") or "owner")
        hdrs = {"apikey": key, "Authorization": "Bearer " + key,
                "Content-Type": "application/json"}

        def rq(path, data=None):
            req = urllib.request.Request(url.rstrip("/") + path, headers=hdrs,
                                         data=json.dumps(data).encode() if data is not None else None)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())

        try:
            prof = rq("/rest/v1/rpc/public_profile_by_handle",
                      {"handle_in": handle.lower()})
            row = prof[0] if isinstance(prof, list) and prof else prof
            author = (row or {}).get("id")
            if not author:
                # Distinguish "never connected" from "was connected, handle
                # moved" — the second is a real thing map.ca supports, and
                # the fix is different.
                known = bool(j.get("fetched_at"))
                self._sync_log({"trigger": d.get("trigger") or "user",
                                "outcome": "handle-unknown", "handle": handle})
                return self._json({"ok": False, "error":
                                   "no map.ca profile for @%s.%s" % (
                                       handle,
                                       " You synced as this handle before — "
                                       "did you rename it? Update "
                                       "profile/mapca.json." if known else "")})
            q = ("/rest/v1/posts_with_coords?select=*&status=eq.published"
                 "&author_id=eq." + urllib.parse.quote(str(author)) +
                 "&order=created_at.desc&limit=500")
            posts = rq(q)
        except Exception as exc:
            self._sync_log({"trigger": d.get("trigger") or "user",
                            "outcome": "fetch-failed", "error": str(exc)})
            return self._json({"ok": False, "error": "live fetch failed: %s" % exc})
        live, skipped = [], 0
        for p in posts if isinstance(posts, list) else []:
            pin = self._map_remote_pin(p)
            if pin is None:
                # One malformed record must not fail the whole sync — the
                # other 499 pins are still the member's data (contract §8).
                skipped += 1
                continue
            live.append(pin)
        local = [p for p in j.get("pins", []) if p.get("origin") == "local"]
        j.update({"handle": handle, "source": "map.ca",
                  "fetched_at": utc_now(),
                  "pins": live + local})
        self._write_json(self._pins_path(), j, keep_backup=True)
        self._sync_log({"trigger": d.get("trigger") or "user", "outcome": "ok",
                        "handle": handle, "live": len(live),
                        "local": len(local), "skipped": skipped})
        return self._json({"ok": True, "live": len(live), "local": len(local),
                           "skipped": skipped, "fetched_at": j["fetched_at"]})

    @staticmethod
    def _map_remote_pin(p):
        """One published post -> a Pin (contract §4). None when unusable.

        `latitude`/`longitude` vs `lat`/`lng` are both accepted because the
        view has carried both spellings; a pin with neither is still legal
        (it simply does not draw), so only a missing id disqualifies a row.
        """
        if not isinstance(p, dict) or not p.get("id"):
            return None
        return {"id": str(p.get("id")), "origin": "map.ca",
                "title": p.get("title") or "", "category": p.get("category") or "",
                "subcategory": p.get("subcategory"), "location": p.get("location"),
                "lat": p.get("latitude") or p.get("lat"),
                "lng": p.get("longitude") or p.get("lng"),
                "thumbnail_url": p.get("thumbnail_url"),
                "created_at": p.get("created_at")}

    def status(self):
        icon = None
        if os.path.exists(ICON_PNG):
            with open(ICON_PNG, "rb") as fh:
                icon = "data:image/png;base64," + base64.b64encode(fh.read()).decode()
        size = 0
        for dp, _, fs in os.walk(VAULT_IMG):
            for f in fs:
                try:
                    size += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        pw = keychain_get("MAPAI_VAULT_PASSWORD") or ""
        return {
            "ok": True,
            "open": os.path.ismount(VAULT_MP),
            "image": VAULT_IMG,
            "mount": VAULT_MP,
            "bytes_on_disk": size,
            "icon": icon,
            "password_len": len(pw),
            "password_in_keychain": bool(pw),
            "nas": keychain_get("SYNOLOGY_HOST"),
            "agent": keychain_get("SYNOLOGY_AGENT_USER"),
        }

    def set_password(self, d):
        old = d.get("current") or ""
        new = d.get("new") or ""
        if not old or not new:
            return self._json({"ok": False, "error":
                               "both the current and the new password are required"}, 400)
        if new == old:
            return self._json({"ok": False, "error": "that is the same password"}, 400)
        if len(new) < 1:
            return self._json({"ok": False, "error": "the new password cannot be empty"}, 400)

        # Driven through expect, NOT piped into hdiutil. `-oldstdinpass` eats
        # the whole of stdin as the old password, leaving `-newstdinpass` empty,
        # and hdiutil then sets a BLANK password and reports success. That is a
        # silent total loss of protection. See mapai-chpass.exp.
        was_open = os.path.ismount(VAULT_MP)
        if was_open:
            sh(["hdiutil", "detach", VAULT_MP, "-quiet"]) or \
                sh(["hdiutil", "detach", VAULT_MP, "-force", "-quiet"])

        p = sh(["expect", os.path.join(BIN, "mapai-chpass.exp"), VAULT_IMG, old, new])
        if p.returncode != 0:
            if was_open:
                subprocess.run(["hdiutil", "attach", "-stdinpass", "-quiet", VAULT_IMG],
                               input=old, text=True, capture_output=True)
            msg = ("the current password is wrong" if p.returncode == 1 else
                   "the new password cannot be empty" if p.returncode == 3 else
                   "hdiutil timed out" if p.returncode == 2 else
                   "hdiutil refused the change")
            return self._json({"ok": False, "error": msg}, 400)

        # Prove the new password actually works before trusting it. If hdiutil
        # ever sets something other than what we asked for, this is what catches
        # it while the old password may still be recoverable.
        v = subprocess.run(["hdiutil", "attach", "-stdinpass", "-quiet", VAULT_IMG],
                           input=new, text=True, capture_output=True)
        if v.returncode != 0 or not os.path.ismount(VAULT_MP):
            return self._json({
                "ok": False, "critical": True,
                "error": "hdiutil reported success but the vault will NOT open with "
                         "the new password. Do not close this window. Try opening it "
                         "by hand with both the old and new password: "
                         "hdiutil attach ~/Desktop/MapAi.sparsebundle . Your files are "
                         "also backed up at ~/.mapai/backups/ and on the NAS.",
            }, 500)
        if not was_open:
            sh(["hdiutil", "detach", VAULT_MP, "-quiet"])

        if not keychain_set("MAPAI_VAULT_PASSWORD", new):
            return self._json({
                "ok": False,
                "critical": True,
                "error": "The vault password WAS changed, but writing it to the "
                         "Keychain failed. Nothing can open the vault unattended "
                         "until you run:  mapsec set MAPAI_VAULT_PASSWORD  "
                         "and enter the new password. Write it down first.",
            }, 500)
        strength = ("very weak — digits only" if new.isdigit() else
                    "weak — under 8 characters" if len(new) < 8 else "reasonable")
        return self._json({"ok": True, "length": len(new), "strength": strength})

    def set_icon(self, d):
        raw = d.get("image_base64") or ""
        raw = re.sub(r"^data:[^,]+,", "", raw)
        try:
            blob = base64.b64decode(raw, validate=True)
        except Exception:
            return self._json({"ok": False, "error": "that was not valid image data"}, 400)
        if not blob:
            return self._json({"ok": False, "error": "no image was sent"}, 400)
        if len(blob) > MAX_UPLOAD:
            return self._json({"ok": False, "error": "image is over 8 MB"}, 413)
        if not any(blob.startswith(m) or (m == b"icns" and blob[:4] == b"icns")
                   for m in MAGIC):
            return self._json({"ok": False, "error":
                               "that file is not a PNG, JPEG, GIF, WEBP or ICNS"}, 400)

        suffix = (".icns" if blob[:4] == b"icns" else
                  ".jpg" if blob[:3] == b"\xff\xd8\xff" else ".png")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(blob)
        tmp.close()
        try:
            r = sh([os.path.join(BIN, "mapai-icon"), tmp.name])
            if r.returncode != 0:
                return self._json({"ok": False,
                                   "error": (r.stderr or r.stdout).strip()[:300]}, 500)
        finally:
            os.unlink(tmp.name)
        return self._json({"ok": True, "note": r.stdout.strip()})

    def doc_extract(self, d):
        """Photo of a document in, House DNA fields out. Apple Vision reads it,
        the local model understands it, doc-extract grounds every value back in
        the OCR text before returning it."""
        raw = re.sub(r"^data:[^,]+,", "", d.get("image_base64") or "")
        try:
            blob = base64.b64decode(raw, validate=True)
        except Exception:
            return self._json({"ok": False, "error": "not valid image data"}, 400)
        if not blob:
            return self._json({"ok": False, "error": "no image sent"}, 400)
        if len(blob) > MAX_UPLOAD:
            return self._json({"ok": False, "error": "image is over 8 MB"}, 413)

        suffix = (".pdf" if blob[:4] == b"%PDF" else
                  ".jpg" if blob[:3] == b"\xff\xd8\xff" else ".png")
        if blob[:4] != b"%PDF" and not any(blob.startswith(m) for m in MAGIC):
            return self._json({"ok": False,
                               "error": "not a PNG, JPEG, GIF, WEBP or PDF"}, 400)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(blob)
        tmp.close()
        cmd = [os.path.join(BIN, "doc-extract"), tmp.name, "--quiet"]
        if d.get("model"):
            cmd += ["--model", str(d["model"])]
        if d.get("doc_type"):
            cmd += ["--type", str(d["doc_type"])]
        if d.get("property"):
            cmd += ["--property", str(d["property"]), "--save"]
        try:
            # 32B on a big scan can take a while; the UI shows progress meanwhile.
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            os.unlink(tmp.name)
            return self._json({"ok": False, "error": "extraction timed out"}, 504)
        finally:
            if os.path.exists(tmp.name):
                os.unlink(tmp.name)
        if r.returncode != 0:
            return self._json({"ok": False,
                               "error": (r.stderr or r.stdout).strip()[:400]}, 500)
        try:
            out = json.loads(r.stdout)
        except ValueError:
            return self._json({"ok": False, "error": "extractor returned no JSON"}, 500)
        out["ok"] = True
        out.pop("raw_text", None)          # keep the payload small; it is on disk
        return self._json(out)

    def reset_icon(self, d):
        if not os.path.exists(BRAND_LOGO):
            return self._json({"ok": False,
                               "error": "the map.ca logo is not at %s" % BRAND_LOGO}, 404)
        r = sh([os.path.join(BIN, "mapai-icon"), BRAND_LOGO])
        if r.returncode != 0:
            return self._json({"ok": False,
                               "error": (r.stderr or r.stdout).strip()[:300]}, 500)
        return self._json({"ok": True, "note": "map.ca logo restored"})


def main():
    if not os.path.isdir(ROOT):
        sys.stderr.write("mapai-server: %s is not there — is the vault open?\n" % ROOT)
        sys.exit(2)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("mapai-server: serving %s on http://127.0.0.1:%d\n" % (ROOT, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
