#!/usr/bin/env python3
"""
m-comms/1 — private peer relay for M · LifeOS.

Text chat + file transfer between your own machines. Transport is
SSE down / POST up (stdlib only — no pip, so no WebSocket server).
Intended to run bound to a Tailscale interface: WireGuard-encrypted,
no router ports opened, no third party holding message contents.

Run (loopback, no token needed — nothing off this machine can reach it):
    python3 ~/Desktop/MapAi/comms.py

To actually reach your other machines, bind the tailnet address. A token
is REQUIRED for any non-loopback bind and the service refuses to start
without one; WireGuard is the security model, the token is the backstop:
    M_COMMS_BIND=100.x.y.z \
    M_COMMS_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
    python3 comms.py

Never bind 0.0.0.0 on a network you do not control — that publishes the
relay to every host on a cafe or hotel LAN.

Endpoints (see HANDOFF-comms.md §2 for the contract):
    GET  /health                    -> {ok, relay, peers, up_since}
    GET  /events?peer=NAME&since=ID -> text/event-stream
    POST /send    {peer, text}      -> {ok, id}
    POST /file    raw bytes         -> {ok, id, name, size}
    GET  /file/<fid>                -> the bytes
"""
import os, re, sys, json, time, queue, threading, uuid
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT  = int(os.environ.get("M_COMMS_PORT", "8788"))
# Loopback by default. This relay carries your messages and files, and the
# previous default (0.0.0.0) published it to whatever network you happened
# to be on — a cafe or hotel LAN included. Opting out is deliberate and
# documented: set M_COMMS_BIND to your tailnet address, where WireGuard is
# the security model and the token is only a backstop.
BIND  = os.environ.get("M_COMMS_BIND", "127.0.0.1")
TOKEN = os.environ.get("M_COMMS_TOKEN", "")

# Browser origins allowed to drive the relay: a LifeOS dashboard on this
# machine, and — when the relay is published to a tailnet — one served from
# that same address. A wildcard here would be a hole, because the token
# (the other guard) is empty in the default configuration.
DASHBOARD_PORT = int(os.environ.get("MAPAI_WEB_PORT", "8765"))
_ORIGIN_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
if BIND not in ("0.0.0.0", "::", ""):
    _ORIGIN_HOSTS.add(BIND)
ALLOWED_ORIGINS = frozenset(
    "http://%s:%d" % (host, port)
    for host in _ORIGIN_HOSTS
    for port in (DASHBOARD_PORT, PORT)
)
ROOT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comms")
FILES = os.path.join(ROOT, "files")
os.makedirs(FILES, exist_ok=True)

MAX_FILE = 100 * 1024 * 1024
RING     = 200
NAME_RE  = re.compile(r"^[a-z0-9-]{1,32}$")
UP       = int(time.time())

LOCK = threading.Lock()
LOG  = []      # ring buffer of msg/file events, oldest first
SUBS = {}      # peer -> [ {q, since} ]  one entry per open /events stream
SEQ  = [0]     # monotonic event id


def publish(ev, store=True):
    """Assign an id, fan out to every open stream, optionally keep in the ring.

    presence frames pass store=False: they carry an id (ids stay monotonic)
    but are never replayed on reconnect — stale presence is worse than none,
    and a fresh one is always sent the moment a stream opens.
    """
    with LOCK:
        SEQ[0] += 1
        ev["id"] = SEQ[0]
        ev.setdefault("ts", int(time.time()))
        if store:
            LOG.append(ev)
            del LOG[:-RING]
        subs = [s for lst in SUBS.values() for s in lst]
    for s in subs:
        s["q"].put(ev)
    return ev["id"]


