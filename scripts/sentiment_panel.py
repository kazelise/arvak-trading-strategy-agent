#!/usr/bin/env python3
"""Retail sentiment panel skeleton — 宝妈指数 (M2-prep).

Design notes (docs/SENTIMENT.md, docs/DESIGN.md):
- stdlib only. Closed vocabulary for index_level; escape hatch required.
- Input path v0 = manual paste adapter only. No social crawlers (ToS).
- Output protocol (shared by all scripts in this repo):
  stdout → {"ok": true, "data": ...} | {"ok": false, "error": ..., "hint": ...}
  stderr → human-readable progress diagnostics.
- Sources referred to only by abstract config `id` — never real names (D2).
- LLM path is reserved: prompt lives in prompts/sentiment_panel.md as data.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = ROOT / "prompts" / "sentiment_panel.md"
SOURCES_RAW = ROOT / "sources" / "raw"

# ── Closed vocabularies (docs/SENTIMENT.md) ──────────────────────────

INDEX_LEVELS = frozenset({"冰点", "低迷", "中性", "亢奋", "狂热", "信息不足以分级"})
DOMINANT_MODES = frozenset({"FUD", "FOMO", "观望", "混合", "信息不足"})
CONFIDENCE_LEVELS = frozenset({"低", "中", "高"})
SAMPLE_QUALITIES = frozenset({"可用", "偏薄", "不可用"})
POLARITIES = frozenset({"FUD", "FOMO", "观望", "中性", "噪声"})

ESCAPE_LEVEL = "信息不足以分级"
MIN_USABLE_CHARS = 40
MIN_THIN_CHARS = 120

# Named pattern library — closed, not open NLP. Keep abstract; no real handles.
FOMO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("追涨/怕踏空", re.compile(r"怕踏空|踏空|追涨|上车|干就完了|all\s*in|加杠杆|梭哈|不看估值|人生翻身|必涨|要起飞|moon|FOMO", re.I)),
    ("暴富叙事", re.compile(r"翻身|财富自由|一夜|狂飙|疯了一样买|排队入金", re.I)),
)
FUD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("投降/割肉", re.compile(r"割肉|投降|离场|再也不碰|永久退出|爆仓|崩了|完蛋|血亏|清仓跑", re.I)),
    ("恐慌叙事", re.compile(r"恐慌|绝望|没救了|归零|泡沫破裂|闪崩|FUD", re.I)),
)
WATCH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("观望/不敢", re.compile(r"观望|先看|不敢(追|买|做多)?|再等等|轻仓|空仓等待|静观", re.I)),
)


@dataclass
class Sample:
    source_id: str
    text: str
    observed_at: str
    origin: str = "paste"
    path: str | None = None


@dataclass
class Evidence:
    source_id: str
    signal: str
    quote_span: str = ""
    polarity: str = "中性"
    note: str = ""

    def as_dict(self) -> dict:
        d = {
            "source_id": self.source_id,
            "signal": self.signal,
            "quote_span": self.quote_span,
            "polarity": self.polarity,
        }
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class PanelResult:
    index_level: str
    dominant_mode: str
    confidence: str
    sample_quality: str
    as_of: str
    method: str
    sample_count: int
    source_ids: list[str]
    evidence: list[Evidence] = field(default_factory=list)
    triggers_fired: list[str] = field(default_factory=list)
    escape_reason: str | None = None
    llm_status: str = "not_invoked"

    def as_dict(self) -> dict:
        if self.index_level not in INDEX_LEVELS:
            raise ValueError(f"index_level outside closed vocab: {self.index_level!r}")
        if self.dominant_mode not in DOMINANT_MODES:
            raise ValueError(f"dominant_mode outside closed vocab: {self.dominant_mode!r}")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"confidence outside closed vocab: {self.confidence!r}")
        if self.sample_quality not in SAMPLE_QUALITIES:
            raise ValueError(f"sample_quality outside closed vocab: {self.sample_quality!r}")
        return {
            "index_level": self.index_level,
            "dominant_mode": self.dominant_mode,
            "confidence": self.confidence,
            "sample_quality": self.sample_quality,
            "as_of": self.as_of,
            "method": self.method,
            "sample_count": self.sample_count,
            "source_ids": list(self.source_ids),
            "evidence": [e.as_dict() for e in self.evidence],
            "triggers_fired": list(self.triggers_fired),
            "escape_reason": self.escape_reason,
            "llm_status": self.llm_status,
        }


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


class JsonArgumentParser(argparse.ArgumentParser):
    """Argparse that never bypasses the shared {"ok":…} stdout protocol."""

    def error(self, message: str) -> None:  # type: ignore[override]
        log(f"[args] {message}")
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": message,
                    "hint": "Check flags: --smoke | --input PATH | --text '…'; "
                    "--method rules|llm|rules+llm. See docs/SENTIMENT.md.",
                },
                ensure_ascii=False,
            )
        )
        sys.exit(2)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Hand-rolled front matter (no yaml dep, D4). Same spirit as build_brief."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
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


def _default_observed_at() -> str:
    return date.today().isoformat()


def sample_from_text(
    text: str,
    *,
    source_id: str | None = None,
    origin: str = "paste",
    path: str | None = None,
) -> Sample:
    meta, body = parse_front_matter(text)
    sid = source_id or meta.get("source") or "sentiment-paste-a"
    observed = (
        meta.get("observed_at")
        or meta.get("published")
        or meta.get("fetched_at", "")[:10]
        or _default_observed_at()
    )
    return Sample(
        source_id=sid,
        text=body.strip() if meta else text.strip(),
        observed_at=observed[:10] if observed else _default_observed_at(),
        origin=meta.get("origin") or origin,
        path=path,
    )


def _infer_source_id(path: Path, override: str | None) -> str | None:
    if override:
        return override
    parent = path.parent.name
    if parent and parent not in {".", "raw", "sources", "tmp", "temp"}:
        return parent
    return None


def load_samples_from_path(path: Path, source_id: str | None = None) -> list[Sample]:
    if not path.exists():
        raise RuntimeError(f"input path does not exist: {path}")
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        sid = _infer_source_id(path, source_id)
        return [sample_from_text(text, source_id=sid, path=str(path))]

    files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in {".md", ".txt"})
    if not files:
        raise RuntimeError(f"no .md/.txt samples under {path}")
    samples: list[Sample] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        sid = _infer_source_id(f, source_id)
        samples.append(sample_from_text(text, source_id=sid, path=str(f)))
    return samples


def _first_match_span(pattern: re.Pattern[str], text: str, radius: int = 16) -> str:
    m = pattern.search(text)
    if not m:
        return ""
    start = max(0, m.start() - radius)
    end = min(len(text), m.end() + radius)
    span = text[start:end].replace("\n", " ").strip()
    return span[:80]


def scan_patterns(
    samples: list[Sample],
) -> tuple[list[Evidence], int, int, int]:
    """Return (evidence, fomo_hits, fud_hits, watch_hits)."""
    evidence: list[Evidence] = []
    fomo = fud = watch = 0
    for s in samples:
        text = s.text
        for name, pat in FOMO_PATTERNS:
            if pat.search(text):
                fomo += 1
                evidence.append(
                    Evidence(
                        source_id=s.source_id,
                        signal=name,
                        quote_span=_first_match_span(pat, text),
                        polarity="FOMO",
                        note="规则命中：FOMO 模式库",
                    )
                )
        for name, pat in FUD_PATTERNS:
            if pat.search(text):
                fud += 1
                evidence.append(
                    Evidence(
                        source_id=s.source_id,
                        signal=name,
                        quote_span=_first_match_span(pat, text),
                        polarity="FUD",
                        note="规则命中：FUD 模式库",
                    )
                )
        for name, pat in WATCH_PATTERNS:
            if pat.search(text):
                watch += 1
                evidence.append(
                    Evidence(
                        source_id=s.source_id,
                        signal=name,
                        quote_span=_first_match_span(pat, text),
                        polarity="观望",
                        note="规则命中：观望模式库",
                    )
                )
    return evidence, fomo, fud, watch


def assess_sample_quality(samples: list[Sample]) -> tuple[str, int, str | None]:
    """Return (quality, total_chars, early_escape_reason)."""
    if not samples:
        return "不可用", 0, "样本集合为空"
    total = sum(len(s.text.strip()) for s in samples)
    nonempty = [s for s in samples if len(s.text.strip()) >= MIN_USABLE_CHARS]
    if total < MIN_USABLE_CHARS or not nonempty:
        return "不可用", total, "样本过短或无可解析正文"
    if total < MIN_THIN_CHARS or len(nonempty) < len(samples):
        return "偏薄", total, None
    return "可用", total, None


def _count_named_pattern_hits(samples: list[Sample]) -> int:
    """Count distinct named patterns hit across the library (independent signals).

    A "signal" is one named pattern entry (e.g. 追涨/怕踏空, 投降/割肉),
    not text length. docs/SENTIMENT.md requires ≥2 independent signals for
    every concrete (non-escape) index_level.
    """
    names: set[str] = set()
    for s in samples:
        for name, pat in (*FOMO_PATTERNS, *FUD_PATTERNS, *WATCH_PATTERNS):
            if pat.search(s.text):
                names.add(name)
    return len(names)


def classify_rules(samples: list[Sample], *, as_of: str | None = None) -> PanelResult:
    """rules_v0: closed pattern counts → index_level. Intentionally coarse.

    Hard rule (docs/SENTIMENT.md): every concrete level needs ≥2 independent
    named-pattern signals. Text length is coverage/quality only — never a
    substitute signal.
    """
    as_of = as_of or _default_observed_at()
    source_ids = sorted({s.source_id for s in samples})
    quality, total_chars, early = assess_sample_quality(samples)
    evidence, fomo, fud, watch = scan_patterns(samples)
    independent_signals = _count_named_pattern_hits(samples)
    triggers: list[str] = []
    llm_status = "not_invoked"

    if early:
        return PanelResult(
            index_level=ESCAPE_LEVEL,
            dominant_mode="信息不足",
            confidence="低",
            sample_quality=quality,
            as_of=as_of,
            method="rules_v0",
            sample_count=len(samples),
            source_ids=source_ids,
            evidence=evidence,
            triggers_fired=[],
            escape_reason=early,
            llm_status=llm_status,
        )

    if fomo:
        triggers.append(f"FOMO模式命中×{fomo}")
    if fud:
        triggers.append(f"FUD模式命中×{fud}")
    if watch:
        triggers.append(f"观望模式命中×{watch}")
    triggers.append(f"独立具名信号×{independent_signals}")

    # Dominant mode (descriptive even when we later escape).
    if fomo == 0 and fud == 0 and watch == 0:
        dominant = "信息不足"
    elif fomo > 0 and fud > 0 and abs(fomo - fud) <= 1:
        dominant = "混合"
    elif fomo > fud and fomo > watch:
        dominant = "FOMO"
    elif fud > fomo and fud > watch:
        dominant = "FUD"
    elif watch >= fomo and watch >= fud and watch > 0:
        dominant = "观望"
    else:
        dominant = "混合"

    # Hard escape: every concrete level needs ≥2 independent named signals.
    # Length/coverage is not a signal (review blocker #1).
    if independent_signals < 2:
        reason = (
            "未命中任何具名情绪模式，独立信号不足"
            if independent_signals == 0
            else "独立具名信号不足 2 条，不足以给出具体分档"
        )
        return PanelResult(
            index_level=ESCAPE_LEVEL,
            dominant_mode=dominant if independent_signals else "信息不足",
            confidence="低",
            sample_quality=quality,
            as_of=as_of,
            method="rules_v0",
            sample_count=len(samples),
            source_ids=source_ids,
            evidence=evidence,
            triggers_fired=triggers,
            escape_reason=reason,
            llm_status=llm_status,
        )

    # Map counts → level (coarse placeholder); only reached with ≥2 signals.
    if fud >= 3 and fud > fomo * 2 and watch <= fud:
        level = "冰点"
        triggers.append("投降/恐慌话术占主导")
    elif fud >= 1 and fud >= fomo and fud >= watch:
        level = "低迷"
        triggers.append("悲观/FUD 话术占优")
    elif fomo >= 3 and fomo > fud * 2 and watch <= fomo:
        level = "狂热"
        triggers.append("极端FOMO/暴富叙事占主导")
    elif fomo >= 1 and fomo >= fud and fomo >= watch:
        level = "亢奋"
        triggers.append("追涨/怕踏空话术占优")
    elif watch > 0 and watch >= fomo and watch >= fud and max(fomo, fud) <= 1:
        if fud > 0:
            level = "低迷"
            triggers.append("观望偏空")
        else:
            level = "中性"
            triggers.append("观望为主、多空不极端")
    else:
        level = "中性"
        triggers.append("多空信号并存或倾斜不足")

    if quality == "偏薄":
        confidence = "低"
    elif independent_signals >= 2 and total_chars >= MIN_THIN_CHARS:
        confidence = "中" if max(fomo, fud, watch) < 3 else "高"
    else:
        confidence = "低"

    return PanelResult(
        index_level=level,
        dominant_mode=dominant,
        confidence=confidence,
        sample_quality=quality,
        as_of=as_of,
        method="rules_v0",
        sample_count=len(samples),
        source_ids=source_ids,
        evidence=evidence,
        triggers_fired=triggers,
        escape_reason=None,
        llm_status=llm_status,
    )


def llm_path_reserved(samples: list[Sample], *, as_of: str | None = None) -> PanelResult:
    """LLM path skeleton: do not call network in v0; surface reservation clearly."""
    as_of = as_of or _default_observed_at()
    prompt_present = PROMPT_FILE.exists()
    base = classify_rules(samples, as_of=as_of)
    base.method = "llm_v0_reserved"
    base.llm_status = "reserved"
    if base.escape_reason is None:
        base.escape_reason = None
    # Keep rules result as offline fallback but mark reservation on triggers.
    base.triggers_fired = list(base.triggers_fired) + [
        "llm路径预留：未发起模型调用",
        f"prompt_file={'present' if prompt_present else 'missing'}:{PROMPT_FILE.relative_to(ROOT)}",
    ]
    log("[llm] path reserved — returning rules_v0 fallback with llm_status=reserved")
    return base


def build_smoke_data() -> dict:
    fixture = (
        "今天又割肉了，再也不碰这个市场，心态崩了。\n"
        "另一边有人喊怕踏空要上车，还有人说先观望不敢追。\n"
        "样本仅用于连通性检查，不是真实频道导出。"
    )
    samples = [
        Sample(
            source_id="sentiment-paste-a",
            text=fixture,
            observed_at=_default_observed_at(),
            origin="paste",
        )
    ]
    result = classify_rules(samples)
    data = result.as_dict()
    data["smoke"] = True
    data["prompt_present"] = PROMPT_FILE.exists()
    return data


def build_parser() -> JsonArgumentParser:
    ap = JsonArgumentParser(
        description="Retail sentiment panel (宝妈指数) — paste adapter + rules_v0 skeleton"
    )
    ap.add_argument(
        "--input",
        help="path to a sample file or directory of .md/.txt pastes",
    )
    ap.add_argument(
        "--source-id",
        help="abstract source id override (e.g. sentiment-paste-a)",
    )
    ap.add_argument(
        "--method",
        choices=("rules", "llm", "rules+llm"),
        default="rules",
        help="classification method (llm paths are reserved in this skeleton)",
    )
    ap.add_argument(
        "--as-of",
        help="ISO date for the panel snapshot (default: today)",
    )
    ap.add_argument(
        "--text",
        help="inline paste text (alternative to --input; for quick checks)",
    )
    ap.add_argument(
        "--smoke",
        action="store_true",
        help="run built-in connectivity/fixture check",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    try:
        if args.smoke:
            data = build_smoke_data()
            log(f"[smoke] index_level={data['index_level']} quality={data['sample_quality']}")
            print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
            return 0

        samples: list[Sample] = []
        if args.text is not None:
            samples.append(
                sample_from_text(args.text, source_id=args.source_id or "sentiment-paste-a")
            )
        if args.input:
            samples.extend(load_samples_from_path(Path(args.input), source_id=args.source_id))

        if not samples and not args.input and args.text is None:
            # Default discovery: sources/raw/*/ sentiment-ish files if present.
            if SOURCES_RAW.exists():
                discovered = sorted(SOURCES_RAW.glob("*/*sentiment*.md"))
                for f in discovered:
                    samples.append(
                        sample_from_text(
                            f.read_text(encoding="utf-8"),
                            source_id=args.source_id or f.parent.name,
                            path=str(f),
                        )
                    )
            if not samples:
                raise RuntimeError(
                    "no input samples — pass --input, --text, or --smoke"
                )

        log(f"[input] {len(samples)} sample(s); method={args.method}")
        as_of = args.as_of or _default_observed_at()

        if args.method == "rules":
            result = classify_rules(samples, as_of=as_of)
        elif args.method in {"llm", "rules+llm"}:
            result = llm_path_reserved(samples, as_of=as_of)
        else:
            raise RuntimeError(f"unknown method {args.method!r}")

        data = result.as_dict()
        log(
            f"[result] level={data['index_level']} mode={data['dominant_mode']} "
            f"confidence={data['confidence']} quality={data['sample_quality']}"
        )
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2))
        return 0

    except Exception as exc:  # noqa: BLE001 — single exit point, structured error
        log(f"[error] {exc}")
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                    "hint": "Provide --input path/to/paste.md or --text '…' or --smoke; "
                    "see docs/SENTIMENT.md for the paste adapter and closed vocabulary.",
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
