# LifeOS Changelog

Newest first. Every release gets an entry: version, date, author, changes,
any new network calls or dependencies (with justification). The approver
tags applied releases with **APPROVED — applied YYYY-MM-DD**.

**Working on a branch?** Add your notes under `## Unreleased` and leave the
version alone — `manifest.json` is set once, when a release is cut. See
`UPDATE-PROTOCOL.md` §5. (Before 2026-08-06 each branch bumped the version
itself, and any two open at once collided on this file and that one.)

---

## Unreleased

### The record core is durable (M6)

People, projects, money, properties — everything the member actually keeps
here — lived only in the browser's `localStorage`. Clearing site data, or the
browser evicting it under storage pressure, took all of it. Those records
exist nowhere else, and no backup covered them because nothing was ever
written to disk. LifeOS's own founding notes put dashboard robustness first
for this reason.

- **Records now live in `vault/records.json`** — zone Z1, never synced, and
  inside what `bin/backup.sh` already copies. Served by new
  `GET`/`POST /api/records` endpoints.
- **The browser copy is kept, deliberately, as a cache.** Offline-first is
  the product: the dashboard still opens and still saves with the local
  server stopped. What changed is that the browser is no longer the only
  copy.
- **Existing installs migrate themselves.** The first load after upgrading
  writes the browser's records into the vault once, in the background, and
  says so in the console.
- **Every save keeps one previous generation**, and a `records.json` that
  cannot be read is restored from it rather than silently starting empty —
  the same protection the pins mirror already had.
- **Saves are coalesced** (a short debounce) and flushed on page close, so a
  burst of edits is one write rather than several, and a close with an edit
  pending is not lost.
- **The stored shape is checked, but the content is not narrowed.** These are
  the member's own records; dropping a field this build did not recognise
  would destroy data to satisfy a schema. Structure and size are validated,
  and nothing else is touched.

Verification: `python3 -m unittest discover -s tests -t tests` — **87 tests**,
14 of them new here: round-trip, replace-not-merge, unrecognised fields
preserved, corrupt-file restore from backup, corrupt-with-no-backup starting
empty rather than erroring, the record cap, and a refused save leaving the
stored core untouched.

A parse check for the dashboard's inline script was also added on the map.ca
side (`tests/unit/lib/lifeos/dashboardScript.test.ts`). That file is excluded
from Prettier, absent from the build, and had no automated check of any kind
— a syntax error in it would blank the page with nothing to catch it. One was
in fact introduced while writing this change and caught by that check before
it went anywhere.

- New network calls: **none that leave the machine** — the dashboard now
  talks to the local server it is already served from.
- New dependencies: **none**.

### Releases are checked before they are built (M4a)

ADR 0073 requires anything published through downloadable.ca to ship with a
checksum and readable source, and this repository carries a known credential
leak in its git history — so a LifeOS release has to be a *checked* clean
export rather than an assumed one.

- **`bin/release-scan.py`** refuses to package a release that carries a
  secret: private key blocks, provider token formats, and a secret-ish name
  assigned a real-looking value. The update protocol already asked the
  approver to grep the package on arrival; by then it has been built and
  sent. This runs first and stops.
- **The scan is narrow on purpose.** A scanner that cries wolf gets switched
  off, and a switched-off scanner is worse than none. Documentation that
  names a keychain entry, an address at a reserved example domain, a shell
  variable pass-through and a URL being assembled are all quiet — the last
  two because they were the first two false alarms it produced against the
  real tree, and both are now pinned by tests.
- **`CHECKSUMS.txt` ships inside the archive.** The whole-archive hash proves
  the download arrived intact; per-file checksums let someone verify an
  unpacked copy afterwards, which is what readable source is worth.
- **The shipping list is named once** and reused by the scan, the checksums
  and the archive, so the three can never cover different sets of files.
- **`UPDATE-PROTOCOL.md` documents installing over an existing copy.** It is
  safe for a structural reason rather than a careful one: member data is not
  in the archive, so there is nothing to overwrite it with.

Verification: `python3 -m unittest discover -s tests -t tests` — **85 tests**,
12 new here, covering both halves — that real credentials are caught, and
that the lookalikes stay quiet.

- New network calls: **none**. New dependencies: **none**.
- Distribution follow-up now resolved: LifeOS is licensed **Apache 2.0**
  (`LICENSE` + `NOTICE`, Copyright 2026 And Can Did Inc.), and the license
  ships with the release. (The stale MIT claim in the root README is tracked
  separately.)

## v0.3.3 — 2026-08-06 — the profile endpoint validates what it writes (H5)

`POST /api/profile` accepted any object with a `name` key and wrote it to
disk verbatim. That file is the one thing map.ca reads when a member clicks
Connect, so anything reaching the endpoint could put arbitrary keys and
unbounded strings into it.

H1 (v0.2.0) already stops a web page reaching the endpoint at all. This is
the second layer: even a caller that gets through cannot leave a malformed
card behind.

