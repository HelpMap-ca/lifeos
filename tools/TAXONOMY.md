# downloadable.ca tool library — taxonomy v0

_2026-07-30 · seed by Fable 5 · expand with OpenAI credits via `bin/taxonomy-expand.py`_

Every tool in the library is a **micro-tool**: one job, one file where possible,
readable source, SHA-256 checksum, no sudo, no telemetry, and a stated **rung** —
the cheapest model on the runner ladder that runs it well (see `models/registry.json`).
"Proven to work with the LLMs available" is a testable claim: each tool ships with a
`verify:` block the harness can run.

## Spec schema (what every tool entry must have)

```json
{
  "name": "", "category": "", "job": "one line",
  "inputs": [], "outputs": [],
  "min_rung": 1, "offline_ok": true,
  "verify": "command or check proving it works on the stated rung",
  "risk": "what could go wrong / misuse notes",
  "data_policy": "local | temporary | community-good"
}
```

## Categories

| # | Category | The job | Example micro-tools | Typical rung |
|---|----------|---------|---------------------|--------------|
| 1 | **Capture & Inbox** | Get things out of your head and devices into `inbox/` | voice-memo transcriber, screenshot OCR, clipboard collector, quick-note classifier | 1 |
| 2 | **Files & Folders** | Order out of disk chaos | download sorter, dedupe finder, rename normalizer, archive checksummer | 0–1 |
| 3 | **Own-Your-Data** | Pull your data home from platforms | **chats-import (shipped)**, social-archive importers, contact merger, bookmark consolidator, subscription auditor | 0–1 |
| 4 | **Text & Documents** | Transform words | summarizer, tone rewriter, template filler, PDF→markdown, minutes-from-transcript | 1–2 |
| 5 | **Knowledge & Study** | Turn archives into understanding | **study-run (shipped)**, flashcard generator, corpus indexer, citation checker | 2–3 |
| 6 | **Money & Ledger** | Honest books with evidence | receipt OCR→CSV, statement categorizer, invoice generator, e-transfer verifier (built, undeployed) | 1–2 |
| 7 | **People & Comms** | Relationships maintained | CRM record updater, follow-up drafter, intro writer, list hygiene | 1–2 |
| 8 | **Home & Property** | The physical world tracked | maintenance scheduler, warranty tracker, utility-bill parser, property doc filer | 1 |
| 9 | **Web & Research** | The web, on your terms | single-file page archiver, price watcher, RSS digester, open-data fetcher | 1–3 |
| 10 | **Media** | See/hear/say locally | photo tagger (local VLM), audio transcriber, TTS reader, video chapterizer | 2–3 |
| 11 | **Automation & Schedules** | Things that run without you | cron wrapper, backup verifier (restore-test!), uptime pinger, queue runner | 0–1 |
| 12 | **Safety & Verification** | Trust, earned mechanically | checksum verifier, secret scanner, permission auditor, sandbox runner | 0–1 |
| 13 | **Community Good** | Small files worth hosting centrally | accessibility checker, translation packs, local-info explainers (askmap.ca), civic open-data mirrors | 1–2 |
| 14 | **Harness & Meta** | The system that runs the tools | manifest validator, model router (runner ladder), eval harness, Arena handoff-readiness scorer | 2–4 |

_Rung 0 = no model needed (plain script). Rungs 1–4 per `models/registry.json`._

## Build loop (the demo that goes public)

1. **Spec** — generate candidate specs per category (OpenAI credits, prompts in `openai-prompts.md`)
2. **Rank** — value × feasibility on a small local model; pick the essential five
3. **Build** — rung 1–2 does the drafting; frontier (rung 4) reviews
4. **Verify** — run the tool's `verify:` block against each listed LLM on the DGX; record pass/fail per model
5. **Publish** — source + checksum + verify results to downloadable.ca
6. **Compensate** — outside builders whose tools pass verification get paid (see ops plan)

First five to build (essential, demonstrable, all rung ≤2):
`download-sorter` · `receipt-ocr-csv` · `page-archiver` · `flashcard-gen` · `backup-verifier`
