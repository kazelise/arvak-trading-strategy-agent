# arvak-trading-strategy-agent — Design Log

> Status: **M0a** · A standalone, analysis-first trading research agent.
> Every entry here is a decision I can defend out loud. If I can't explain a line, it doesn't ship.

## What this is

A personal trading research agent: fetch my subscribed sources, synthesize a
disciplined daily brief, journal every judgment so it can be scored later.
Analysis-first. Any future order flow stays human-approved — the agent never
executes.

## Decisions

**D1 — Standalone agent, not a coding-agent plugin.**
This system's center of gravity is *analysis*, not coding. It gets its own
identity, prompt discipline, and tool set. The agent loop arrives in M1
(conversation layer), following the architecture I already validated in my
previous agent project (Model protocol / ToolExecutor / RunContext / trace).
M0 needs no loop: fetch → clean → synthesize → journal is a straight line.

**D2 — Privacy model: source identities are config, not code.**
Real source names/URLs/channel IDs exist only in `config/sources.local.toml`
(gitignored). Code, docs, and commits refer to abstract ids (`newsletter-a`,
`research-b`, `sentiment-room`). Fetched content (`sources/`) and generated
briefs (`journal/`) never enter git — the inputs are paid-subscription
material, and derived artifacts are an attack surface (lesson carried over
from a real credential-leak incident in a previous project).

**D3 — Fetch via my real, logged-in browser session (`browser-use` CLI).**
Paid posts render fully in-session; RSS/feeds truncate them. No credentials
are stored or scripted — the browser session *is* the auth. Trade-off
accepted: fetching requires my desktop Chrome to be running. Politeness:
sequential fetches, fixed settle delay, small per-run caps.

**D4 — stdlib-only scripts + one shared output protocol.**
Python ≥3.11 (`tomllib`), zero pip dependencies. Every script prints
`{"ok": true, "data": …}` or `{"ok": false, "error": …, "hint": …}` to
stdout and human diagnostics to stderr, so scripts compose and an agent can
call them as tools later without adapters.

**D5 — Scale judgment: no retrieval index in v0.**
The daily corpus is a handful of documents; full text fits in context.
Chunking/embedding infrastructure is deferred until the corpus actually
outgrows the context window — not before.

**D6 — Everything lands as files (journaling discipline).**
Fetched posts → `sources/raw/<source-id>/YYYY-MM-DD-<slug>.md` with
front-matter (source, url, title, published, fetched_at, chars).
Runtime state is never the record; files are. Auditable, re-runnable,
diff-able.

## Milestones

- **M0a** (this): scaffold, privacy-first config, newsletter fetcher — *done*
- **M0b**: clean/normalize + disciplined daily brief (locked structure:
  per-claim source attribution; Bull/Base/Bear scenarios only, no point
  targets; no vague language) + journal write
- **M1**: multi-source synthesis + conversation loop over the brief
- **M2**: market-data tools (volatility, options positioning) + sentiment
  panel (incl. retail-FOMO contrarian gauge)
- **M3**: broker read-only integration (paper account first)
- **M4**: prediction journaling & scoring (make every call falsifiable)
- **M5**: execution staging behind an explicit human approval gate — never
  autonomous
