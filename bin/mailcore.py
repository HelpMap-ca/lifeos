#!/usr/bin/env python3
"""mailcore — LifeOS mail engine: sync, search, send, and the local-model agent.

One mailbox, many addresses: mail for every alias funnels into a single IMAP
login. The integrated inbox splits that stream into named lanes by
delivered-to address, so the UI can show (say) "map.ca" and personal mail
side by side.

Who that mailbox belongs to is CONFIGURATION, not code: `profile/mail.json`
holds the host, login, aliases and lane needles, and `profile/mail.example.json`
is the template. No address appears in this file, so there is nothing here to
leak, and nothing to scrub when someone runs a hygiene scan.

Credentials are not in the config either — the app password stays in the OS
keychain via mapsec, and the config only names which entry to read. Never
argv, never a file in the repo, never this process's environment at rest.
Reads use BODY.PEEK so the poller/other readers never see messages flip state.

Store: ~/LifeOS/vault/mail/  (vault rule: never synced to map.ca)
  mail.db        sqlite — message index, lanes, seen flags
  bodies/<uid>   full text bodies
  sent.jsonl     everything sent through here
  drafts.jsonl   agent-written drafts awaiting a human send
"""
import email
import email.header
import email.utils
import imaplib
import json
import os
import re
import smtplib
import sqlite3
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate

HOME = os.path.expanduser("~")
BIN = os.path.join(HOME, "LifeOS", "bin")
MAILDIR = os.path.join(HOME, "LifeOS", "vault", "mail")
BODIES = os.path.join(MAILDIR, "bodies")
DB = os.path.join(MAILDIR, "mail.db")

SYNC_DAYS = 60          # first-run window
FLAG_REFRESH = 300      # newest N messages get their \Seen flag re-checked

# ---------------------------------------------------------------- identity
#
# Who this mailbox belongs to lives in `profile/mail.json`, NOT here.
#
# It used to be module constants, which had two costs. Every install shipped
# with someone else's example addresses baked in and silently synced nothing
# until a person edited the source. And keeping a real mailbox out of the
# tree became a recurring chore — the placeholder was rewritten in-place
# once already to satisfy a hygiene scan. Configuration has neither problem:
# there is no address in this file to leak or to scrub, and the member's own
# details never enter version control.
#
# The password is still not here either: it stays in the OS keychain via
# `mapsec`, and this file only names which entry to read.
CONFIG_PATH = os.path.join(HOME, "LifeOS", "profile", "mail.json")

DEFAULTS = {
    "imap_host": "",
    "smtp_host": "",
    "login": "",
    "aliases": [],
    "secret_name": "",
    # delivered-to needles that split the one stream into named lanes
    "lanes": {},
}


def load_config(path=None):
    """Read profile/mail.json, falling back to empty (= not configured)."""
    try:
        with open(path or CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULTS)
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    cfg = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw and isinstance(raw[key], type(DEFAULTS[key])):
            cfg[key] = raw[key]
    # A login is always a valid sender, whether or not it was listed.
    if cfg["login"] and cfg["login"] not in cfg["aliases"]:
        cfg["aliases"] = [cfg["login"]] + list(cfg["aliases"])
    return cfg


def config_error(cfg):
    """A member-readable reason the mail engine cannot run, or None."""
    missing = [k for k in ("imap_host", "login", "secret_name") if not cfg.get(k)]
    if missing:
        return ("mail is not configured: %s missing from profile/mail.json "
                "(copy profile/mail.example.json to start)" % ", ".join(missing))
    return None


CFG = load_config()
IMAP_HOST = CFG["imap_host"]
SMTP_HOST = CFG["smtp_host"] or CFG["imap_host"].replace("imap.", "smtp.", 1)
LOGIN = CFG["login"]
ALIASES = CFG["aliases"]
SECRET_NAME = CFG["secret_name"]
LANES = CFG["lanes"]


