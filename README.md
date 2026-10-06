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

## Sessions

A session is one conversation with the agent. **New session** (top left) gives the agent a clean working memory and
a clean activity feed; the report, its figures and every version carry over, and the agent is told the report already
exists. Earlier sessions stay in the Sessions menu and open read-only.

## Macro data and web search

If the `Macro and Context` folder is empty, the agent sources the country snapshot itself. Search uses **Tavily by
default and Brave as the automatic fallback** (`services/search.py`): when one reports its credit is used up it is skipped
for a cooldown and the other takes over, and the default returns by itself when its allowance renews. Either key alone
works. A figure from the web is only usable if it can be checked later: the agent must open the page (`web_fetch`, which
stores the page as it was read) and cite the exact sentence or table row containing the figure; the quotation is verified
against the stored page, exactly as workbook cells and PDF pages are. A figure seen only in a search result is refused.

## PDF conversion

Word to PDF runs in LibreOffice on the server, in LibreOffice as a service of its own (`chui-converter`, the production setup), or through iLoveAPI (`CHUI_PDF_CONVERTER=auto|libreoffice|remote|iloveapi`).
LibreOffice is the reference: private and font-exact, but it needs about 400 MB while converting. The hosted converter
needs no installation and suits a small host, with three caveats the code handles explicitly: the service lists a fixed
font set, so the brand font is embedded in a temporary copy sent for conversion (and `python -m
chui_reporter.render.check_converter` verifies the font survived); free accounts have a monthly file allowance, so
identical documents are never converted twice and a render needs one conversion when the contents page numbers are
unchanged; and the unfinished report leaves your server for the conversion.

**Tested result (6 Oct 2026): iLoveAPI converts the report but does not keep the brand font.** Larken's licence flag is
`fsType=4` (preview and print only) on every face, and the service will not use a font flagged that way; it does use an
embedded font with no restriction. The PDF comes back in Times New Roman, with different letter widths, so pagination and
the contents page numbers change too. Correctly typeset PDFs therefore need one of: LibreOffice on the server (fonts are
installed at run time from the synced Branding folder; roughly 400 MB while converting, so the free 512 MB instance is
marginal and the paid Starter instance is safe); or a Larken licence that permits embedding (`fsType` 0 or 8), after which
the hosted converter would work unchanged. The font files themselves are never modified.

## Quarters, sources and review notes

**Each quarter is a clean slate.** Choosing another quarter shows that quarter's own folder, files, report, notes and
conversation, and nothing of the previous one: the report area is blank until something is built, and the folder has to be
connected again (a folder belongs to one quarter; every sync says which, and is refused if the server has moved on). Only
things that do not change carry over: the brand kit (logos and fonts, stored once and never asked for again) and, on request,
the previous quarter's report (the agent reads it with `prior_quarter_report`; it also stands in for the baseline PDF, so the
new quarter never asks for it). Synced the wrong quarter? Sources has *Move to another quarter* and *Remove synced files*
(the brand kit is never touched). A folder that is empty is shown as connected and empty, not as "nothing chosen".

**What the user gives the agent is a source.** A figure typed in the conversation is accepted when the agent quotes the
user's own sentence (checked against what they wrote); figures in attached PDFs, workbooks, Word files, CSV and text are
verified in those files; a figure read from an attached image is accepted as the user's and flagged for their review, since
nothing can verify an image by machine. These are recorded as `provided` facts and license the figure like any verified one.

**Review notes can be closed.** Each note has *I have more information* (the next message is about that note; the agent uses it
and resolves the note) and *Resolve*. Resolved notes fold away and are not raised again; notes derived from the data (the macro
gaps) are rewritten from the ledger but never bring back one the user resolved.

**A message is a conversation first.** "Hello" gets an answer, not a render; only work that changes the report is held to the
render-and-inspect check. The agent's replies are stored and shown whole.

## Reliability

