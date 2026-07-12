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
# Freshness window relative to panel as_of (docs/SENTIMENT.md escape: 过旧).
MAX_SAMPLE_AGE_DAYS = 7
# Allow small clock skew / same-day timezone drift; future beyond this → escape.
MAX_FUTURE_DAYS = 0
DEFAULT_SOURCE_ID = "sentiment-paste-a"

# Abstract source-id shape only (D2). Real channel/account names are rejected.
ABSTRACT_SOURCE_ID_RE = re.compile(
    r"^(?:sentiment-paste|sentiment-room|social|newsletter|research)"
    r"-[a-z0-9]+(?:-[a-z0-9]+)*$"
)

# Redact handles / @mentions / bare URLs / long digit IDs from quote spans.
REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://\S+", re.I),
    re.compile(r"www\.\S+", re.I),
    re.compile(r"@[\w.\u4e00-\u9fff]{2,}"),
    re.compile(r"#\w{2,}"),
    re.compile(r"\b\d{10,}\b"),  # snowflake-like / long numeric ids
)

# Named pattern library — closed, not open NLP. Keep abstract; no real handles.
# Vocabulary is intentionally de-overlapped where possible (e.g. 翻身 lives only
# under 暴富叙事). Runtime still de-duplicates overlapping match spans so one
# observation cannot satisfy the two-signal gate via multiple rule names.
FOMO_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("追涨/怕踏空", re.compile(
        r"怕踏空|踏空|追涨|上车|干就完了|all\s*in|加杠杆|梭哈|不看估值|必涨|要起飞|moon|FOMO",
        re.I,
    )),
    ("暴富叙事", re.compile(r"人生翻身|翻身|财富自由|一夜|狂飙|疯了一样买|排队入金", re.I)),
)
FUD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("投降/割肉", re.compile(r"割肉|投降|离场|再也不碰|永久退出|爆仓|崩了|完蛋|血亏|清仓跑", re.I)),
    ("恐慌叙事", re.compile(r"恐慌|绝望|没救了|归零|泡沫破裂|闪崩|FUD", re.I)),
)
WATCH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("观望/不敢", re.compile(r"观望|先看|不敢(追|买|做多)?|再等等|轻仓|空仓等待|静观", re.I)),
)

# (name, pattern, polarity) — single scan table for span-aware matching.
ALL_NAMED_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    *[(n, p, "FOMO") for n, p in FOMO_PATTERNS],
    *[(n, p, "FUD") for n, p in FUD_PATTERNS],
    *[(n, p, "观望") for n, p in WATCH_PATTERNS],
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