# ------------------------------------------------------------------ store
def db():
    os.makedirs(BODIES, exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS messages(
        uid INTEGER PRIMARY KEY, msgid TEXT, ts REAL, date TEXT,
        sender TEXT, sender_email TEXT, recipients TEXT, delivered TEXT,
        subject TEXT, snippet TEXT, lane TEXT, seen INTEGER, has_body INTEGER)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(ts DESC)")
    return c


def secret(name):
    try:
        out = subprocess.run([os.path.join(BIN, "mapsec"), "get", name],
                             capture_output=True, text=True, timeout=20)
        v = out.stdout.strip()
        return v if out.returncode == 0 and v else None
    except Exception:
        return None


# ------------------------------------------------------------------ helpers
def dec(s):
    if not s:
        return ""
    parts = email.header.decode_header(s)
    out = ""
    for txt, enc in parts:
        out += txt.decode(enc or "utf-8", "replace") if isinstance(txt, bytes) else txt
    return re.sub(r"\s+", " ", out).strip()


def lane_of(delivered, recipients):
    hay = " ".join([delivered or ""] + (recipients or [])).lower()
    for lane, needles in LANES.items():
        if any(n in hay for n in needles):
            return lane
    return "other"


def body_text(msg):
    """Prefer text/plain; fall back to de-tagged HTML."""
    plain, html_part = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ctype == "text/plain" and plain is None:
            plain = text
        elif ctype == "text/html" and html_part is None:
            html_part = text
    if plain is None and html_part is not None:
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_part)
        t = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", t)
        t = re.sub(r"<[^>]+>", " ", t)
        plain = re.sub(r"[ \t]+", " ", email.utils.unquote(t))
        plain = re.sub(r"\n{3,}", "\n\n", plain)
    return (plain or "").strip()[:120000]


# ------------------------------------------------------------------ sync
def sync():
    err = config_error(CFG)
    if err:
        return {"ok": False, "error": err}
    pw = secret(SECRET_NAME)
    if not pw:
        return {"ok": False, "error": "no credentials — run: mapsec set " + SECRET_NAME}
    con = db()
    last = con.execute("SELECT MAX(uid) FROM messages").fetchone()[0] or 0
    box = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        box.login(LOGIN, pw)
        box.select("INBOX", readonly=True)
        if last:
            typ, data = box.uid("SEARCH", None, "UID %d:*" % (last + 1))
        else:
            since = time.strftime("%d-%b-%Y", time.localtime(time.time() - SYNC_DAYS * 86400))
            typ, data = box.uid("SEARCH", None, "SINCE", since)
        uids = [int(u) for u in (data[0].split() if data and data[0] else [])
                if int(u) > last]
        added = 0
        for uid in uids:
            typ, fd = box.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
            if typ != "OK" or not fd or fd[0] is None:
                continue
            flags = " ".join(f.decode() if isinstance(f, bytes) else str(f)
                             for f in fd if not isinstance(f, tuple))
            raw = b""
            for part in fd:
                if isinstance(part, tuple):
                    raw = part[1]
                    break
            msg = email.message_from_bytes(raw)
            sender_name, sender_email = email.utils.parseaddr(dec(msg.get("From")))
            recips = [a for _, a in email.utils.getaddresses(
                [dec(msg.get(h, "")) for h in ("To", "Cc", "X-Original-To")]) if a]
            delivered = email.utils.parseaddr(msg.get("Delivered-To", ""))[1]
            try:
                ts = email.utils.mktime_tz(email.utils.parsedate_tz(msg.get("Date", "")))
            except Exception:
                ts = time.time()
            text = body_text(msg)
            with open(os.path.join(BODIES, str(uid)), "w") as fh:
                fh.write(text)
            con.execute("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uid, msg.get("Message-ID", "").strip(), ts,
                         time.strftime("%b %d, %Y %H:%M", time.localtime(ts)),
                         sender_name or sender_email, sender_email,
                         json.dumps(recips), delivered,
                         dec(msg.get("Subject")) or "(no subject)",
                         text[:180].replace("\n", " "),
                         lane_of(delivered, recips),
                         1 if "\\Seen" in flags else 0, 1))
            added += 1
        # refresh seen-flags on the newest window (reads elsewhere show up here)
        newest = [str(r[0]) for r in con.execute(
            "SELECT uid FROM messages ORDER BY ts DESC LIMIT ?", (FLAG_REFRESH,))]
        if newest:
            typ, fd = box.uid("FETCH", ",".join(newest), "(FLAGS)")
            if typ == "OK" and fd:
                for line in fd:
                    line = line.decode() if isinstance(line, bytes) else str(line)
                    m = re.search(r"UID (\d+)", line)
                    if m:
                        con.execute("UPDATE messages SET seen=? WHERE uid=?",
                                    (1 if "\\Seen" in line else 0, int(m.group(1))))
        con.commit()
        return {"ok": True, "new": added, "total":
                con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]}
    except imaplib.IMAP4.error as exc:
        return {"ok": False, "error": "IMAP: %s" % exc}
    finally:
        try:
            box.logout()
        except Exception:
            pass
        con.close()


