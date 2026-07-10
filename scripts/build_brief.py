#!/usr/bin/env python3
"""Assemble a daily brief from fetched sources and call the model (M0b).

Design notes (docs/DESIGN.md):
- stdlib only. Config = config/model.local.toml (gitignored, D8).
- Output protocol (shared by all scripts in this repo):
  stdout → {"ok": true, "data": ...} | {"ok": false, "error": ..., "hint": ...}
  stderr → human-readable progress diagnostics.
- Reads sources/raw/<source-id>/*.md (gitignored fetched content, D6) and
  writes journal/<date>-briefing.md (gitignored generated artifact, D2).
- Sources are referred to only by their config `id` — never by real name
  (D2 privacy model). This script never prints or hardcodes a publication
  name; whatever appears inside a fetched document is just data.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SOURCES_RAW = ROOT / "sources" / "raw"
JOURNAL_DIR = ROOT / "journal"
DEBUG_DIR = JOURNAL_DIR / ".debug"
PROMPT_FILE = ROOT / "prompts" / "daily_brief.md"
MODEL_CONFIG = ROOT / "config" / "model.local.toml"

NY_TZ = ZoneInfo("America/New_York")

FALLBACK_SYSTEM_PROMPT = (
    "PLACEHOLDER SYSTEM PROMPT — replace with prompts/daily_brief.md"
)

API_VERSION = "2023-06-01"
REQUEST_TIMEOUT = 180

# Generic Substack-style UI chrome that leaks into extracted article text.
# Deliberately generic (no publication names) — see D2 privacy model.
JUNK_LINE_RE = re.compile(
    r"^(?:"
    r"Share this post|Share|Subscribed|Subscribe now|Subscribe|"
    r"Thanks for reading.*|Read full story|Discussion about this post|"
    r"Leave a comment|Comments?|Restacks?|Copy link|"
    r"Facebook|Twitter|Email|Notes|More|Previous|Next|"
    r"Like|Reply|Give a gift subscription|Upgrade to paid"
    r")$",
    re.IGNORECASE,
)
EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ─────────────────────────── collection ───────────────────────────

def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a `---\\nkey: value\\n---\\nbody` file into (meta, body).

    Hand-rolled (no yaml dependency, D4). Values are taken verbatim after
    the first ':'; a wrapping pair of double quotes is stripped so titles
    like `"07/06 recap"` come through clean. Malformed/missing front
    matter just yields an empty meta dict and the original text as body.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    meta: dict[str, str] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        meta[key.strip()] = value
    return meta, body


def collect_documents(days: int, source_filter: str | None) -> list[dict]:
    """Scan sources/raw/*/*.md, keep files published within the last `days`."""
    files = sorted(SOURCES_RAW.glob("*/*.md"))
    if source_filter:
        files = [f for f in files if f.parent.name == source_filter]

    cutoff = date.today().toordinal() - days
    docs = []
    for path in files:
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        published = meta.get("published", "")
        try:
            pub_ordinal = date.fromisoformat(published[:10]).toordinal()
        except ValueError:
            log(f"[skip] {path}: unparseable published date {published!r}")
            continue
        if pub_ordinal < cutoff:
            continue
        docs.append({
            "source": meta.get("source", path.parent.name),
            "title": meta.get("title", ""),
            "published": published,
            "url": meta.get("url", ""),
            "body": body,
            "path": path,
        })
    docs.sort(key=lambda d: (d["published"], d["source"]))
    return docs


# ─────────────────────────── cleaning ───────────────────────────

def clean(text: str) -> str:
    """Strip generic Substack UI chrome; keep everything else (conservative).

    Only drops lines that are *exactly* (whitespace-trimmed) a known UI
    residue pattern — never touches prose. Collapses 3+ blank lines to 2.
    When in doubt, keep the line.
    """
    lines = text.split("\n")
    kept = [ln for ln in lines if not JUNK_LINE_RE.match(ln.strip())]
    collapsed = EXCESS_BLANK_LINES_RE.sub("\n\n", "\n".join(kept))
    return collapsed.strip()


# ─────────────────────────── session / previous brief ───────────────────────────

def current_session(now: datetime) -> str:
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return "closed"
    minutes = now.hour * 60 + now.minute
    if minutes < 9 * 60 + 30:
        return "premarket"
    if minutes < 16 * 60:
        return "intraday"
    return "postmarket"


PREV_SECTION_RE = re.compile(
    r"^##\s*(一句话结论|情景推演)\s*$",
    re.MULTILINE,
)


def extract_previous_sections(text: str) -> str | None:
    """Pull '## 一句话结论' and '## 情景推演' sections out of a prior brief.

    A "section" runs from its heading to the next '## ' heading (or EOF).
    Returns None if neither section is found.
    """
    matches = list(PREV_SECTION_RE.finditer(text))
    if not matches:
        return None
    chunks = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # Trim to the next top-level heading if one exists inside the tail.
        tail = text[m.end():end]
        next_heading = re.search(r"^##\s+\S", tail, re.MULTILINE)
        if next_heading:
            end = m.end() + next_heading.start()
        chunks.append(text[start:end].strip())
    return "\n\n".join(chunks) if chunks else None


def find_previous_brief() -> tuple[str, str] | None:
    """Return (date, extracted_sections) for the most recent prior briefing, if any."""
    if not JOURNAL_DIR.exists():
        return None
    candidates = sorted(JOURNAL_DIR.glob("*-briefing*.md"))
    if not candidates:
        return None
    latest = candidates[-1]
    text = latest.read_text(encoding="utf-8")
    sections = extract_previous_sections(text)
    if not sections:
        return None
    # date prefix is the leading YYYY-MM-DD of the filename
    file_date = latest.name[:10]
    return file_date, sections


