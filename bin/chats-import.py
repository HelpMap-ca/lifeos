#!/usr/bin/env python3
"""
chats-import — own a local copy of your AI chat history.  (downloadable.ca)

Pulls conversations into ~/LifeOS/vault/chats/ as plain markdown, one file
per conversation, organized by source and date, with a master INDEX.md.
Plain text on your disk: greppable, backupable, yours. Nothing is uploaded.

Usage:
  python3 chats-import.py claude-code          # harvest ~/.claude/projects (local, automatic)
  python3 chats-import.py import EXPORT.zip    # claude.ai or ChatGPT data export (auto-detected)
  python3 chats-import.py index                # rebuild INDEX.md only

Getting the exports:
  claude.ai : Settings -> Privacy -> Export data  (link arrives by email)
  ChatGPT   : Settings -> Data controls -> Export data  (link arrives by email)

Stdlib only. Re-running is safe: files are rewritten in place by stable name.
"""
import json, os, re, sys, zipfile, tempfile
from datetime import datetime, timezone
from pathlib import Path

LIFEOS = Path(os.environ.get("LIFEOS_HOME", Path.home() / "LifeOS"))
CHATS = LIFEOS / "vault" / "chats"
CLAUDE_PROJECTS = Path(os.environ.get("CLAUDE_PROJECTS", Path.home() / ".claude" / "projects"))


def slug(text, n=48):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return (s[:n].rstrip("-")) or "untitled"


def day(ts):
    """ts: iso string or epoch float -> YYYY-MM-DD (or '' if unknown)."""
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if isinstance(ts, str) and ts:
            return ts[:10]
    except (ValueError, OSError):
        pass
    return ""


