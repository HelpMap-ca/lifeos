# LifeOS in this repository

LifeOS is the offline-first desktop companion to map.ca — the member's records live in an encrypted
vault on their own machine and never sync; their public profile card is the only bridge. It arrived
here on **2026-08-05** from its standalone line (`LifeOS v0.1.0`, prepared by And Can Did Inc.),
following the same path realmap.ca took: **built standalone, then ported in so it evolves with the
platform instead of drifting beside it.**

## Where things live

| Piece                    | Path                                   | What it is                                                                |
| ------------------------ | -------------------------------------- | ------------------------------------------------------------------------- |
| The desktop product      | `apps/lifeos/` (here)                  | Python + a single-file dashboard. Runs on the member's machine, not ours. |
| The map.ca-side surface  | `src/components/lifeos/`               | The Connect tile — ordinary Next.js code, no different from any tile.     |
| The card parser          | `src/lib/lifeos/schema.ts`             | Validates a picked `profile.json` (§15.2 — a file is third-party input).  |
| The decisions            | `docs/adr/0081-lifeos-integration.md`  | Why the bridge is a browser-local file read and nothing else.             |
| The planning line        | `docs/lifeos/exec-plan/`               | Phases 0–12: discovery through delivery.                                  |

`apps/lifeos/` follows the `apps/signaling/` precedent: a standalone service in this repo with its
own runtime and conventions, excluded from the root `tsconfig`. It is **not** part of the Next.js
build and ships in no bundle.

## What this is not

It is not a vertical surface like realmap.ca or artisanmap.ca. Those are Next.js surfaces built from
`src/` with their own build profile and domain. LifeOS is a program that runs on a member's own
computer — the only thing map.ca renders is the tile that reads a file the member picks.

## Running it

Python 3.11+, standard library only — **there is deliberately no `pip install` step**, and adding a
dependency requires a written argument (see `UPDATE-PROTOCOL.md`). macOS is the reference platform;
the Python data spine is portable, while the vault (`hdiutil`), the Keychain secret store, and the
Swift photo/OCR tools are macOS-only.

```bash
# Serve the dashboard without the encrypted vault (the dev shortcut):
MAPAI_WEB_ROOT=apps/lifeos/app MAPAI_WEB_PORT=8765 python3 apps/lifeos/bin/mapai-server.py
# then open http://localhost:8765/lifeos.html
```

Full setup, including the encrypted vault volume, is in `SETUP.md`.

## Tests

```bash
python3 apps/lifeos/tests/test_local_guards.py
```

Stdlib `unittest`, no fixtures beyond a temp directory. The guards are exercised over **real
sockets** rather than by calling handler methods, because what is being verified is what the port
answers to a hostile request.

CI runs them on every PR via the **LifeOS Local-Service Guards** job in
`.github/workflows/test.yml` — required, not advisory, because they assert a security boundary. Run
them locally too when you change anything under `bin/` or `app/`; the suite takes about six seconds
and needs no install step.

## Rebuilding the Swift tools

The compiled binaries are build artifacts and are **not committed** (see `.gitignore`); their
sources are. On macOS:

```bash
cd apps/lifeos/tools/mapshot && swiftc -O -o mapshot mapshot.swift
```

Repeat per tool (`mapcolor`, `maphdr`, `mapmatte`, `ocr`). CI cannot build these — they need Apple's
Vision and Core Image frameworks.

## Rules that still bind

These came with the product and did not stop applying when it moved:

1. **The vault never syncs.** No map.ca system receives vault content. `.gitignore` here refuses the
   vault directories outright; no code path exists to send them anywhere.
2. **Offline-first.** Every feature completes its job with zero network. Sync is an enhancement.
3. **No credentials, anywhere.** Not in code, not in comments, not in the changelog. LifeOS holds
   only public-by-design values (ADR 0081 §2) — never a map.ca session token.
4. **Verifiable distribution.** Anything members download ships with a SHA-256 and readable source.
   Distribution now runs through downloadable.ca's integrity pipeline (ADR 0073), which supersedes
   the standalone zip flow described in `UPDATE-PROTOCOL.md`; that file is kept for its review
   discipline and its history.

## Versioning

Do not bump `manifest.json` on a feature branch, and do not invent a version
number in the changelog — add your notes under `## Unreleased`. The version
is assigned once, when a release is cut (`UPDATE-PROTOCOL.md` §5 and
"Cutting a release").

The reason is mechanical: every LifeOS PR used to touch `CHANGELOG.md` and
`manifest.json`, so any two open at the same time conflicted on both, every
time, on nothing of substance. The number a branch chose was also a guess
about merge order, and went stale as soon as another branch landed first.

## Formatting

`apps/lifeos/` is in `.prettierignore`. The dashboard is a 227 KB single-file HTML application whose
inline formatting is load-bearing to read; Prettier would rewrite it wholesale and make every future
diff unreadable.