| Situation | What happens |
| --- | --- |
| Rate limit, dropped connection, provider 5xx | Retry from the last checkpoint with exponential backoff and jitter, honouring `Retry-After`. Visible in the activity feed. |
| Provider still down after the retry budget | Switches to `CHUI_FALLBACK_PROVIDER` if configured (e.g. DeepSeek → Anthropic); otherwise the run stops, resumable. |
| Bad API key, empty balance, rejected request | Stops at once with a plain explanation. The work is saved; a button continues it. |
| Conversation filling the model's context | Three layers, in the order the mature open-source agents use them. (1) Bulky work never enters the conversation: web research runs in subagents (below). (2) The model's real window is asked of the provider (`agent/limits.py`) and the provider's own token counts decide when to act: at half the window old tool results are replaced by a one-line stub (the agent can call the tool again), and only at 80% are old exchanges condensed into a brief, because rewriting old messages breaks the provider's prompt cache. (3) The agent's memory is the database (fact ledger, report, review notes), so nothing recorded is lost. The interface shows the real figures while the agent works. |
| The agent's saved memory growing | The agent saves a checkpoint per step, each with a full copy of the conversation, which grew to 327 MB. Only the newest checkpoints are ever needed to resume, so `runtime/retention.py` keeps the newest two per conversation (every ~20 steps and at the end of each run, always between steps), so a conversation costs about one copy. A distant safety net drops the state of the longest-idle conversations only if the total passes a budget (150 MB), never one that is running or parked. Nothing is deleted on a timer. `python -m chui_reporter.runtime.retention --apply` is the one-time clean-up for a database that has already grown. |
| Process crash or redeploy mid-run | The run's heartbeat goes stale; the next process re-queues it and resumes from the last checkpoint. |
| Model repeats itself | Loop guard interrupts with guidance; after repeated loops the run stops. |
| Model ends its turn early | A completion check (rendered? pages inspected?) sends it back to work. |
| User changes their mind mid-run | Steering and stop are applied at the next clean boundary (never between a tool call and its result). |

## Context management and subagents

The main agent builds the report; it does not do the reading. Anything that produces a lot of text (web search results, long
pages) is delegated to a **subagent**: a short-lived worker with its own empty context, its own tools and a budget (`agents/`).
It does its searching and reading, then returns one small structured result. Only that result enters the main conversation, so
the main agent's context stays small: in a live macro refresh the researchers used about 1M tokens (90% of it served from the
provider's cache) while the main conversation stayed around 15k.

* **Workers, not authors.** A researcher returns claims (a figure, the page it came from, the exact sentence containing it).
  Code, not the model, then re-reads the stored page and checks the sentence and the figure are really there
  (`services/web.verify_web_claim`); only verified figures are recorded in the ledger. Unverified claims become gaps.
  Figures for periods after the quarter end, and social media sources, are refused.
* **Macros.** `research_macro` runs one researcher per country in parallel (`CHUI_SUBAGENT_PARALLEL`, default 3); `build_macro_table`
  lays the table out from the ledger; the agent writes the prose from those figures only. `delegate_research` covers any other
  question that needs the web. The main agent has no web tools of its own.
* **Bounded and visible.** Each subagent has a step limit and a search/fetch budget; it retries transient failures with backoff; a stop
  from the user reaches it; its work appears nested in the activity feed and its tokens are counted separately in the header.
  `CHUI_SUBAGENT_MODEL` lets researchers run on a cheaper model than the main agent.
* **Messages arrive between steps.** The graph pauses before every model call; that is the only moment steering, loop warnings
  and stop take effect, so a saved conversation can never contain a tool call without its result.

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

Two free Render web services (`render.yaml`: the app and the agent, which runs inside it as a worker thread;
`render.converter.yaml`: LibreOffice as a service of its own), each in its own workspace. Keep each at a **single instance**. See `docs/DEPLOYMENT.md`.

## Layout

```
src/chui_reporter/
  agent/       tools, ledger, store, LangGraph graph, provider factory, real model limits, deterministic table builders
  agents/      subagent engine (isolated context, budgets, verified claims) and the macro researchers
  runtime/     run manager, narrator, retries, loop guard, compaction, report versions, UI state
  workspace/   folder sync (per quarter, plus the shared brand kit) and the source-slot registry
  render/      numeric gate, lint, branded Word renderer, LibreOffice conversion, charts
  extract/     source readers (workbooks, valuation reports, workpapers)
  app/         FastAPI app, auth, page images, static front end (vanilla ES modules)
```

## Known limits

* The File System Access API is Chromium-only; elsewhere the folder must be re-selected to look for changes.
* Macro figures come from the central banks' and statistics offices' own pages. Sites that omit an intermediate certificate
  are read the way a browser reads them (the chain is completed and still fully verified). Pages that sit behind a bot wall
  (ANSTAT, Côte d'Ivoire) or draw their figures with JavaScript (rate tables at the CBN, SARB and BCEAO) cannot be read
  without a real browser; those figures are left as dashes and listed as review notes rather than filled from a secondary source. Supplying the
  data in the `Macro and Context` folder always wins.
* The visual inspection by a vision model is good at gross layout and logo checks, unreliable on fine alignment.
* Qualitative claims in prose cannot be verified mechanically; only figures are.
