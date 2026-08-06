# LifeOS — Development Setup

Welcome. This is a **clean development copy** of LifeOS — the offline-first
"your data, your machine" operating layer that pairs with map.ca. It contains
all the product code and **zero personal data**: the vault directories are
empty, the profile is blank, the seeds are generic examples.

Read this file top to bottom before running anything.

## 1. What's in the box

| Path | What it is |
|------|-----------|
| `app/` | The dashboard itself. `lifeos.html` is the single-file cockpit (UI + local store + views). `m-bridge.py` bridges to a local Claude Code install, `comms.py` is the peer-comms service, `mapsec` is the machine-facing secret store CLI. In production these live **inside the encrypted vault volume**, not in a plain folder — see §3. |
| `bin/` | The CLI layer: `mapai` (vault open/close/status), `mapai-front` (launcher: vault → Ollama → web server → browser), `mapai-server.py` (localhost HTTP server + APIs on port **8765**), `rag`/`rag.py` (local hybrid retrieval), `mail`/`mailcore.py` (IMAP mail engine — needs your own account, see §6), `mapsec`, `backup.sh`, `lifeos-setup.sh`, importers (`chats-import.py`, `study-run.py`), `doc-extract` (House DNA extractor), `prompts` (build-card runner). |
| `tools/` | Swift photo/OCR tools (`mapshot`, `mapcolor`, `maphdr`, `mapmatte`, `ocr`) with sources, plus `house-dna.json` (property schema) and `TAXONOMY.md`. macOS-only; rebuild with `swiftc` if needed. |
| `vault/` | **Empty.** Your working data lives here (people, projects, money, chats…). Never ships. |
| `profile/` | Blank `profile.json` — the only file map.ca ever reads, and only in-browser when the user clicks Connect. |
| `models/` | `registry.json` — the model ladder. Cheapest rung that fits, escalate on failure. |
| `M.command` | Desktop double-click launcher shim → `bin/mapai-front`. |

## 2. Prerequisites

- macOS (Apple Silicon) is the reference platform. The Swift tools and the
  `hdiutil` vault are macOS-only; everything Python runs anywhere.