def write_conv(source, subdir, name, title, started, ended, msgs, meta=None):
    """msgs: list of (role, text, when). Returns Path or None if empty."""
    msgs = [(r, t.strip(), w) for r, t, w in msgs if t and t.strip()]
    if not msgs:
        return None
    d = CHATS / source / subdir if subdir else CHATS / source
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{started or 'undated'}-{name}.md"
    lines = ["---",
             f"title: {title or 'Untitled'}",
             f"source: {source}",
             f"started: {started or 'unknown'}",
             f"ended: {ended or 'unknown'}",
             f"messages: {len(msgs)}"]
    for k, v in (meta or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", f"# {title or 'Untitled'}", ""]
    for role, text, when in msgs:
        stamp = f" · {when}" if when else ""
        lines.append(f"## {role.capitalize()}{stamp}\n")
        lines.append(text)
        lines.append("")
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


# ---------- source: Claude Code (local ~/.claude/projects) ----------

def text_of(content):
    """Claude Code message content -> plain text (text blocks only)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    return ""

NOISE = re.compile(r"^\s*<(command-name|command-message|local-command-stdout|system-reminder)")

def harvest_claude_code():
    total, skipped = 0, 0
    for jl in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
        project = jl.parent.name.strip("-").replace("-Users-mapai", "home").replace("-", "/") or "home"
        title, msgs, started, ended, sid = None, [], None, None, jl.stem
        try:
            for line in jl.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") == "summary" and rec.get("summary"):
                    title = title or rec["summary"]
                    continue
                if rec.get("isMeta") or rec.get("type") not in ("user", "assistant"):
                    continue
                text = text_of(rec.get("message", {}).get("content"))
                if not text.strip() or NOISE.match(text):
                    continue
                when = day(rec.get("timestamp", ""))
                started, ended = started or when, when or ended
                msgs.append((rec["type"], text, when))
        except OSError:
            skipped += 1
            continue
        if not msgs:
            skipped += 1
            continue
        first_user = next((t for r, t, _ in msgs if r == "user"), "")
        title = title or first_user.replace("\n", " ")[:70]
        out = write_conv("claude-code", slug(project, 40), sid[:8], title,
                         started, ended, msgs, {"session": sid, "project": project})
        total += 1 if out else 0
    return total, skipped


# ---------- source: claude.ai / ChatGPT data-export zips ----------

def load_conversations(path):
    """Accept a .zip or a conversations.json; return (parsed list, label)."""
    p = Path(path)
    if p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            cand = [n for n in z.namelist() if n.endswith("conversations.json")]
            if not cand:
                sys.exit(f"no conversations.json inside {p}")
            data = json.loads(z.read(cand[0]).decode("utf-8", errors="replace"))
    else:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    return data


def import_claude_ai(convs):
    n = 0
    for c in convs:
        msgs = []
        for m in c.get("chat_messages", []):
            role = "user" if m.get("sender") == "human" else "assistant"
            text = m.get("text") or "\n\n".join(
                b.get("text", "") for b in m.get("content", [])
                if isinstance(b, dict) and b.get("type") == "text")
            msgs.append((role, text or "", day(m.get("created_at", ""))))
        started = day(c.get("created_at", ""))
        out = write_conv("claude-ai", started[:7] if started else "undated",
                         slug(c.get("name") or c.get("uuid", ""), 48),
                         c.get("name"), started, day(c.get("updated_at", "")), msgs,
                         {"uuid": c.get("uuid", "")})
        n += 1 if out else 0
    return n


def import_chatgpt(convs):
    n = 0
    for c in convs:
        mapping = c.get("mapping", {})
        # walk main thread: current_node -> parents; fallback to create_time sort
        chain = []
        node = c.get("current_node")
        seen = set()
        while node and node in mapping and node not in seen:
            seen.add(node)
            chain.append(mapping[node])
            node = mapping[node].get("parent")
        nodes = list(reversed(chain)) if chain else sorted(
            mapping.values(), key=lambda x: (x.get("message") or {}).get("create_time") or 0)
        msgs = []
        for nd in nodes:
            m = nd.get("message") or {}
            role = (m.get("author") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content") or {}
            if content.get("content_type") not in (None, "text"):
                continue
            text = "\n\n".join(p for p in content.get("parts", []) if isinstance(p, str))
            msgs.append((role, text, day(m.get("create_time"))))
        started = day(c.get("create_time"))
        out = write_conv("chatgpt", started[:7] if started else "undated",
                         slug(c.get("title") or c.get("id", ""), 48),
                         c.get("title"), started, day(c.get("update_time")), msgs,
                         {"id": c.get("id", "")})
        n += 1 if out else 0
    return n


def import_export(path):
    convs = load_conversations(path)
    if not isinstance(convs, list) or not convs:
        sys.exit("export file parsed but contains no conversations")
    if "chat_messages" in convs[0]:
        return import_claude_ai(convs), "claude-ai"
    if "mapping" in convs[0]:
        return import_chatgpt(convs), "chatgpt"
    sys.exit("unrecognized export format (expected claude.ai or ChatGPT conversations.json)")


# ---------- index ----------

def rebuild_index():
    rows = []
    for f in sorted(CHATS.rglob("*.md")):
        if f.name == "INDEX.md":
            continue
        head = f.read_text(encoding="utf-8", errors="replace")[:600].splitlines()
        get = lambda k: next((l.split(":", 1)[1].strip() for l in head if l.startswith(k + ":")), "")
        rows.append((get("source"), get("started"), get("title") or f.stem,
                     get("messages"), f.relative_to(CHATS)))
    out = ["# Chat archive index", "",
           f"_{len(rows)} conversations · rebuilt {datetime.now().strftime('%Y-%m-%d %H:%M')}_", ""]
    for source in sorted({r[0] for r in rows}):
        subset = sorted((r for r in rows if r[0] == source), key=lambda r: r[1], reverse=True)
        out += [f"## {source} ({len(subset)})", ""]
        out += [f"- {d or '????-??-??'} · [{t}]({p}) · {m} msgs" for _, d, t, m, p in subset]
        out.append("")
    (CHATS / "INDEX.md").write_text("\n".join(out), encoding="utf-8")
    return len(rows)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "claude-code":
        n, sk = harvest_claude_code()
        print(f"claude-code: {n} conversations imported ({sk} empty/skipped)")
        print(f"index: {rebuild_index()} total conversations")
    elif cmd == "import" and len(sys.argv) > 2:
        n, label = import_export(sys.argv[2])
        print(f"{label}: {n} conversations imported")
        print(f"index: {rebuild_index()} total conversations")
    elif cmd == "index":
        print(f"index: {rebuild_index()} total conversations")
    else:
        print(__doc__)