- **Schema-2 validation on write.** Fields are copied one at a time into a
  fresh object — never the request dict — so no invented key survives to
  disk. Strings are trimmed and capped, wrong-typed values become empty
  rather than propagating, and a `profile_schema` newer than this build
  understands is refused rather than guessed at.
- **Unknown keys are dropped, not rejected** (a newer LifeOS may send
  fields this build predates) but they are **named in the response**, so a
  caller can see exactly what did not survive instead of wondering.
- **The caps are pinned to the reader.** They mirror
  `src/lib/lifeos/schema.ts`, and a cross-language test compares the two
  tables field by field. Writing a card this side accepts but the Connect
  tile refuses is the failure worth preventing — it surfaces at the very
  end of the journey, as "not a LifeOS profile card" about the member's own
  profile.

Verification: `python3 -m unittest discover -s tests -t tests` — **71 tests**
across the suite, all passing; 15 are new here. Confirmed meaningful: change
one cap on either side and the cross-language test names the mismatch
(`writer and reader disagree (ts, python): {'bio': (500, 400)}`).

- New network calls: **none**. New dependencies: **none**.
- Behavior change to disclose: a request carrying keys outside schema 2 now
  has them dropped rather than written. Nothing the dashboard sends is
  affected — it posts the card it read from this same endpoint.

## v0.3.2 — 2026-08-06 — a fresh install's profile is readable

`bin/lifeos-setup.sh` seeded `profile/profile.json` without a
`profile_schema` field, and with a field set that predated schema 2
(`interests`, no `pronouns`/`tags`/`contact`/`business`/`tabs`). map.ca's
Connect tile uses that field to tell a LifeOS card from any other JSON, so
it refused the file outright: install LifeOS, run setup, click **Connect**,
and be told your own freshly-created card is "not a LifeOS profile card".

The installer now seeds the same schema-2 shape the shipped template uses.

Verification: a new contract test —
`tests/unit/lib/lifeos/setupSeedContract.test.ts` — extracts the JSON the
shell script writes and runs it through the tile's **real** parser, plus
asserts the installer and the template declare identical fields. Confirmed
meaningful: all three assertions fail against the previous seed.

- New network calls: **none**. New dependencies: **none**.
- Existing installs are unaffected: setup never overwrites a file that
  exists. A member who already ran the old version can add
  `"profile_schema": 2` to their profile, or copy the shipped template.

## v0.3.1 — 2026-08-06 — mail identity becomes configuration (H4)

`mailcore.py` carried the IMAP login, the alias list and the lane needles as
module constants. That cost twice over: every install shipped with someone
else's example addresses baked in and quietly synced nothing until a person
edited the source, and keeping a real mailbox out of the tree became a
recurring chore — the placeholder was rewritten in place once already to
satisfy a hygiene scan.

- **`profile/mail.json` holds the identity** (`profile/mail.example.json` is
  the template): host, login, aliases, lane needles, and the *name* of the
  keychain entry to read. The app password still lives only in the OS
  keychain via `mapsec` — never in the config, never in the tree.
- **There is no longer an email address anywhere in `mailcore.py`**, and a
  test asserts it stays that way. Nothing to leak, nothing to scrub.
- **The agent's system prompt and its lane list are built from the config**,
  so a local model is told about the mailbox that actually exists rather
  than one this file guessed at.
- **Unconfigured mail says so.** `sync()` and `send()` return a plain "mail
  is not configured: … missing from profile/mail.json" instead of failing
  somewhere deeper against a stranger's address.
- **The dashboard's setup note follows suit** — it now points at
  `mail.example.json` and names the member's own keychain entry, rather than
  printing example addresses as if they were theirs.
- **Forgetting to list yourself in `aliases` no longer stops you sending as
  yourself**: the login is always a valid sender.

Verification: `python3 -m unittest discover -s tests -t tests` — **56 tests**
across the whole LifeOS suite, all passing. Sixteen are new here: the config
matrix (malformed JSON, wrong-typed values, a root-level array), the
missing-field message, the agent prompt with and without config, the guard
that no address remains in the module, and three source-level guards on the
dashboard — the setup note's identifier scope, the handler no longer
swallowing programming errors, and no hardcoded mailbox in the HTML.

- New network calls: **none**. New dependencies: **none**.
- Behavior change to disclose: an install that relied on the baked-in
  constants now reports "not configured" until `profile/mail.json` exists.
  Since those constants were example addresses, such an install was never
  actually reaching a mailbox.

## v0.3.0 — 2026-08-06 — the map.ca connection becomes configuration

The pins mirror worked only on a machine that happened to have the map.ca
website checked out: the Supabase URL and anon key were scraped from
`~/map-ca/.env.local`. That was an accident of the author's laptop, not a
design, and it is why nobody else could sync.

- **`profile/mapca.json` is the connection** (`profile/mapca.example.json`
  is the template). It pins `contract: 1` — the version of the map.ca read
  contract this build speaks — so a breaking change on either side produces
  a clear message instead of an empty mirror. The values are public by
  design; LifeOS still holds no session token and no service key.
  The old env scrape survives one release as a deprecated fallback that
  logs a warning.
