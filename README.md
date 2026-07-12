# arvak-trading-strategy-agent

Personal trading research agent — fetches my subscribed sources, synthesizes a
disciplined daily brief, and journals every judgment so it can be scored later.
**Analysis-first, execution-never**: any future order flow stays behind an
explicit human approval gate.

## Layout

```
config/    sources.example.toml (template) · sources.local.toml (real, gitignored)
scripts/   stdlib-only Python tools; shared {"ok":…} stdout protocol
sources/   fetched raw material (gitignored — paid content stays local)
journal/   generated daily briefs (gitignored)
prompts/   prompt-as-data (daily brief, sentiment panel, …)
docs/      DESIGN.md · BRIEF_SPEC.md · SENTIMENT.md
tests/     stdlib unittest skeletons
```

## Quickstart

```bash
cp config/sources.example.toml config/sources.local.toml   # fill in your sources
python3 scripts/fetch_substack.py --smoke                  # connectivity check
python3 scripts/fetch_substack.py                          # fetch latest posts
python3 scripts/sentiment_panel.py --smoke                 # 宝妈指数 panel skeleton
python3 -m unittest tests/test_sentiment_panel.py -v
```

Requires Python ≥ 3.11 and [`browser-use`](https://github.com/browser-use/browser-use)
connected to your logged-in browser (`browser-use connect`).

## Privacy

Source identities never appear in code, docs, or commits — only abstract ids
(`newsletter-a`, `research-b`, …). See `docs/DESIGN.md` (D2).
