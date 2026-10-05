# Chui Ventures reporter

An agent that builds the quarterly LP report for Chui Ventures Fund I from the documents in a folder,
keeps it up to date as those documents change, and renders it to a branded Word file and PDF.

Nothing in the report is typed by the model without being checked: every figure must trace to a cell or
page in a source document (or be computed by code from figures that do), and a deterministic gate refuses
to render anything else.

> **Confidentiality.** This repository holds code only. The fund's source documents, the brand fonts and
> every rendered report stay out of it (see `.gitignore`). Do not commit them.

## How it works in production

```
 your computer                          server (Render)                         Neon Postgres
 ┌───────────────────┐  changed files   ┌──────────────────────────────┐        ┌──────────────────┐
 │ the quarter's     │ ───────────────▶ │ FastAPI app                  │ ─────▶ │ source files     │
 │ folder            │  (File System    │  ├ workspace sync            │        │ fact ledger      │
 │ (Chrome / Edge)   │   Access API)    │  ├ run manager (worker thread)│        │ report content   │
 └───────────────────┘                  │  │   └ LangGraph ReAct agent │        │ agent checkpoints│
        ▲                               │  ├ numeric gate + renderer   │        │ run events       │
        │   live activity, questions,   │  │   (python-docx + LibreOffice)       │ report versions  │
        └─────────────────────────────  │  └ SSE event stream          │        └──────────────────┘
              the report, page by page  └──────────────────────────────┘
```

1. **The user keeps working the way they always have**: documents go in a local folder. They point the
   app at it once (File System Access API; Chrome and Edge remember the folder). The browser re-scans it every
   few seconds, hashes what changed and uploads only that. Safari and Firefox fall back to a one-off folder
   selection.
2. **The server mirrors the folder** into Postgres and knows, for each kind of source, whether it has it
   (`workspace/slots.py`): what is in, what is missing, and which report sections each one feeds.
3. **A run starts by itself** when the required sources are present and something changed (or on one button
   press). The agent works from a snapshot of the files taken when the run starts.
4. **The agent narrates as it works.** Each tool call becomes an event (`runtime/narrator.py`) that the
   interface replays, grouped into chapters. The text comes from what the agent *did*, not from what the
   model says it did.
5. **It asks when it needs a person.** `ask_user` (a judgement the data cannot settle) and `request_sources`
   (missing documents) pause the run through LangGraph's `interrupt`. The answer, or the arrival of the files,
   resumes it from the exact same point, even after a restart. Questions are capped per run.
6. **One input box talks to the agent.** It grows as you type, Enter sends, files can be attached (button, drag and
   drop, or paste; PDF, Excel, Word, CSV, text, images). While the agent works, a message is steering: it is queued
   and delivered at the next step, and the send button becomes Stop when the box is empty. When the agent has asked
   a question, the box is the answer. Attached files are stored under `Uploads/` and survive folder syncs.
7. **Every render becomes a numbered version** (PDF, Word, review notes) with the sections that changed, so
   the interface can show what an update touched. Any version can be opened beside the latest, its changed pages
   are marked, and "Changes" shows the wording that changed (kept from the version after this feature shipped).

## Reliability

| Situation | What happens |
| --- | --- |
| Rate limit, dropped connection, provider 5xx | Retry from the last checkpoint with exponential backoff and jitter, honouring `Retry-After`. Visible in the activity feed. |
| Provider still down after the retry budget | Switches to `CHUI_FALLBACK_PROVIDER` if configured (e.g. DeepSeek → Anthropic); otherwise the run stops, resumable. |
| Bad API key, empty balance, rejected request | Stops at once with a plain explanation. The work is saved; a button continues it. |
| Conversation too long | `pre_model_hook` condenses old tool output and old exchanges. The agent's memory is the database (fact ledger, report, review notes), so nothing is lost. |
| Process crash or redeploy mid-run | The run's heartbeat goes stale; the next process re-queues it and resumes from the last checkpoint. |
| Model repeats itself | Loop guard interrupts with guidance; after repeated loops the run stops. |
| Model ends its turn early | A completion check (rendered? pages inspected?) sends it back to work. |
| User changes their mind mid-run | Steering and stop are applied at the next clean boundary (never between a tool call and its result). |

## Safeguards on the numbers

* **Ledger.** Figures enter only by deterministic extraction, by a *verified* claim (the cited cell or page is re-read
  and must contain the number), or by *derivation* (code evaluates arithmetic over grounded figures).
* **Gate.** Every numeral in prose, tables and charts is checked by exact set membership at the precision the text
  shows. It fails closed. (`render/gate.py`)
* **No meta-commentary in the report.** Notes about the data go to the review notes, never into the document.
  (`render/lint.py`)
* **Visual check.** After rendering, the agent inspects the pages with a vision model; its limits are documented in
  the code, and layout is also made deterministic (exact row heights, page-flow estimation).

## Running it locally

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), LibreOffice, a Neon (or any Postgres) database.

```bash
cp .env.example .env            # fill in DATABASE_URL and a provider key
uv sync --extra dev
CHUI_ENV=dev uv run python -m chui_reporter.app      # http://localhost:8000, no password in dev mode
```

`CHUI_ENV=dev` disables the password and exposes `/api/dev/*`, which lets the interface sync a *virtual* folder
(`/?mock=1`) so the real sync path can be exercised without the native folder picker. Never set it in production.

The brand fonts (Larken) are licensed and are not in the repository. They are installed from the `Branding` folder you
sync (`render/fonts.py`); locally they can also simply be installed into your font library.

Command-line agent (no web app): `./chui "Build the Q2 2026 report"`.

## Tests

```bash
uv run pytest -m "not slow"     # fast: gate, ledger logic, runtime, recovery, store (needs DATABASE_URL)
uv run pytest                   # includes LibreOffice rendering
```

Tests that read the real source documents or contain their figures live in `tests/private/` and are not published.

## Configuration

See `.env.example`. The essentials: `DATABASE_URL`, `CHUI_ACCESS_PASSWORD`, one of `DEEPSEEK_API_KEY` /
`ANTHROPIC_API_KEY`, and optionally `CHUI_FALLBACK_PROVIDER`.

## Deploying

`render.yaml` describes one Docker web service (the agent runs inside it as a worker thread). Keep it at a **single
instance**. See `docs/DEPLOY-HANDOFF.md`.

## Layout

```
src/chui_reporter/
  agent/       tools, ledger, store, LangGraph graph, provider factory, deterministic table builders
  runtime/     run manager, narrator, retries, loop guard, compaction, report versions, UI state
  workspace/   folder sync and the source-slot registry
  render/      numeric gate, lint, branded Word renderer, LibreOffice conversion, charts
  extract/     source readers (workbooks, valuation reports, workpapers)
  app/         FastAPI app, auth, page images, static front end (vanilla ES modules)
```

## Known limits

* The File System Access API is Chromium-only; elsewhere the folder must be re-selected to look for changes.
* Macro indicators (country snapshot) are not collected automatically yet; the section is left out unless the
  data is supplied in the `Macro and Context` folder.
* The visual inspection by a vision model is good at gross layout and logo checks, unreliable on fine alignment.
* Qualitative claims in prose cannot be verified mechanically; only figures are.