# ─────────────────────────── prompt assembly ───────────────────────────

def load_system_prompt() -> str:
    if PROMPT_FILE.exists():
        return PROMPT_FILE.read_text(encoding="utf-8")
    log(f"[warn] {PROMPT_FILE} not found — using placeholder system prompt")
    return FALLBACK_SYSTEM_PROMPT


def build_user_message(docs: list[dict], now_ny: datetime) -> tuple[str, bool]:
    session = current_session(now_ny)
    parts = [f"date: {now_ny.date().isoformat()}", f"session: {session}"]

    prev = find_previous_brief()
    has_previous = prev is not None
    if prev:
        prev_date, prev_sections = prev
        parts.append(f'<previous-brief date="{prev_date}">\n{prev_sections}\n</previous-brief>')

    for d in docs:
        cleaned = clean(d["body"])
        parts.append(
            f'<document source="{d["source"]}" title="{d["title"]}" '
            f'published="{d["published"]}" url="{d["url"]}">\n{cleaned}\n</document>'
        )

    return "\n\n".join(parts), has_previous


# ─────────────────────────── model call ───────────────────────────

def load_model_config() -> dict:
    if not MODEL_CONFIG.exists():
        raise RuntimeError(
            "missing config/model.local.toml — copy config/model.example.toml and fill it in"
        )
    cfg = tomllib.loads(MODEL_CONFIG.read_text())
    model_cfg = cfg.get("model", {})
    if not model_cfg.get("base_url") or not model_cfg.get("model"):
        raise RuntimeError("config/model.local.toml is missing base_url or model under [model]")
    return model_cfg


def resolve_api_key(model_cfg: dict) -> str:
    import os

    if model_cfg.get("api_key"):
        return model_cfg["api_key"]
    env_name = model_cfg.get("api_key_env")
    if env_name:
        val = os.environ.get(env_name)
        if val:
            return val
        raise RuntimeError(f"env var {env_name!r} (api_key_env) is not set")
    raise RuntimeError("config/model.local.toml [model] needs api_key or api_key_env")


def call_model(system: str, user: str, model_cfg: dict) -> str:
    api_key = resolve_api_key(model_cfg)
    base_url = model_cfg["base_url"].rstrip("/")
    body = json.dumps({
        "model": model_cfg["model"],
        "max_tokens": model_cfg.get("max_tokens", 4000),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=body,
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise RuntimeError(f"model call failed: HTTP {exc.code} — {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"model call failed: {exc.reason}") from exc

    try:
        return payload["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected model response shape: {json.dumps(payload)[:200]}") from exc


# ─────────────────────────── journal write ───────────────────────────

def write_journal(ny_date: str, content: str) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    path = JOURNAL_DIR / f"{ny_date}-briefing.md"
    n = 2
    while path.exists():
        path = JOURNAL_DIR / f"{ny_date}-briefing-{n}.md"
        n += 1
    path.write_text(content, encoding="utf-8")
    return path


def write_debug_prompt(ny_date: str, system: str, user: str) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_DIR / f"{ny_date}-prompt.md"
    n = 2
    while path.exists():
        path = DEBUG_DIR / f"{ny_date}-prompt-{n}.md"
        n += 1
    dump = f"# SYSTEM\n\n{system}\n\n# USER\n\n{user}\n"
    path.write_text(dump, encoding="utf-8")
    return path


# ─────────────────────────── main ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the daily brief from fetched sources and call the model"
    )
    ap.add_argument("--days", type=int, default=7, help="lookback window in days (default 7)")
    ap.add_argument("--source", help="only this source id")
    ap.add_argument("--dry-run", action="store_true", help="assemble prompt, skip the model call")
    args = ap.parse_args()

    try:
        now_ny = datetime.now(NY_TZ)
        ny_date = now_ny.date().isoformat()

        docs = collect_documents(args.days, args.source)
        if not docs:
            raise RuntimeError(
                f"no source files found in sources/raw/ within the last {args.days} day(s)"
                + (f" for source {args.source!r}" if args.source else "")
            )
        log(f"[collect] {len(docs)} document(s) within {args.days}d"
            + (f" (source={args.source})" if args.source else ""))

        system = load_system_prompt()
        user, has_previous = build_user_message(docs, now_ny)
        log(f"[assemble] system={len(system)} chars, user={len(user)} chars, "
            f"previous_brief={has_previous}")

        if args.dry_run:
            debug_path = write_debug_prompt(ny_date, system, user)
            log(f"[dry-run] wrote {debug_path.relative_to(ROOT)}")
            print(json.dumps({
                "ok": True,
                "data": {
                    "system_chars": len(system),
                    "user_chars": len(user),
                    "documents": len(docs),
                    "previous_brief": has_previous,
                    "debug_file": str(debug_path.relative_to(ROOT)),
                },
            }, ensure_ascii=False, indent=2))
            return 0

        model_cfg = load_model_config()
        log(f"[model] calling {model_cfg['model']} at {model_cfg['base_url']}…")
        brief_text = call_model(system, user, model_cfg)

        journal_path = write_journal(ny_date, brief_text)
        log(f"[journal] wrote {journal_path.relative_to(ROOT)}")
        print(json.dumps({
            "ok": True,
            "data": {
                "journal_file": str(journal_path.relative_to(ROOT)),
                "documents": len(docs),
                "previous_brief": has_previous,
                "chars": len(brief_text),
            },
        }, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:  # noqa: BLE001 — single exit point, structured error
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "hint": "Run scripts/fetch_substack.py first if no sources are found; "
                    "or cp config/model.example.toml config/model.local.toml and fill it in.",
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
