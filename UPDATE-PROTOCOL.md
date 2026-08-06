# Update Protocol — how work flows back to the company

This copy of LifeOS is the **company development line**. When you (Robert)
send a build back and it passes the checks below, it is applied as an
**approved company update** to the production LifeOS. That approval carries
weight, so the protocol is strict and short.

## Roles

- **Robert** — contributor. Works on this copy, owns the changelog.
- **Mike** — owner/approver. Verifies, diffs, applies. Only Mike touches
  production.

## While you work

1. **Never put real data in code.** Your own test data goes in `vault/`,
   `inbox/`, `models/*.db` — those directories never ship (the release
   script excludes them). Seeds inside `app/lifeos.html` must stay generic.
2. **No credentials, anywhere, ever.** No API keys, passwords, tokens — not
   in code, not in comments, not in the changelog. The secrets register
   pattern (locations + rotation clocks, never values) is the model.
3. **No new network calls without a note.** LifeOS is offline-first. Any new
   outbound request must be documented in the changelog entry with what it
   sends and why. Anything that would upload vault content is an automatic
   reject.
4. **No new dependencies without a note.** The Python layer is stdlib-only
   on purpose. If something truly needs a dependency, argue for it in the
   changelog.
5. **Log every change** in `CHANGELOG.md`, under the `## Unreleased`
   heading at the top of the file — **do not pick a version number and do
   not touch `manifest.json`.** The version is assigned once, at release
   time (below).

   This changed on 2026-08-06. Every branch used to bump the version
   itself, which meant any two open at once collided on the same two files
   for the same reason — four of five consecutive PRs conflicted on
   `CHANGELOG.md` + `manifest.json`, never on anything real. Worse, the
   number a branch picked was a guess about merge order, so it went stale
   the moment another branch landed first.

   It also matches what a release actually is here: not what a branch
   claims, but what the approver applies.

## Cutting a release

One step, done once, when a set of merged work is ready to go out — not on
every branch:

1. Rename the `## Unreleased` heading in `CHANGELOG.md` to
   `## vX.Y.Z — YYYY-MM-DD — <one-line summary>`, and open a fresh empty
   `## Unreleased` above it.
2. Set the same `X.Y.Z` in `manifest.json` → `lifeos`. Patch for fixes,
   minor for features.
3. Then package it, below.

Because only this step touches the version, two branches in flight can no
longer disagree about what it is.

## Installing an update over an existing copy

The archive contains only code and documentation, so upgrading is an unzip
over `~/LifeOS`:

```bash
unzip -o LifeOS-vX.Y.Z.zip -d ~/LifeOS
~/LifeOS/bin/lifeos-setup.sh          # idempotent; fills any new layout pieces
shasum -c ~/LifeOS/CHECKSUMS.txt      # confirms the unpacked copy is intact
```

Nothing of the member's is at risk in that step, and the reason is structural
rather than careful sequencing: `vault/`, `inbox/`, `backups/`, `models/*.db`
and the real `profile/*.json` configuration files **are not in the archive**,
so there is nothing to overwrite them with. `lifeos-setup.sh` never replaces a
file that already exists.

**Going back** is the same operation with the previous archive. Because member
data was never in either one, a downgrade costs nothing but features.

## Sending a build back

```bash
~/LifeOS/bin/release.sh        # → LifeOS-vX.Y.Z.zip + its SHA-256
```

The script packages **code + docs only** (bin, app, tools, docs, templates)
and prints the archive's SHA-256. Send the zip, and send the SHA-256 through
a **different channel** (e.g. zip by file transfer, hash by chat message).

Include in the same message: version, one-paragraph summary, and anything
you want flagged for extra review.

## On the receiving side (Mike)

1. Verify the SHA-256 matches the one sent out-of-band.
2. Run the sweep: `grep -rn` for emails, IPs, keys — the package must be as
   clean as it went out.
3. Diff against the current line (`diff -r`), read the changelog, spot-run
   the dashboard from the unpacked folder (`MAPAI_WEB_ROOT=… mapai-server.py`).
4. If clean: apply to `~/LifeOS` + vault app files, tag the version in the
   changelog as **APPROVED — applied YYYY-MM-DD**. From that moment it *is*
   the company build.
5. If not clean: it goes back with notes. No partial applies.

## The one-line summary

Code travels in the zip; trust travels in the hash + the diff. Neither
substitutes for the other.
