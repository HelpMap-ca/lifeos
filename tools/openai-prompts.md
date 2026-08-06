# OpenAI prompt kit — expanding the tool taxonomy

Copy-paste these into ChatGPT, or run them at scale with your API credits:
`OPENAI_API_KEY=sk-... python3 ~/LifeOS/bin/taxonomy-expand.py all`

Paste `TAXONOMY.md` after each prompt where it says {TAXONOMY}.

---

## P1 — Critique & gaps
> You are reviewing the category taxonomy for a public library of micro-tools that run on small LOCAL language models (8B–32B) on personal computers, offline-first, one job per tool. Here is the taxonomy: {TAXONOMY}
> 1) Name categories that are missing or wrongly merged. 2) Name categories that will attract low-value or unsafe submissions and how to constrain them. 3) Propose the minimal change list, not a rewrite.

## P2 — Specs per category (the workhorse — run per category)
> Generate 20 micro-tool specs for the category "{CATEGORY}" in this exact JSON schema, one array, no prose: {SPEC_SCHEMA}
> Constraints: each tool does ONE job; must run offline on an 8B–32B local model (state min_rung 0–4, 0 = no model); inputs/outputs are files or stdin/stdout; verify must be mechanically checkable; assume the user owns all data touched. Avoid tools that need accounts, cloud APIs, or scraping that violates terms.

## P3 — Rank for the build loop
> Here are candidate tool specs: {SPECS}. Score each 1–5 on (a) everyday value to a non-technical person, (b) feasibility on an 8B local model, (c) demo power for an open-source launch. Return a table sorted by a×b×c and name the five I should build first as a coherent "essential kit".

## P4 — Adversarial pass (run before anything ships)
> For each of these tool specs: {SPECS} — describe 1) the worst realistic failure on a weak local model, 2) how a malicious variant of this tool would abuse the user's trust, 3) the one-line mitigation the library's harness should enforce. Be specific; this gates publication.

## P5 — Taxonomy → curriculum
> Given this taxonomy {TAXONOMY} and the fact that our harness can batch-run an idle 32B model overnight: design a "study syllabus" where the local model practices each category against synthetic fixtures and reports a per-category competence score. Output as a JSON list of (category, fixture_description, pass_criterion).