def parse_iso_date(value: str, *, field_name: str = "date") -> date:
    """Parse YYYY-MM-DD (or longer ISO prefix). Raises ValueError on failure."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} is empty")
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid ISO date: {value!r}") from exc


def normalize_source_id(raw: str | None, *, fallback: str = DEFAULT_SOURCE_ID) -> str:
    """Accept only abstract ids; map anything else to the default paste id.

    Real channel/account/server names must never leave the adapter (D2).
    """
    if raw is None:
        return fallback
    candidate = raw.strip()
    if ABSTRACT_SOURCE_ID_RE.fullmatch(candidate):
        return candidate
    if candidate:
        log(f"[privacy] rejected non-abstract source id {candidate!r} → {fallback}")
    return fallback


def redact_text(text: str) -> str:
    """Strip handles, URLs, and long numeric ids from excerpts before emit."""
    out = text
    for pat in REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def sample_from_text(
    text: str,
    *,
    source_id: str | None = None,
    origin: str = "paste",
    path: str | None = None,
) -> Sample:
    meta, body = parse_front_matter(text)
    # Prefer explicit CLI override; never trust free-form front-matter names
    # unless they already match the abstract-id shape.
    sid = normalize_source_id(source_id or meta.get("source") or DEFAULT_SOURCE_ID)
    observed_raw = (
        meta.get("observed_at")
        or meta.get("published")
        or meta.get("fetched_at", "")[:10]
        or _default_observed_at()
    )
    # Validate early so bad dates surface as structured errors, not silent today.
    observed_date = parse_iso_date(str(observed_raw)[:10], field_name="observed_at")
    return Sample(
        source_id=sid,
        text=body.strip() if meta else text.strip(),
        observed_at=observed_date.isoformat(),
        origin=meta.get("origin") or origin,
        path=path,
    )


def _infer_source_id(path: Path, override: str | None) -> str:
    if override:
        return normalize_source_id(override)
    parent = path.parent.name
    if parent and parent not in {".", "raw", "sources", "tmp", "temp"}:
        return normalize_source_id(parent)
    return DEFAULT_SOURCE_ID


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
    return redact_text(span[:80])


def assess_freshness(
    samples: list[Sample],
    *,
    as_of: date,
    max_age_days: int = MAX_SAMPLE_AGE_DAYS,
) -> str | None:
    """Return escape_reason if samples are unusable for as_of, else None.

    Rules (docs/SENTIMENT.md):
    - unparseable observed_at → escape (caller should have validated already)
    - any sample observed_at > as_of + MAX_FUTURE_DAYS → escape (future-dated)
    - all samples older than max_age_days relative to as_of → escape (过旧)
    - mix of fresh + stale: keep going; stale ones are filtered by caller if needed
    """
    if not samples:
        return "样本集合为空"

    ages: list[int] = []
    for s in samples:
        try:
            obs = parse_iso_date(s.observed_at, field_name="observed_at")
        except ValueError:
            return f"样本 observed_at 无法解析: {s.observed_at!r}"
        delta = (as_of - obs).days
        if delta < -MAX_FUTURE_DAYS:
            return f"样本 observed_at 晚于 as_of（未来日期）: {s.observed_at} > {as_of.isoformat()}"
        ages.append(delta)

    fresh = [a for a in ages if 0 <= a <= max_age_days]
    if not fresh:
        oldest = max(ages) if ages else -1
        return (
            f"样本全部过旧：相对 as_of={as_of.isoformat()} 均超过 "
            f"{max_age_days} 天（最旧偏移 {oldest} 天）"
        )
    return None


def filter_fresh_samples(
    samples: list[Sample],
    *,
    as_of: date,
    max_age_days: int = MAX_SAMPLE_AGE_DAYS,
) -> list[Sample]:
    """Keep only samples within [as_of - max_age_days, as_of + MAX_FUTURE_DAYS]."""
    kept: list[Sample] = []
    for s in samples:
        try:
            obs = parse_iso_date(s.observed_at, field_name="observed_at")
        except ValueError:
            continue
        delta = (as_of - obs).days
        if -MAX_FUTURE_DAYS <= delta <= max_age_days:
            kept.append(s)
    return kept


@dataclass(frozen=True)
class MatchHit:
    """One regex observation with character span for independence checks."""

    source_id: str
    name: str
    polarity: str
    start: int
    end: int
    quote_span: str

    @property
    def span_len(self) -> int:
        return self.end - self.start


def _collect_raw_hits(samples: list[Sample]) -> list[MatchHit]:
    """All first-match hits per (sample, named pattern), before de-overlap."""
    hits: list[MatchHit] = []
    for s in samples:
        text = s.text
        for name, pat, polarity in ALL_NAMED_PATTERNS:
            m = pat.search(text)
            if not m:
                continue
            start, end = m.start(), m.end()
            radius = 16
            q0 = max(0, start - radius)
            q1 = min(len(text), end + radius)
            span = redact_text(text[q0:q1].replace("\n", " ").strip()[:80])
            hits.append(
                MatchHit(
                    source_id=s.source_id,
                    name=name,
                    polarity=polarity,
                    start=start,
                    end=end,
                    quote_span=span,
                )
            )
    return hits


def _spans_overlap(a: MatchHit, b: MatchHit) -> bool:
    """True if character ranges overlap or nest (same observation)."""
    if a.source_id != b.source_id:
        return False
    return a.start < b.end and b.start < a.end


def dedupe_hits_by_span(hits: list[MatchHit]) -> list[MatchHit]:
    """Keep independent observations only: overlapping spans collapse to one.

    Prefer the longer match (more specific observation), then earlier start,
    then stable name order. Distinct non-overlapping spans remain independent
    even if they share a pattern name family.
    """
    if not hits:
        return []
    ordered = sorted(hits, key=lambda h: (-h.span_len, h.start, h.name))
    kept: list[MatchHit] = []
    for h in ordered:
        if any(_spans_overlap(h, k) for k in kept):
            continue
        kept.append(h)
    # Stable output order by source then position.
    kept.sort(key=lambda h: (h.source_id, h.start, h.name))
    return kept


def scan_patterns(
    samples: list[Sample],
) -> tuple[list[Evidence], int, int, int]:
    """Return (evidence, fomo_hits, fud_hits, watch_hits) after span de-overlap."""
    independent = dedupe_hits_by_span(_collect_raw_hits(samples))
    evidence: list[Evidence] = []
    fomo = fud = watch = 0
    for h in independent:
        if h.polarity == "FOMO":
            fomo += 1
            note = "规则命中：FOMO 模式库（span 去重叠）"
        elif h.polarity == "FUD":
            fud += 1
            note = "规则命中：FUD 模式库（span 去重叠）"
        else:
            watch += 1
            note = "规则命中：观望模式库（span 去重叠）"
        evidence.append(
            Evidence(
                source_id=h.source_id,
                signal=h.name,
                quote_span=h.quote_span,
                polarity=h.polarity,
                note=note,
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
    """Count independent observations after span de-overlap.

    Independence is defined on non-overlapping character spans, not merely
    distinct rule names. Overlapping multi-name matches (e.g. nested 翻身
    variants) count as one signal. docs/SENTIMENT.md requires ≥2 independent
    signals for every concrete (non-escape) index_level. Text length is never
    a signal.
    """
    return len(dedupe_hits_by_span(_collect_raw_hits(samples)))


def classify_rules(samples: list[Sample], *, as_of: str | None = None) -> PanelResult:
    """rules_v0: closed pattern counts → index_level. Intentionally coarse.

    Hard rules (docs/SENTIMENT.md):
    - every concrete level needs ≥2 independent named-pattern signals
    - text length is coverage/quality only — never a substitute signal
    - samples must fall inside the freshness window relative to as_of
    - emitted source_ids are abstract only; quote spans are redacted
    """
    as_of_str = as_of or _default_observed_at()
    as_of_date = parse_iso_date(as_of_str, field_name="as_of")
    as_of_str = as_of_date.isoformat()

    # Normalize source ids at the panel boundary (defense in depth).
    samples = [
        Sample(
            source_id=normalize_source_id(s.source_id),
            text=s.text,
            observed_at=s.observed_at,
            origin=s.origin,
            path=s.path,
        )
        for s in samples
    ]

    freshness_reason = assess_freshness(samples, as_of=as_of_date)
    if freshness_reason:
        source_ids = sorted({s.source_id for s in samples})
        return PanelResult(
            index_level=ESCAPE_LEVEL,
            dominant_mode="信息不足",
            confidence="低",
            sample_quality="不可用",
            as_of=as_of_str,
            method="rules_v0",
            sample_count=len(samples),
            source_ids=source_ids,
            evidence=[],
            triggers_fired=["freshness_gate"],
            escape_reason=freshness_reason,
            llm_status="not_invoked",
        )

    # Drop out-of-window samples so grading uses only the falsifiable window.
    windowed = filter_fresh_samples(samples, as_of=as_of_date)
    if not windowed:
        source_ids = sorted({s.source_id for s in samples})
        return PanelResult(
            index_level=ESCAPE_LEVEL,
            dominant_mode="信息不足",
            confidence="低",
            sample_quality="不可用",
            as_of=as_of_str,
            method="rules_v0",
            sample_count=len(samples),
            source_ids=source_ids,
            evidence=[],
            triggers_fired=["freshness_gate"],
            escape_reason="新鲜度窗口内无可用样本",
            llm_status="not_invoked",
        )

    source_ids = sorted({s.source_id for s in windowed})
    quality, total_chars, early = assess_sample_quality(windowed)
    evidence, fomo, fud, watch = scan_patterns(windowed)
    independent_signals = _count_named_pattern_hits(windowed)
    triggers: list[str] = []
    llm_status = "not_invoked"

    if early:
        return PanelResult(
            index_level=ESCAPE_LEVEL,
            dominant_mode="信息不足",
            confidence="低",
            sample_quality=quality,
            as_of=as_of_str,
            method="rules_v0",
            sample_count=len(windowed),
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
            as_of=as_of_str,
            method="rules_v0",
            sample_count=len(windowed),
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
        as_of=as_of_str,
        method="rules_v0",
        sample_count=len(windowed),
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
        if args.as_of:
            as_of = parse_iso_date(args.as_of, field_name="as_of").isoformat()
        else:
            as_of = _default_observed_at()

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