# ------------------------------------------------------------------ queries
def list_messages(lane=None, q=None, limit=50, unseen=None):
    con = db()
    sql, args = "SELECT uid,date,sender,sender_email,subject,snippet,lane,seen,ts FROM messages", []
    where = []
    if lane and lane != "all":
        where.append("lane=?")
        args.append(lane)
    if q:
        where.append("(subject LIKE ? OR sender LIKE ? OR sender_email LIKE ? OR snippet LIKE ?)")
        args += ["%" + q + "%"] * 4
    if unseen:
        where.append("seen=0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    rows = [dict(zip(["id", "date", "sender", "sender_email", "subject",
                      "snippet", "lane", "seen", "ts"], r))
            for r in con.execute(sql, args)]
    con.close()
    return rows


def get_message(uid):
    con = db()
    r = con.execute("SELECT uid,date,sender,sender_email,recipients,delivered,"
                    "subject,lane,seen FROM messages WHERE uid=?", (int(uid),)).fetchone()
    con.close()
    if not r:
        return None
    meta = dict(zip(["id", "date", "sender", "sender_email", "recipients",
                     "delivered", "subject", "lane", "seen"], r))
    meta["recipients"] = json.loads(meta["recipients"] or "[]")
    try:
        meta["body"] = open(os.path.join(BODIES, str(uid))).read()
    except OSError:
        meta["body"] = ""
    return meta


def status():
    con = db()
    total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    unseen = con.execute("SELECT COUNT(*) FROM messages WHERE seen=0").fetchone()[0]
    lanes = dict(con.execute("SELECT lane, COUNT(*) FROM messages GROUP BY lane"))
    newest = con.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
    con.close()
    return {"ok": True, "total": total, "unseen": unseen, "lanes": lanes,
            # `lanes` above is {lane: count} and only covers lanes that have
            # mail; the UI needs the configured names so the tabs exist even
            # on an empty mailbox.
            "lane_names": sorted(LANES.keys()),
            "newest": newest, "creds": bool(secret(SECRET_NAME)),
            "login": LOGIN, "aliases": ALIASES,
            # The NAME of the keychain entry, never its value — the setup
            # note in the dashboard tells the member what to run.
            "secret_name": SECRET_NAME,
            "configured": config_error(CFG) is None}