def presence():
    with LOCK:
        peers = [{"peer": p, "since": min(s["since"] for s in lst)}
                 for p, lst in SUBS.items() if lst]
    publish({"t": "presence", "peers": peers}, store=False)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- plumbing -------------------------------------------------
    def _origin_ok(self):
        """True when this request may use the relay.

        The token is not a substitute for this check, because the token is
        EMPTY in the default (loopback) configuration — so with a wildcard
        CORS header, any page the owner had open could POST /send into the
        relay and read /events, which streams message text and file names.
        That is the same cross-origin shape the server and bridge guards
        close; it applies here too.

        No Origin  -> a local process or a peer script, not a browser page.
        Known Origin -> a LifeOS dashboard on this machine (or on the bind
                        address, when the relay is published to a tailnet).
        """
        origin = self.headers.get("Origin")
        return origin is None or origin.strip().lower() in ALLOWED_ORIGINS

    def _deny_origin(self):
        b = b'{"ok":false,"error":"forbidden: origin not allowed"}'
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin.strip().lower() in ALLOWED_ORIGINS:
            # Echo the caller's own origin — never "*", which would hand the
            # relay to every page in the browser.
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-M-Token, X-File-Name, X-Peer")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _auth(self, args=None):
        """Header token normally. EventSource and <a download> cannot set
        headers, so /events and /file/* also accept ?token= — those two only."""
        if not TOKEN:
            return True
        got = self.headers.get("X-M-Token") or (args or {}).get("token")
        if got == TOKEN:
            return True
        self._json({"ok": False, "error": "unauthorized"}, 401)
        return False

    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET ------------------------------------------------------
    def do_GET(self):
        if not self._origin_ok():
            return self._deny_origin()
        path, _, qs = self.path.partition("?")
        args = {}
        for p in qs.split("&"):
            if "=" in p:
                k, v = p.split("=", 1)
                args[k] = unquote(v)

        if path == "/health":
            if not self._auth(args): return
            with LOCK:
                peers = [{"peer": p, "since": min(s["since"] for s in lst)}
                         for p, lst in SUBS.items() if lst]
            return self._json({"ok": True, "relay": "m-comms/1",
                               "peers": peers, "up_since": UP})

        if path.startswith("/file/"):
            if not self._auth(args): return
            fid = os.path.basename(path[len("/file/"):])
            fp = os.path.join(FILES, fid)
            if not fid or not os.path.isfile(fp):
                return self._json({"ok": False, "error": "not found"}, 404)
            size = os.path.getsize(fp)
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % fid.split("-", 1)[-1])
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(fp, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)
            return

        if path == "/events":
            if not self._auth(args): return
            peer = args.get("peer", "")
            if not NAME_RE.match(peer):
                return self._json({"ok": False, "error": "bad peer name"}, 400)
            try:
                since = int(args.get("since", "0") or 0)
            except ValueError:
                since = 0

            sub = {"q": queue.Queue(), "since": int(time.time())}
            with LOCK:
                SUBS.setdefault(peer, []).append(sub)
                backlog = [e for e in LOG if e["id"] > since]

            self.send_response(200); self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                for e in backlog:
                    self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n")
                self.wfile.flush()
                threading.Thread(target=presence, daemon=True).start()
                while True:
                    try:
                        e = sub["q"].get(timeout=20)
                        self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")   # keeps NAT/proxies from killing it
                    self.wfile.flush()
            except Exception:
                pass                                            # client hung up
            finally:
                with LOCK:
                    lst = SUBS.get(peer, [])
                    if sub in lst: lst.remove(sub)
                    if not lst: SUBS.pop(peer, None)
                threading.Thread(target=presence, daemon=True).start()
            return

        self._json({"ok": False, "error": "not found"}, 404)

    # ---- POST -----------------------------------------------------
    def do_POST(self):
        # Origin first: refuse a foreign page before the token check, so a
        # tokenless default install is not the thing standing between a web
        # page and the relay.
        if not self._origin_ok():
            return self._deny_origin()
        if not self._auth(): return
        n = int(self.headers.get("Content-Length", 0) or 0)

        if self.path == "/send":
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json({"ok": False, "error": "bad json"}, 400)
            peer = body.get("peer", "")
            text = (body.get("text") or "")[:8000]
            if not NAME_RE.match(peer):
                return self._json({"ok": False, "error": "bad peer name"}, 400)
            if not text.strip():
                return self._json({"ok": False, "error": "empty"}, 400)
            return self._json({"ok": True,
                               "id": publish({"t": "msg", "from": peer, "text": text})})

        if self.path == "/file":
            peer = self.headers.get("X-Peer", "")
            # header values must be latin-1, so senders percent-encode the
            # filename; a plain ASCII name unquotes to itself.
            name = os.path.basename(unquote(self.headers.get("X-File-Name", "file.bin")))[:120]
            if not NAME_RE.match(peer):
                return self._json({"ok": False, "error": "bad peer name"}, 400)
            if n <= 0:
                return self._json({"ok": False, "error": "empty"}, 400)
            if n > MAX_FILE:
                return self._json({"ok": False, "error": "too large"}, 413)
            fid = uuid.uuid4().hex[:16] + "-" + (re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file.bin")
            fp = os.path.join(FILES, fid)
            got = 0
            try:
                with open(fp, "wb") as f:
                    while got < n:
                        chunk = self.rfile.read(min(65536, n - got))
                        if not chunk: break
                        f.write(chunk); got += len(chunk)
            except Exception:
                try: os.remove(fp)
                except OSError: pass
                return self._json({"ok": False, "error": "write failed"}, 500)
            if got != n:
                try: os.remove(fp)
                except OSError: pass
                return self._json({"ok": False, "error": "truncated upload"}, 400)
            mid = publish({"t": "file", "from": peer, "name": name,
                           "size": got, "fid": fid})
            return self._json({"ok": True, "id": mid, "name": name, "size": got})

        self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, *a):
        pass


def _require_token_for_public_bind():
    """Refuse to publish an unauthenticated relay to a network.

    On loopback the token is optional: nothing off this machine can reach
    the port anyway. The moment the bind address is anything else, the
    relay is reachable by other hosts, and starting it without a token
    would put your messages and files behind no check at all. Fail loudly
    at startup rather than quietly serving them.
    """
    if BIND in ("127.0.0.1", "::1", "localhost") or TOKEN:
        return
    sys.stderr.write(
        "refusing to start: M_COMMS_BIND=%s exposes this relay beyond this\n"
        "machine, but M_COMMS_TOKEN is empty. Set a token:\n\n"
        "    M_COMMS_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \\\n"
        "    M_COMMS_BIND=%s python3 comms.py\n" % (BIND, BIND)
    )
    raise SystemExit(2)


if __name__ == "__main__":
    _require_token_for_public_bind()
    print("m-comms/1  ->  http://%s:%d" % (BIND, PORT))
    print("files      ->  %s" % FILES)
    print("token      ->  %s" % ("set" if TOKEN else "none (loopback only)"))
    if BIND not in ("127.0.0.1", "::1", "localhost"):
        print("note       ->  reachable beyond this machine; token required and set")
    try:
        ThreadingHTTPServer((BIND, PORT), H).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