- Python 3.11+ (stdlib only — there is deliberately **no pip install step**).
- [Ollama](https://ollama.com) with at least one small model
  (`ollama pull llama3.1:8b`). The dashboard chat degrades gracefully without
  it, but you'll want it.
- Optional: Tailscale (the Nodes view understands tailnets), Claude Code
  (for `m-bridge.py`).

## 3. Local drives — the vault volume

Production LifeOS keeps the app + all deliverables inside an **encrypted
sparsebundle** that mounts at `/Volumes/MapAi`. Finder asks a human for the
password; scripts read it from the macOS Keychain. Create your own:

```bash
hdiutil create -size 10g -type SPARSEBUNDLE -fs APFS \
  -encryption AES-256 -volname MapAi ~/Desktop/MapAi.sparsebundle
# pick a STRONG passphrase — 8 chars falls to brute force in seconds
open ~/Desktop/MapAi.sparsebundle        # mounts at /Volumes/MapAi
cp -R ~/Desktop/LifeOS/app/* /Volumes/MapAi/
```

Then move the `LifeOS` folder to `~/LifeOS` (the scripts assume
`$HOME/LifeOS`):

```bash
mv ~/Desktop/LifeOS ~/LifeOS
```

`bin/mapai` handles `open` / `close` / `status` for the vault, and
`bin/mapai-chpass.exp` changes its password safely (read its header comment —
there's a real hdiutil footgun it exists to avoid).

**Shortcut for pure dev work:** you can skip the vault entirely and serve the
app folder directly:

```bash
MAPAI_WEB_ROOT=$HOME/LifeOS/app MAPAI_WEB_PORT=8765 python3 ~/LifeOS/bin/mapai-server.py
# then open http://localhost:8765/lifeos.html
```

It's served over http://localhost (not file://) because Ollama returns 403 to
a null origin.

## 4. Backups — second drive

Point `LIFEOS_BACKUP_TARGET` at an external drive and automate it:

```bash
export LIFEOS_BACKUP_TARGET=/Volumes/YourBackupDrive/LifeOS-backups
~/LifeOS/bin/backup.sh
crontab -e   # add:  0 9 * * *  $HOME/LifeOS/bin/backup.sh
```

Two places, on a schedule. This is doctrine, not a suggestion.

## 5. First run

```bash
~/LifeOS/bin/lifeos-setup.sh   # idempotent — fills any missing layout pieces
double-click M.command          # or: ~/LifeOS/bin/mapai-front
```

The launcher: unlocks the vault → starts Ollama → starts the web server on
:8765 → opens the dashboard. First load seeds a small generic dataset
(sample corporation, sample property, sample task) so every view has
something to render.

## 6. Things that are intentionally blank

- **Mail** (`mailcore.py`): no mailbox at all until you configure one. Copy
  `profile/mail.example.json` to `profile/mail.json` and fill in your host,
  login, aliases and lane needles; then store the app password in the
  keychain with `bin/mapsec set <the secret_name you chose>`. The password
  never goes in the config file — only the *name* of the keychain entry
  does. Unconfigured, mail says so plainly instead of half-working.
- **Nodes / network**: the DGX + NAS entries are generic placeholders. Env
  var `LIFEOS_DGX_HOST` names an optional GPU box.
- **Secrets register** (Vault → Keys view): one placeholder entry. The
  register holds *locations and rotation clocks only* — never values.
- **profile.json**: blank template.

## 6a. The local ports, and what guards them (v0.2.0)

LifeOS runs three loopback services. Binding `127.0.0.1` stops the network
reaching them, but it does **not** stop a web page you have open in your own
browser from calling them — so each carries its own guard:

| Port | Service           | Guard                                                                         |
| ---- | ----------------- | ----------------------------------------------------------------------------- |
| 8765 | `mapai-server.py` | `Host` + `Origin` validated; POST must be `application/json`; profile writes are schema-validated |
| 8787 | `m-bridge.py`     | Origin allow-list (the dashboard only) — never `*`                            |
| 8788 | `comms.py`        | Origin allow-list; loopback by default; any wider bind requires a token       |

Local processes with no `Origin` — the launcher, cron, `curl` — are
unaffected.

**What the comms token does and does not do.** The check is
"authenticated if not loopback", not "tailnet only": setting a token
satisfies it for *any* bind address, `0.0.0.0` included. If you set
`M_COMMS_BIND=0.0.0.0` with a token on a hotel LAN, every host on that
network can reach the relay and the token is all that stands in the way.
Bind the tailnet address specifically — that is where WireGuard, not the
token, is doing the work:

```bash
M_COMMS_BIND=100.x.y.z \
M_COMMS_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
python3 app/comms.py
```

**Where your records live.** People, projects, money, properties — the record
core — are stored in `vault/records.json`, alongside documents, mail and
chats. That means they are inside the encrypted volume, and inside what
`bin/backup.sh` copies.

The browser keeps a copy too, and that is deliberate: it is what lets the
dashboard open and keep working when the local server is not running. But it
is a cache, not the record. Clearing your browser data no longer costs you
anything — the next load reads the vault back.

Every save keeps one previous generation beside the file
(`records.json.bak`), and a file that cannot be read is restored from it
rather than starting over empty.

> Earlier versions kept the browser copy as the ONLY copy, and clearing site
> data destroyed it. If you are upgrading from one of those, the first load
> writes your existing records into the vault automatically — once, in the
> background, with a line in the browser console to say it happened.

## 6b. Connecting to map.ca (optional)

LifeOS works entirely offline. Connecting only adds one thing: a mirror of
the pins you have already **published** on map.ca, so they sit beside your
private ones on your own map.

Copy `profile/mapca.example.json` to `profile/mapca.json` and fill it in:

```json
{
  "contract": 1,
  "base_url": "https://<your-project>.supabase.co",
  "anon_key": "<the public anon key>",
  "handle": "<your map.ca handle>"
}
```

Those values are **public by design** — the anon key is the same one in
every visitor's browser bundle, and it reads only already-published pins
through the same guarded doors the website uses. LifeOS never holds a
session token or a service key, so this file is not a secret and
`mapca.json` is gitignored only because it is *yours*, not because it is
sensitive.

Then: Profile → **Sync from map.ca**. What that does, exactly:

- fetches your published pins (newest 500) and replaces the previous
  mirrored set;
- **never touches pins you made here** — `origin: "local"` records always
  survive;
- backs the file up first, so a bad write costs you nothing;
- writes one line to `profile/sync-log.jsonl` — a plain file you can
  `tail`, kept to the last 50 attempts, sent nowhere.

Delete `mapca.json` and you are back to pure offline, with your data intact.

**No config?** That is not an error. The dashboard says "Offline — your data
is complete", because it is.

> **Deprecated:** builds before 0.2.0 read these settings from a `~/map-ca`
> checkout's `.env.local`. That still works for one more release and logs a
> warning; move to `profile/mapca.json`.

## 7. The three rules (unchanged from production)

1. **Vault is offline.** Nothing in `vault/` is ever synced to map.ca.
2. **Back up on a schedule, to two places.**
3. **Only run tools you can verify** — checksum + readable source, every time.

Next: read `UPDATE-PROTOCOL.md` (how your work comes back to the company) and
`MAPCA-INTEGRATION.md` (what to build toward).