# ------------------------------------------------------------------ send
def send(to, subject, body, sender=None, cc=None):
    err = config_error(CFG)
    if err:
        return {"ok": False, "error": err}
    pw = secret(SECRET_NAME)
    if not pw:
        return {"ok": False, "error": "no credentials — run: mapsec set " + SECRET_NAME}
    sender = sender or LOGIN
    if sender not in ALIASES:
        return {"ok": False, "error": "from must be one of %s" % ", ".join(ALIASES)}
    to_list = [a.strip() for a in (to if isinstance(to, list) else re.split(r"[,;]", to)) if a.strip()]
    if not to_list or not subject:
        return {"ok": False, "error": "need to + subject"}
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to_list)
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body or "")
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=30) as s:
            s.login(LOGIN, pw)
            s.send_message(msg)
    except Exception as exc:
        return {"ok": False, "error": "SMTP: %r" % exc}
    with open(os.path.join(MAILDIR, "sent.jsonl"), "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "from": sender, "to": to_list,
                             "cc": cc, "subject": subject, "body": body}) + "\n")
    return {"ok": True, "to": to_list}


def save_draft(to, subject, body, sender=None, note=None):
    d = {"ts": time.time(), "when": time.strftime("%b %d, %Y %H:%M"),
         "from": sender or LOGIN, "to": to, "subject": subject, "body": body,
         "note": note or "agent draft — review then send"}
    os.makedirs(MAILDIR, exist_ok=True)
    with open(os.path.join(MAILDIR, "drafts.jsonl"), "a") as fh:
        fh.write(json.dumps(d) + "\n")
    return {"ok": True, "draft": True}


def list_drafts():
    out = []
    try:
        with open(os.path.join(MAILDIR, "drafts.jsonl")) as fh:
            out = [json.loads(ln) for ln in fh if ln.strip()]
    except OSError:
        pass
    return out[::-1]


# ------------------------------------------------------------------ agent
# Lane names come from the member's own config, so the model is offered the
# lanes that actually exist rather than a list this file guessed at.
LANE_ENUM = ["all"] + sorted(LANES.keys()) + ["other"]

TOOLS = [
    {"type": "function", "function": {
        "name": "search_mail",
        "description": "Search the synced mailbox. Returns newest-first message summaries.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "text to match in subject/sender/snippet; empty for all"},
            "lane": {"type": "string", "enum": LANE_ENUM},
            "unseen_only": {"type": "boolean"},
            "limit": {"type": "integer", "default": 20}},
            "required": []}}},
    {"type": "function", "function": {
        "name": "read_mail",
        "description": "Read one message in full by its id (from search_mail results).",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer"}}, "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Send an email (or save a draft when sending is not allowed this run). "
                       "From must be one of the owner's addresses.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"},
            "from_address": {"type": "string", "enum": ALIASES}},
            "required": ["to", "subject", "body"]}}},
]

def agent_system(cfg=None):
    """Build the agent's system prompt from the configured identity.

    The addresses are injected rather than written in, for the same reason
    the constants moved: the prompt should describe the member's actual
    mailbox, and no mailbox should live in this file.
    """
    cfg = cfg or CFG
    login = cfg.get("login") or "the configured mailbox"
    others = [a for a in cfg.get("aliases", []) if a != cfg.get("login")]
    also = (" which also receives %s" % ", ".join(others)) if others else ""
    return (
        "You are the LifeOS mail agent running locally for the owner.\n"
        "You operate their one real mailbox (login %s)%s. Use the tools to\n"
        "search and read before answering; quote only what you actually read. "
        "Never\ninvent messages, senders, or amounts. Money/deposit questions: "
        "report exactly\nwhat the emails say and remind that only the bank "
        "confirms funds.\nWhen drafting or sending, write plainly and briefly "
        "in the owner's voice. If\nsend_email returns draft:true, tell the "
        "owner a draft was saved for their review." % (login, also)
    )


def agent_backend():
    tok = os.environ.get("DGX_API_TOKEN") or secret("DGX_API_TOKEN")
    if tok:
        return "https://ai.map.ca/v1", tok, os.environ.get("MAIL_AGENT_MODEL", "qwen3-32b")
    return "http://localhost:11434/v1", None, os.environ.get("MAIL_AGENT_MODEL", "qwen2.5:32b")


