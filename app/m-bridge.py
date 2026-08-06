#!/usr/bin/env python3
"""
M bridge — routes the LifeOS dashboard's frontier chat through the
locally-installed Claude Code binary, which uses the owner's Max subscription
(apiKeySource: none). No Anthropic API key involved.

Run:   python3 ~/Desktop/MapAi/m-bridge.py
Then in the dashboard Settings, enable "Route Claude via local bridge".

Endpoints:
  GET  /            -> health {ok, binary}
  POST /v1/chat     -> body {messages:[{role,content}], model} -> SSE data: {"text":...}
"""
import glob, os, re, json, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("M_BRIDGE_PORT", "8787"))

# Who may drive this bridge from a browser.
#
# This endpoint spends the owner's Claude subscription and returns the
# model's answer. With `Access-Control-Allow-Origin: *` — what this file
# shipped with — ANY page the owner had open could POST here, run prompts
# on their account, and read the replies. The only cost to the attacker
# was knowing the port.
#
# So: echo the Origin back only when it is the LifeOS dashboard. Callers
# with no Origin at all (the CLI, curl, a launcher script) are local
# processes, not browser pages, and keep working.
DASHBOARD_PORT = int(os.environ.get("MAPAI_WEB_PORT", "8765"))
ALLOWED_ORIGINS = frozenset(
    "http://%s:%d" % (host, port)
    for host in ("localhost", "127.0.0.1", "[::1]")
    for port in (DASHBOARD_PORT, PORT)
)

# dashboard model id -> claude CLI --model value
MODEL_MAP = {
    "claude-opus-4-8": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-haiku-4-5": "haiku",
    "claude-fable-5": "claude-fable-5",
    "opus": "opus", "sonnet": "sonnet", "haiku": "haiku",
}

def find_claude():
    pats = glob.glob(os.path.expanduser(
        "~/.vscode*/extensions/anthropic.claude-code-*/resources/native-binary/claude"))
    if not pats:
        return None
    def ver(p):
        m = re.search(r"claude-code-(\d+)\.(\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return sorted(pats, key=ver)[-1]

CLAUDE = find_claude()

def to_text(c):
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""

def build_prompt(messages):
    """Flatten user/assistant history into one prompt for `claude -p`."""
    turns = []
    for m in messages:
        role = m.get("role")
        txt = to_text(m.get("content"))
        if not txt:
            continue
        if role == "assistant":
            turns.append("Assistant: " + txt)
        elif role == "user":
            turns.append("User: " + txt)
    if not turns:
        return ""
    # single turn -> just the message; multi -> transcript then cue a reply
    if len(turns) == 1 and turns[0].startswith("User: "):
        return turns[0][6:]
    return "\n".join(turns) + "\nAssistant:"

class H(BaseHTTPRequestHandler):
    def _origin_ok(self):
        """True when this request may use the bridge.

        No Origin header  -> a local process (CLI, script). Allowed.
        Known Origin      -> the dashboard. Allowed.
        Anything else     -> a foreign page. Refused.
        """
        origin = self.headers.get("Origin")
        return origin is None or origin.strip().lower() in ALLOWED_ORIGINS

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin.strip().lower() in ALLOWED_ORIGINS:
            # Echo the exact origin — never "*", which would re-open the hole.
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _deny(self):
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":false,"error":"forbidden: origin not allowed"}')

    def do_OPTIONS(self):
        # A disallowed origin gets no CORS headers, so the browser's
        # preflight fails and the real request is never sent.
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._deny()
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"ok": CLAUDE is not None, "binary": CLAUDE}).encode())

    def _send(self, txt):
        self.wfile.write(("data: " + json.dumps({"text": txt}) + "\n\n").encode())
        self.wfile.flush()

    def do_POST(self):
        # The subscription-spending path. Guard it before reading a byte.
        if not self._origin_ok():
            return self._deny()
        ln = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            body = {}
        messages = body.get("messages", [])
        model = MODEL_MAP.get(body.get("model", ""), body.get("model") or "sonnet")
        sys_txt = "\n".join(to_text(m.get("content")) for m in messages if m.get("role") == "system")
        prompt = build_prompt([m for m in messages if m.get("role") != "system"])

        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache"); self.end_headers()

        if not CLAUDE:
            self._send("[bridge] Claude Code binary not found under ~/.vscode/extensions/")
            self.wfile.write(b"data: [DONE]\n\n"); return
        if not prompt:
            self.wfile.write(b"data: [DONE]\n\n"); return

        args = [CLAUDE, "-p", prompt, "--model", model,
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if sys_txt:
            args += ["--append-system-prompt", sys_txt]
        proc = None
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, bufsize=1)
            any_delta = False
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "stream_event":
                    e = ev.get("event", {})
                    if e.get("type") == "content_block_delta" and e.get("delta", {}).get("type") == "text_delta":
                        any_delta = True
                        self._send(e["delta"]["text"])
                elif t == "result" and not any_delta:
                    r = ev.get("result", "")
                    if r:
                        self._send(r)
            proc.wait()
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if proc and proc.poll() is None:
                proc.kill()

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    print(f"M bridge  ->  http://localhost:{PORT}")
    print(f"binary    ->  {CLAUDE or 'NOT FOUND'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
