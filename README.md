# LifeOS — your data, your machine

## Why this is open

LifeOS runs **entirely on your own machine**. Your records live in a local vault
and are never synced to any server — no account, no cloud, no third party. It is
the part of map.ca that keeps working when everything else doesn't: if a hosted
service has a bad day, LifeOS is still yours and still online, on hardware you own.

We publish it to demonstrate something specific — that a civic platform can be
**operated from Canada, on infrastructure its community controls.** map.ca's public
surface still leans on hosted services today (a CDN, a managed database); that is
the honest state, and it is on a deliberate path to being independently operable so
that a temporary outage anywhere can never take the mission offline. This open
release is the invitation to help finish that path — with participation, review,
and resources.

**If it isn't local, it isn't private.**

---

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

## License

LifeOS is open source under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Copyright 2026 And Can Did Inc. You may use, modify, and redistribute it under those terms, which include an explicit patent grant.