def run_agent(task, allow_send=False, max_steps=8):
    import urllib.request
    base, tok, model = agent_backend()
    messages = [{"role": "system", "content": agent_system()},
                {"role": "user", "content": task}]
    transcript = []
    for _ in range(max_steps):
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps({"model": model, "messages": messages,
                             "tools": TOOLS, "temperature": 0.2}).encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + tok} if tok else {})})
        try:
            resp = json.load(urllib.request.urlopen(req, timeout=180))
        except Exception as exc:
            return {"ok": False, "error": "model backend (%s): %r" % (base, exc),
                    "transcript": transcript}
        choice = resp["choices"][0]["message"]
        calls = choice.get("tool_calls") or []
        if not calls:
            return {"ok": True, "answer": (choice.get("content") or "").strip(),
                    "transcript": transcript}
        messages.append(choice)
        for call in calls:
            fn = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            if fn == "search_mail":
                result = list_messages(args.get("lane"), args.get("query"),
                                       args.get("limit", 20), args.get("unseen_only"))
            elif fn == "read_mail":
                result = get_message(args.get("id", 0)) or {"error": "no such message"}
            elif fn == "send_email":
                if allow_send:
                    result = send(args.get("to", ""), args.get("subject", ""),
                                  args.get("body", ""), args.get("from_address"))
                else:
                    result = save_draft(args.get("to", ""), args.get("subject", ""),
                                        args.get("body", ""), args.get("from_address"))
            else:
                result = {"error": "unknown tool"}
            transcript.append({"tool": fn, "args": args,
                               "result_preview": str(result)[:300]})
            messages.append({"role": "tool", "tool_call_id": call.get("id", fn),
                             "content": json.dumps(result)[:20000]})
    return {"ok": False, "error": "step limit reached", "transcript": transcript}


# ------------------------------------------------------------------ CLI
def main(argv):
    cmd = argv[0] if argv else "status"
    if cmd == "sync":
        print(json.dumps(sync(), indent=2))
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    elif cmd == "list":
        lane = argv[1] if len(argv) > 1 else None
        for m in list_messages(lane=lane, limit=25):
            print("%6s  %-6s %-18.18s  %-16s %s" %
                  (m["id"], "" if m["seen"] else "NEW", m["sender"], m["date"], m["subject"]))
    elif cmd == "read" and len(argv) > 1:
        m = get_message(argv[1])
        if not m:
            sys.exit("no such message")
        print("From: %s <%s>\nDate: %s\nSubject: %s\nLane: %s\n\n%s" %
              (m["sender"], m["sender_email"], m["date"], m["subject"], m["lane"], m["body"]))
    elif cmd == "send":
        import argparse
        p = argparse.ArgumentParser(prog="mail send")
        p.add_argument("--to", required=True)
        p.add_argument("--subject", required=True)
        p.add_argument("--from", dest="sender", default=None)
        p.add_argument("--body", default=None, help="omit to read body from stdin")
        a = p.parse_args(argv[1:])
        body = a.body if a.body is not None else sys.stdin.read()
        print(json.dumps(send(a.to, a.subject, body, a.sender), indent=2))
    elif cmd == "drafts":
        for d in list_drafts():
            print("%s  →%s  %s" % (d["when"], d["to"], d["subject"]))
    elif cmd == "agent":
        allow = "--allow-send" in argv
        task = " ".join(a for a in argv[1:] if a != "--allow-send")
        if not task:
            sys.exit('usage: mail agent [--allow-send] "task"')
        out = run_agent(task, allow_send=allow)
        if out.get("transcript"):
            for t in out["transcript"]:
                print("· %s %s" % (t["tool"], json.dumps(t["args"])), file=sys.stderr)
        print(out.get("answer") or json.dumps(out, indent=2))
    else:
        sys.exit("usage: mail sync|status|list [lane]|read ID|send|drafts|"
                 'agent [--allow-send] "task"')


if __name__ == "__main__":
    main(sys.argv[1:])
