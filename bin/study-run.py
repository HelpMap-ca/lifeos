#!/usr/bin/env python3
"""
study-run — downtime learning. Feeds new vault material to an idle model
(DGX rung 3 by default) and writes digests into vault/knowledge/digests/.

This is the v0 of "the DGX studies the archive when it isn't doing evals":
each run picks up chats added/changed since the last run, asks the model for
a structured digest (facts, decisions, open loops, people, tools mentioned),
and files it. Local models get a curriculum out of your own history.

Usage:
  MAPAI_VPC_API_TOKEN=...  python3 study-run.py            # DGX qwen3-32b via ai.map.ca
  STUDY_BASE=http://localhost:11434/v1 STUDY_MODEL=qwen2.5:32b python3 study-run.py   # fully local

State: models/study-state.json tracks what's been studied. Re-runs are incremental.
Stdlib only.
"""
import json, os, sys, urllib.request
from datetime import datetime
from pathlib import Path

LIFEOS = Path(os.environ.get("LIFEOS_HOME", Path.home() / "LifeOS"))
CHATS = LIFEOS / "vault" / "chats"
DIGESTS = LIFEOS / "vault" / "knowledge" / "digests"
STATE_F = LIFEOS / "models" / "study-state.json"

BASE = os.environ.get("STUDY_BASE", "https://ai.map.ca/v1").rstrip("/")
MODEL = os.environ.get("STUDY_MODEL", "qwen3-32b")
TOKEN = os.environ.get("MAPAI_VPC_API_TOKEN", os.environ.get("STUDY_TOKEN", ""))
BATCH_CHARS = 24000  # keep prompts modest for 32B-class models

PROMPT = """You are the archivist for a private local knowledge base.
Digest the following conversation transcripts. Return markdown with sections:
## Facts worth keeping (bullet list, concrete, dated where possible)
## Decisions made
## Open loops (things started but unfinished)
## People & entities mentioned
## Tools/processes described
Be terse. Skip pleasantries and dead ends. Transcripts follow:

"""


def chat(text):
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps({"model": MODEL, "temperature": 0.2,
                         "messages": [{"role": "user", "content": PROMPT + text}]}).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def main():
    state = json.loads(STATE_F.read_text()) if STATE_F.exists() else {"studied": {}}
    fresh = []
    for f in sorted(CHATS.rglob("*.md")):
        if f.name == "INDEX.md":
            continue
        key, mtime = str(f.relative_to(CHATS)), f.stat().st_mtime
        if state["studied"].get(key) != mtime:
            fresh.append((key, mtime, f))
    if not fresh:
        print("study-run: nothing new to study.")
        return
    print(f"study-run: {len(fresh)} new/changed conversations -> {MODEL} @ {BASE}")
    DIGESTS.mkdir(parents=True, exist_ok=True)
    batch, size, digests, done = [], 0, [], []
    def flush():
        nonlocal batch, size
        if not batch:
            return
        digests.append(chat("\n\n---\n\n".join(t for _, t in batch)))
        done.extend(k for k, _ in batch)
        print(f"  digested {len(batch)} conversations")
        batch, size = [], 0
    for key, mtime, f in fresh:
        text = f.read_text(encoding="utf-8", errors="replace")[:BATCH_CHARS]
        if size + len(text) > BATCH_CHARS:
            flush()
        batch.append((key, text)); size += len(text)
    flush()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    out = DIGESTS / f"{stamp}-study.md"
    out.write_text(f"# Study digest · {stamp} · {MODEL}\n\n" + "\n\n---\n\n".join(digests),
                   encoding="utf-8")
    for key, mtime, _ in fresh:
        state["studied"][key] = mtime
    STATE_F.write_text(json.dumps(state, indent=1))
    print(f"study-run: wrote {out}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        sys.exit(f"study-run: endpoint unreachable ({e}). Is the DGX up / token set? "
                 f"Fully local fallback: STUDY_BASE=http://localhost:11434/v1 STUDY_MODEL=qwen2.5:32b")
