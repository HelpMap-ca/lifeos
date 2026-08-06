#!/usr/bin/env bash
# ============================================================
#  LifeOS local setup — downloadable.ca
#  Creates your private, offline LifeOS home directory.
#
#  What this script does (and ALL it does):
#    1. Creates ~/LifeOS with a standard directory layout
#    2. Writes a profile template (the only file map.ca ever reads,
#       and only when YOU attach it — in your browser, locally)
#    3. Installs a local backup script + shows you how to automate it
#
#  What it never does:
#    - No sudo. No network calls. No telemetry. Nothing uploaded.
#    - Never overwrites a file that already exists.
#
#  Verify before running (good practice — we teach this):
#    shasum -a 256 lifeos-setup.sh   # compare with the hash on downloadable.ca
#    less lifeos-setup.sh            # read it. It's short on purpose.
# ============================================================
set -euo pipefail

LIFEOS_HOME="${LIFEOS_HOME:-$HOME/LifeOS}"
VERSION="0.1.0"

say()  { printf '  %s\n' "$*"; }
made() { printf '  + %s\n' "$*"; }
kept() { printf '  = %s (already exists, untouched)\n' "$*"; }

# Write stdin to $1 only if it doesn't exist.
put() {
  local f="$1"
  if [ -e "$f" ]; then kept "$f"; cat > /dev/null; else cat > "$f"; made "$f"; fi
}

echo
echo "LifeOS setup v$VERSION → $LIFEOS_HOME"
echo

# ---- 1. Directory layout -----------------------------------
for d in \
  profile \
  vault/people vault/projects vault/money vault/properties vault/business vault/knowledge \
  vault/chats/claude-code vault/chats/claude-ai vault/chats/chatgpt \
  tools \
  models \
  inbox \
  backups \
  bin
do
  if [ -d "$LIFEOS_HOME/$d" ]; then kept "$LIFEOS_HOME/$d/"; else mkdir -p "$LIFEOS_HOME/$d"; made "$LIFEOS_HOME/$d/"; fi
done

# ---- 2. Manifest -------------------------------------------
put "$LIFEOS_HOME/manifest.json" <<EOF
{
  "lifeos": "$VERSION",
  "source": "downloadable.ca",
  "installed": "$(date +%Y-%m-%d)",
  "machine": "$(uname -s) $(uname -m)",
  "layout": ["profile", "vault", "tools", "models", "inbox", "backups", "bin"]
}
EOF

# ---- 3. Profile template -----------------------------------
# This is the ONLY file the map.ca profile page reads — and it reads it
# in your browser, from your disk, when you click "Connect". It is never
# uploaded. Fill in only what you're happy to show on your public profile.
#
# `profile_schema` is not decoration: map.ca's reader uses it to tell a
# LifeOS card from any other JSON, and refuses a file without it. Keep this
# block identical to profile/profile.json — tests/unit/lib/lifeos/
# setupSeedContract.test.ts runs the real parser over what is written here
# and fails if the two drift apart.
put "$LIFEOS_HOME/profile/profile.json" <<'EOF'
{
  "profile_schema": 2,
  "public": true,
  "draft": true,
  "name": "",
  "handle": "",
  "pronouns": "",
  "city": "",
  "bio": "",
  "pillars": [],
  "tags": [],
  "avatar": "",
  "intro_video": "",
  "member_since": "",
  "links": [],
  "contact": { "email": "", "phone": "" },
  "business": { "name": "", "line": "" },
  "tabs": ["profile", "business", "pins", "contact"]
}
EOF

# ---- 4. README (the rules of the house) --------------------
put "$LIFEOS_HOME/README.md" <<'EOF'
# LifeOS — your data, your machine

| Directory   | What lives here                                   | Leaves this machine?      |
|-------------|---------------------------------------------------|---------------------------|
| `profile/`  | The small public card map.ca can display          | Only on-screen, never uploaded |
| `vault/`    | Your real databases: people, projects, money…     | **Never**                 |
| `tools/`    | Tools downloaded from downloadable.ca             | No (checksummed on the way in) |
| `models/`   | Registry of local LLMs (Ollama etc.)              | No                        |
| `inbox/`    | Temporary exchange area — treat as disposable     | Assume yes — keep nothing precious here |
| `backups/`  | Local snapshots made by `bin/backup.sh`           | Your call (external drive recommended) |

## The three rules
1. **Vault is offline.** Nothing in `vault/` is ever synced to map.ca.
2. **Back up on a schedule, to two places.** Run `bin/backup.sh` — then automate it (see below) and point `LIFEOS_BACKUP_TARGET` at an external drive.
3. **Only run tools you can verify.** Every tool on downloadable.ca ships with a SHA-256 checksum and readable source. Check both. Every time.

## Automate your backup (macOS / Linux)
    crontab -e
    # add:  0 9 * * *  $HOME/LifeOS/bin/backup.sh

## Attach to map.ca
Open your map.ca profile → "Connect your LifeOS profile" → pick
`~/LifeOS/profile/profile.json`. The page fills in locally, in your
browser. Close the tab and it's gone from the site.
EOF

# ---- 5. Model registry: the runner ladder ------------------
# Cheapest-adequate rule: for any job with a known time budget, pick the
# LOWEST rung whose speed fits the window. Escalate only on failure.
put "$LIFEOS_HOME/models/registry.json" <<'EOF'
{
  "runtime": "ollama",
  "endpoint": "http://localhost:11434/v1",
  "rule": "known time budget -> cheapest rung that fits the window; escalate only on failure",
  "ladder": [
    { "rung": 1, "name": "llama3.1:8b",  "where": "this Mac (Ollama)",  "cost": "free",         "use": "fast classify/extract/route" },
    { "rung": 2, "name": "qwen2.5:32b",  "where": "this Mac (Ollama)",  "cost": "free",         "use": "daily driver, drafting, summaries" },
    { "rung": 3, "name": "qwen3-32b",    "where": "DGX via ai.map.ca",  "cost": "power only",   "use": "batch/overnight jobs, evals, study runs" },
    { "rung": 4, "name": "claude (bridge)", "where": "m-bridge :8787",  "cost": "subscription", "use": "frontier reasoning, training-task supervision" }
  ],
  "models": []
}
EOF

# ---- 6. Backup script --------------------------------------
put "$LIFEOS_HOME/bin/backup.sh" <<'EOF'
#!/usr/bin/env bash
# LifeOS backup — snapshots profile/ + vault/ + models/ into backups/,
# keeps the newest 14, and mirrors to LIFEOS_BACKUP_TARGET if set
# (point that at an external drive or NAS — two places, always).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$ROOT/backups/lifeos-$STAMP.tar.gz"
tar -czf "$OUT" -C "$ROOT" profile vault models manifest.json
echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"
ls -t "$ROOT/backups"/lifeos-*.tar.gz 2>/dev/null | tail -n +15 | xargs rm -f 2>/dev/null || true
if [ -n "${LIFEOS_BACKUP_TARGET:-}" ] && [ -d "$LIFEOS_BACKUP_TARGET" ]; then
  cp "$OUT" "$LIFEOS_BACKUP_TARGET/" && echo "mirrored: $LIFEOS_BACKUP_TARGET/$(basename "$OUT")"
fi
EOF
chmod +x "$LIFEOS_HOME/bin/backup.sh"

# ---- Done --------------------------------------------------
echo
say "Done. Read the rules of the house:  $LIFEOS_HOME/README.md"
say "First backup:                       $LIFEOS_HOME/bin/backup.sh"
say "Your vault never leaves this machine. That's the whole point."
echo
