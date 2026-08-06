# LifeOS × map.ca — Integration Brief (Ground 0)

map.ca is being rebuilt from a clean repository — internally, **Ground 0**.
LifeOS is planned as a first-class part of that build, not a bolt-on. Your
job on this copy is twofold: make LifeOS itself solid, and **design and
prepare the integration surface** so it can slot into Ground 0 cleanly.

## The doctrine (non-negotiable)

1. **The vault never syncs.** map.ca must never receive vault content. Every
   integration is either browser-local (the site reads a file the user
   picked, in their own browser) or explicit opt-in push of a single record.
2. **Offline-first.** Every LifeOS feature works with zero network. Sync is
   an enhancement, never a requirement.
3. **Local models first.** Cheapest rung that fits the time budget
   (`models/registry.json`); frontier models are the escalation path, not
   the default.
4. **Verifiable distribution.** Anything users download ships with SHA-256 +
   readable source. LifeOS itself will be distributed through
   **downloadable.ca** under exactly that rule.

## Existing integration points (already in this code)

- **Profile connect** — map.ca's profile page reads
  `profile/profile.json` in-browser via a file picker. Nothing uploads.
  (`README.md` §Attach, `mapai-server.py` profile/pins endpoints.)
- **Pins** — `pins.json` mirrors the user's map.ca pins; live sync activates
  only when Supabase env is configured, and works one record at a time.
- **Reviews/feedback loop** — the dashboard harvests action items and writes
  feedback files to `vault/reviews/` (disk → UI, read-only mirror).
- **Chat harness** — local models act through named LEVERS (validated JSON
  actions); the model chooses, validated code executes. Keep this pattern
  for anything agent-facing.

## What to design and prepare (your deliverables)

1. **Integration design note** (`docs/` in your next release): how LifeOS
   attaches to the Ground 0 map.ca — profile connect, pins, and membership
   state — with a sequence diagram per flow and an explicit "what leaves the
   machine" table for each. That table is the review artifact; every flow
   must show `vault: nothing`.
2. **API contract sketch** — the minimal endpoint set Ground 0 must expose
   for LifeOS (auth handshake, pin push/pull, profile claim). Version it
   from day one (`/v1/`). Assume the site may run on Supabase; don't bind
   the contract to it.
3. **Packaging spec for downloadable.ca** — how a LifeOS release becomes a
   downloadable artifact: layout, checksum manifest, install script
   (`lifeos-setup.sh` is the seed), upgrade-in-place story.
4. **Member-app awareness** — a map.ca member app (Expo) is planned with
   photo→pin as its MVP. LifeOS should be ready to be the desktop
   counterpart: note where pin data structures need to stay compatible.
5. **Test plan** — how we prove, on every release, that offline mode is
   fully functional and that no request carries vault data (a localhost
   proxy log during a scripted session is acceptable evidence).

## Working assumptions you can rely on

- Ground 0 is a fresh public-repo build; code you write should be clean
  enough to open-source (licensing headers can come later, secrets never).
- The company runs its own local AI backend; LifeOS reaches it as "an
  OpenAI-compatible endpoint," never by hardcoded address
  (`LIFEOS_DGX_HOST` / node roster in the UI).
- Payments, email, and maps providers are being kept swappable across the
  stack. Don't introduce a hard dependency on any external SaaS.

## Priority order

1. Dashboard correctness and robustness (`app/lifeos.html` + server APIs)
2. Integration design note + API contract (deliverables 1–2)
3. Packaging spec (deliverable 3)
4. Test plan (deliverable 5)
5. Member-app compatibility notes (deliverable 4)

Questions go in the changelog entry or alongside the release — flag anything
where doctrine and practicality collide rather than silently picking one.