- **A sync can no longer cost you local pins.** The mirror is backed up
  before every write (one generation, beside the file), and a `pins.json`
  that fails to parse is restored from that backup instead of silently
  resetting to empty — which previously would have discarded every
  `origin: "local"` record, the ones that exist nowhere else.
- **One malformed remote record no longer fails the whole sync.** Rows are
  mapped individually, bad ones are counted and reported, the rest land.
- **`profile/sync-log.jsonl`** records the last 50 attempts — trigger,
  outcome, counts, duration — as plain JSONL a member can `tail`. It is
  local-first diagnostics: indexed by nothing, transmitted nowhere.
- **Timestamps carry an explicit UTC offset** (H6). The previous naive
  local time was ambiguous the moment a member travelled, and unsortable
  across zones.
- **A renamed handle now says so.** "No profile for @x" is followed by "you
  synced as this handle before — did you rename it?" when a previous sync
  is on record, because that is a thing map.ca supports and the fix differs.

Verification: `python3 -m unittest discover -s tests -t tests` — **37 tests**
(19 guards + 18 new), all passing. The new suite stubs the HTTP layer, so it
proves the merge, recovery and verdict logic rather than Supabase's
behaviour: config matrix including an unsupported contract, both coordinate
spellings, malformed-row rejection, backup/restore of a truncated mirror,
ring-trimming of the log, and an offline sync leaving the mirror
**byte-identical**.

- New network calls: **none** — same two reads, from a documented config
  file instead of a scraped one.
- New dependencies: **none**.
- Behavior change to disclose: installs relying on the `~/map-ca/.env.local`
  scrape keep working but now log a deprecation warning.

## v0.2.0 — 2026-08-05 — local-service hardening (H1–H3)

Closes the three findings from the integration threat model
(`docs/lifeos/exec-plan/80-security-design.md` §2–3) that block distribution.
All three protect people who never use the map.ca integration at all.

- **H1 · `bin/mapai-server.py` is local-only in fact, not just by bind.**
  Binding 127.0.0.1 keeps the LAN out but not the owner's own browser: a
  cross-origin `text/plain` POST carrying JSON is a CORS "simple request",
  so it skipped the preflight entirely and could overwrite `profile.json`
  or drive the photo tools. The Host header is now validated (closing DNS
  rebinding), a foreign `Origin` is refused, and every POST must declare
  `application/json` — which forces any remaining foreign caller into a
  preflight that the Origin rule then rejects. Callers with no Origin
  (the launcher, cron, curl) are unaffected.
- **H2 · `app/m-bridge.py` no longer answers `Access-Control-Allow-Origin: *`.**
  That wildcard let any page the owner had open run prompts on their Claude
  subscription and read the replies. The dashboard's origin is echoed back
  explicitly; everything else gets 403 before a byte of the body is read.
- **H3 · `app/comms.py` binds loopback by default and refuses a tokenless
  public bind.** The old default (`0.0.0.0`, empty token) published the
  message and file relay to whatever network the machine was on. Binding
  beyond loopback now requires `M_COMMS_TOKEN` or the service exits with
  instructions.
- **H3b · `app/comms.py` also refuses foreign browser origins.** Found in
  review of the first pass: fixing the bind left `Access-Control-Allow-Origin: *`
  in place, and the token — the only other guard — is empty in the default
  loopback configuration. So any page the owner had open could `POST /send`
  into the relay and read `/events`, which streams message text and file
  names: the same cross-origin shape as H1 and H2, on the third port. The
  relay now echoes an allowed origin with `Vary: Origin` and refuses others
  **before** the token check, so a tokenless default install is not what
  stands between a web page and the relay.

Verification: `python3 tests/test_local_guards.py` — 19 acceptance tests
driving real sockets (not handler calls), covering the foreign origin, the
preflight-free `text/plain` and form-encoded bodies, the rebound Host, the
wildcard on both bridge and relay, the relay's send/read paths, and the
tokenless public bind. Confirmed meaningful by reverting each service in
turn: 12 of the original 14 fail without the H1/H2/H3 guards, and 3 of the
5 new relay tests fail without H3b. The ones that stay green are the
assertions that legitimate local traffic keeps working — they should pass
either way, which is the evidence the guards do not over-block.

- New network calls: **none** — three surfaces now *refuse* calls they
  previously accepted.
- New dependencies: **none** (stdlib only, as always).
- Behavior change to disclose: anything that talked to these ports from a
  browser page other than the dashboard, or ran `comms.py` on `0.0.0.0`
  without a token, stops working by design.

## v0.1.0 — 2026-08-04 — baseline (And Can Did Inc.)

Clean development copy prepared for Robert Loterh.

- Full product code: dashboard (`app/lifeos.html` + companion services),
  CLI layer (`bin/`), Swift photo/OCR tools with sources (`tools/`).
- All personal data removed: empty vault, blank profile, generic seeds,
  placeholder node roster / camera list / secrets register, no credentials.
- Docs added: `SETUP.md`, `UPDATE-PROTOCOL.md`, `MAPCA-INTEGRATION.md`.
- New network calls: none. New dependencies: none.
