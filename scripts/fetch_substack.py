#!/usr/bin/env python3
"""Fetch latest posts from configured newsletter sources (M0a).

Design notes (docs/DESIGN.md):
- Uses the user's real, logged-in browser session via the `browser-use` CLI
  (subprocess), so paid content they already subscribe to renders fully.
  No credentials are stored anywhere in this repo.
- stdlib only. Config = config/sources.local.toml (gitignored).
- Output protocol (shared by all scripts in this repo):
  stdout → {"ok": true, "data": ...} | {"ok": false, "error": ..., "hint": ...}
  stderr → human-readable progress diagnostics.
- Fetched content lands in sources/raw/<source-id>/ (gitignored: paid material
  and derived artifacts never enter git).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "sources.local.toml"
RAW_DIR = ROOT / "sources" / "raw"

MIN_POST_CHARS = 900        # below this → likely an index/section page or preview
PAGE_SETTLE_SECONDS = 2.5   # politeness + let client-side rendering settle

JS_COLLECT_LINKS = (
    "JSON.stringify(Array.from(document.querySelectorAll('a[href*=\"/p/\"]'))"
    ".map(a=>a.href.split('?')[0]).filter((v,i,s)=>s.indexOf(v)===i))"
)

JS_EXTRACT_POST = (
    "(()=>{const m=p=>document.querySelector(`meta[property=\"${p}\"]`)?.content||'';"
    "let pub=m('article:published_time');"
    "if(!pub){try{const ld=JSON.parse(document.querySelector('script[type=\"application/ld+json\"]')?.textContent||'{}');"
    "pub=ld.datePublished||''}catch(e){}}"
    "const el=document.querySelector('article .available-content')||document.querySelector('article');"
    "const t=el?el.innerText:'';"
    "return JSON.stringify({title:m('og:title')||document.querySelector('h1')?.innerText||'',"
    "published:pub,chars:t.length,body:t})})()"
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def bu(*args: str, timeout: int = 90) -> str:
    """Run a browser-use CLI command and return stdout (raises on failure)."""
    proc = subprocess.run(
        ["browser-use", *args], capture_output=True, text=True, timeout=timeout
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"browser-use {args[0]} failed: {detail[:300]}")
    return proc.stdout


def bu_eval(js: str):
    out = bu("eval", js)
    marker = "result: "
    idx = out.find(marker)
    if idx == -1:
        raise RuntimeError(f"unexpected eval output: {out[:200]!r}")
    return json.loads(out[idx + len(marker):].strip())


def connect() -> None:
    out = bu("connect")
    if "connected" not in out:
        raise RuntimeError(f"connect failed: {out.strip()[:200]}")
    log("[browser] connected to real Chrome session")


def load_sources() -> list[dict]:
    if not CONFIG.exists():
        raise RuntimeError(
            "missing config/sources.local.toml — copy config/sources.example.toml and fill it in"
        )
    cfg = tomllib.loads(CONFIG.read_text())
    return [s for s in cfg.get("newsletters", []) if s.get("enabled")]


def slug_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1][:80]


def looks_like_index(post: dict) -> bool:
    """Section/collection pages list many teasers instead of one article."""
    return post["chars"] < MIN_POST_CHARS and post["body"].count("Read full story") >= 2


def fetch_source(src: dict, latest: int) -> list[dict]:
    sid, base = src["id"], src["url"].rstrip("/")
    out_dir = RAW_DIR / sid
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"[{sid}] opening archive…")
    bu("open", f"{base}/archive")
    time.sleep(PAGE_SETTLE_SECONDS)
    links = [u for u in bu_eval(JS_COLLECT_LINKS) if u.startswith(base + "/p/")]
    log(f"[{sid}] {len(links)} candidate links")

    saved, results = 0, []
    for url in links:
        if saved >= latest:
            break
        slug = slug_of(url)

        existing = sorted(out_dir.glob(f"*-{slug}.md"))
        if existing:
            log(f"[{sid}] cached: {slug}")
            results.append({
                "source": sid, "slug": slug, "status": "cached",
                "file": str(existing[0].relative_to(ROOT)),
            })
            saved += 1
            continue

        bu("open", url)
        time.sleep(PAGE_SETTLE_SECONDS)
        post = bu_eval(JS_EXTRACT_POST)

        if looks_like_index(post):
            log(f"[{sid}] skip index page: {slug}")
            continue

        pub = (post.get("published") or "")[:10] or date.today().isoformat()
        path = out_dir / f"{pub}-{slug}.md"
        front = "\n".join([
            "---",
            f"source: {sid}",
            f"url: {url}",
            f"title: {json.dumps(post['title'], ensure_ascii=False)}",
            f"published: {pub}",
            f"fetched_at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"chars: {post['chars']}",
            "---",
            "",
        ])
        path.write_text(front + post["body"], encoding="utf-8")

        short = post["chars"] < MIN_POST_CHARS
        log(f"[{sid}] saved {path.name} ({post['chars']} chars)"
            + (" ⚠️ short — check paywall/preview" if short else ""))
        results.append({
            "source": sid, "slug": slug, "status": "saved",
            "file": str(path.relative_to(ROOT)),
            "chars": post["chars"], "short_warning": short,
        })
        saved += 1

    return results


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch latest newsletter posts via logged-in browser session"
    )
    ap.add_argument("--source", help="only this source id")
    ap.add_argument("--latest", type=int, help="override per-source fetch_latest")
    ap.add_argument("--smoke", action="store_true", help="connectivity check only")
    args = ap.parse_args()

    try:
        sources = load_sources()
        if args.source:
            sources = [s for s in sources if s["id"] == args.source]
            if not sources:
                raise RuntimeError(f"no enabled source with id {args.source!r}")
        if not sources:
            raise RuntimeError("no enabled newsletter sources in config")

        connect()

        if args.smoke:
            s = sources[0]
            bu("open", s["url"].rstrip("/") + "/archive")
            time.sleep(PAGE_SETTLE_SECONDS)
            n = len(bu_eval(JS_COLLECT_LINKS))
            print(json.dumps({"ok": True, "data": {"smoke": True, "source": s["id"], "archive_links": n}}))
            return 0

        all_results: list[dict] = []
        for s in sources:
            all_results += fetch_source(s, args.latest or s.get("fetch_latest", 3))
        print(json.dumps({"ok": True, "data": {"fetched": all_results}}, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:  # noqa: BLE001 — single exit point, structured error
        print(json.dumps({
            "ok": False,
            "error": str(exc),
            "hint": "Is Chrome running with remote debugging (browser-use connect)? "
                    "Does config/sources.local.toml exist?",
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
