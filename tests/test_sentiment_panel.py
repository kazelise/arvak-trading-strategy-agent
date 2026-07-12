#!/usr/bin/env python3
"""Unit tests for scripts/sentiment_panel.py (M2-prep skeleton).

stdlib unittest only — zero pip dependencies. Run:
  python3 -m unittest tests/test_sentiment_panel.py -v
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sentiment_panel.py"
AS_OF = "2026-07-12"
AS_OF_DATE = date.fromisoformat(AS_OF)


def load_module():
    spec = importlib.util.spec_from_file_location("sentiment_panel", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses looks up cls.__module__ in sys.modules during decoration
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sp = load_module()


class ClosedVocabTests(unittest.TestCase):
    def test_index_levels_are_closed(self):
        expected = {"冰点", "低迷", "中性", "亢奋", "狂热", "信息不足以分级"}
        self.assertEqual(sp.INDEX_LEVELS, expected)

    def test_as_dict_rejects_illegal_level(self):
        result = sp.PanelResult(
            index_level="偏强",  # illegal fuzzy word
            dominant_mode="混合",
            confidence="低",
            sample_quality="偏薄",
            as_of="2026-07-12",
            method="rules_v0",
            sample_count=0,
            source_ids=[],
        )
        with self.assertRaises(ValueError):
            result.as_dict()


class EscapeHatchTests(unittest.TestCase):
    def test_empty_samples(self):
        r = sp.classify_rules([], as_of=AS_OF)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIsNotNone(r.escape_reason)
        self.assertEqual(r.sample_quality, "不可用")
        self.assertEqual(r.dominant_mode, "信息不足")

    def test_too_short_sample(self):
        samples = [sp.Sample("sentiment-paste-a", "短", AS_OF)]
        r = sp.classify_rules(samples, as_of=AS_OF)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIn("过短", r.escape_reason or "")

    def test_long_but_no_pattern(self):
        text = "今天天气不错，大家讨论了午餐吃什么。" * 5
        samples = [sp.Sample("sentiment-paste-a", text, AS_OF)]
        r = sp.classify_rules(samples, as_of=AS_OF)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIsNotNone(r.escape_reason)

    def test_long_single_named_pattern_escapes(self):
        """Length is not a sentiment signal; one named pattern → escape."""
        # Long neutral prose + a single FOMO cue (only 追涨/怕踏空 family).
        filler = "今天大家在聊天气和通勤，没有别的内容。" * 8
        text = filler + "有人说怕踏空。" + filler
        self.assertGreaterEqual(len(text), sp.MIN_THIN_CHARS)
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, AS_OF)], as_of=AS_OF)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIsNotNone(r.escape_reason)
        self.assertIn("独立", r.escape_reason or "")
        self.assertEqual(sp._count_named_pattern_hits([sp.Sample("sentiment-paste-a", text, AS_OF)]), 1)

    def test_single_fud_keyword_only_escapes(self):
        text = ("今天市场一般，有人提到割肉。" + "其他都在聊午饭。") * 6
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, AS_OF)], as_of=AS_OF)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertEqual(sp._count_named_pattern_hits([sp.Sample("sentiment-paste-a", text, AS_OF)]), 1)

    def test_rensheng_fanshen_single_observation_escapes(self):
        """Exact regression: 人生翻身 must not double-count as two independent signals.

        Historically 人生翻身 matched both 追涨/怕踏空 and 暴富叙事 via nested
        翻身; span de-overlap + vocab de-overlap must yield one observation → escape.
        """
        filler = "今天大家在聊天气和通勤，没有别的内容。" * 8
        text = filler + "人生翻身。" + filler
        samples = [sp.Sample("sentiment-paste-a", text, AS_OF)]
        self.assertEqual(sp._count_named_pattern_hits(samples), 1)
        r = sp.classify_rules(samples, as_of=AS_OF)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIsNotNone(r.escape_reason)
        self.assertIn("独立", r.escape_reason or "")
        # At most one evidence item after span dedupe.
        self.assertLessEqual(len(r.evidence), 1)

    def test_overlapping_spans_dedupe_to_one(self):
        hits = [
            sp.MatchHit("sentiment-paste-a", "追涨/怕踏空", "FOMO", 10, 14, "翻身"),
            sp.MatchHit("sentiment-paste-a", "暴富叙事", "FOMO", 8, 14, "人生翻身"),
            sp.MatchHit("sentiment-paste-a", "投降/割肉", "FUD", 40, 42, "割肉"),
        ]
        kept = sp.dedupe_hits_by_span(hits)
        self.assertEqual(len(kept), 2)
        names = {h.name for h in kept}
        self.assertIn("暴富叙事", names)  # longer span preferred
        self.assertIn("投降/割肉", names)
        self.assertNotIn("追涨/怕踏空", names)


class FreshnessTests(unittest.TestCase):
    def _strong_fud(self) -> str:
        return (
            "彻底割肉了，再也不碰，心态崩了，市场没救了，永久退出。"
            "又爆仓了，恐慌盘出不来，血亏清仓跑。"
            "归零风险太大，泡沫破裂闪崩，FUD 满天飞，投降了。"
        )

    def test_stale_samples_escape(self):
        old = (AS_OF_DATE - timedelta(days=sp.MAX_SAMPLE_AGE_DAYS + 30)).isoformat()
        r = sp.classify_rules(
            [sp.Sample("sentiment-paste-a", self._strong_fud(), old)],
            as_of=AS_OF,
        )
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIn("过旧", r.escape_reason or "")
        self.assertIn("freshness_gate", r.triggers_fired)

    def test_future_dated_samples_escape(self):
        future = (AS_OF_DATE + timedelta(days=3)).isoformat()
        r = sp.classify_rules(
            [sp.Sample("sentiment-paste-a", self._strong_fud(), future)],
            as_of=AS_OF,
        )
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIn("未来", r.escape_reason or "")

    def test_fresh_window_boundary_inclusive(self):
        edge = (AS_OF_DATE - timedelta(days=sp.MAX_SAMPLE_AGE_DAYS)).isoformat()
        r = sp.classify_rules(
            [sp.Sample("sentiment-paste-a", self._strong_fud(), edge)],
            as_of=AS_OF,
        )
        self.assertIn(r.index_level, {"冰点", "低迷"})
        self.assertIsNone(r.escape_reason)

    def test_invalid_as_of_raises(self):
        with self.assertRaises(ValueError):
            sp.classify_rules(
                [sp.Sample("sentiment-paste-a", self._strong_fud(), AS_OF)],
                as_of="not-a-date",
            )

    def test_stale_mixed_with_fresh_uses_fresh_only(self):
        old = (AS_OF_DATE - timedelta(days=60)).isoformat()
        # Stale is extreme FOMO; fresh is FUD — result should follow fresh FUD.
        fomo = (
            "怕踏空了赶紧上车，干就完了 all in 加杠杆梭哈。"
            "不看估值要起飞，人生翻身财富自由，moon 了。"
            "排队入金狂飙，疯了一样买，必涨叙事。"
        )
        r = sp.classify_rules(
            [
                sp.Sample("sentiment-paste-a", fomo, old),
                sp.Sample("sentiment-paste-a", self._strong_fud(), AS_OF),
            ],
            as_of=AS_OF,
        )
        self.assertIn(r.index_level, {"冰点", "低迷"})
        self.assertEqual(r.dominant_mode, "FUD")


class BoundaryClassificationTests(unittest.TestCase):
    def test_strong_fud_maps_to_ice_or_low(self):
        text = (
            "彻底割肉了，再也不碰，心态崩了，市场没救了，永久退出。"
            "又爆仓了，恐慌盘出不来，血亏清仓跑。"
            "归零风险太大，泡沫破裂闪崩，FUD 满天飞，投降了。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, AS_OF)], as_of=AS_OF)
        self.assertIn(r.index_level, {"冰点", "低迷"})
        self.assertEqual(r.dominant_mode, "FUD")
        self.assertGreaterEqual(len(r.evidence), 2)
        self.assertIsNone(r.escape_reason)

    def test_strong_fomo_maps_to_hot_or_mania(self):
        text = (
            "怕踏空了赶紧上车，干就完了 all in 加杠杆梭哈。"
            "不看估值要起飞，人生翻身财富自由，moon 了。"
            "排队入金狂飙，疯了一样买，必涨叙事。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, AS_OF)], as_of=AS_OF)
        self.assertIn(r.index_level, {"亢奋", "狂热"})
        self.assertEqual(r.dominant_mode, "FOMO")
        self.assertIsNone(r.escape_reason)

    def test_mixed_signals_map_to_neutral_or_concrete(self):
        text = (
            "一边有人割肉投降说再也不碰，"
            "一边有人怕踏空要上车追涨，"
            "还有人说先观望不敢追，再等等。"
            "多空都很吵，谁也没压过谁。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, AS_OF)], as_of=AS_OF)
        # ≥2 named signals → concrete level (mixed leans 中性 or mild tilt).
        self.assertIn(r.index_level, {"中性", "低迷", "亢奋"})
        self.assertNotEqual(r.index_level, "信息不足以分级")
        data = r.as_dict()
        self.assertIn(data["index_level"], sp.INDEX_LEVELS)

    def test_two_named_signals_required_for_watch_plus_fud(self):
        text = (
            "今天先观望吧，不敢追，再等等，轻仓看看，空仓等待，静观其变。"
            "也有人割肉离场，心态崩了，再也不碰。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, AS_OF)], as_of=AS_OF)
        self.assertIn(r.index_level, {"中性", "低迷"})
        self.assertNotIn(r.index_level, {"冰点", "狂热", "信息不足以分级"})

    def test_single_watch_family_escapes(self):
        # Only the 观望/不敢 named pattern; avoid FOMO/FUD keywords entirely.
        text = (
            "今天先观望吧，不敢追，再等等，轻仓看看，空仓等待，静观其变。"
            "讨论量一般，没有一边倒的情绪集群。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, AS_OF)], as_of=AS_OF)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertEqual(
            sp._count_named_pattern_hits([sp.Sample("sentiment-paste-a", text, AS_OF)]), 1
        )


class AdapterAndProtocolTests(unittest.TestCase):
    def test_front_matter_sample(self):
        raw = (
            "---\n"
            "source: sentiment-paste-a\n"
            "kind: sentiment_sample\n"
            "observed_at: 2026-07-11\n"
            "origin: paste\n"
            "---\n\n"
            "先观望吧，不敢追，再等等看情况。\n"
        )
        sample = sp.sample_from_text(raw)
        self.assertEqual(sample.source_id, "sentiment-paste-a")
        self.assertEqual(sample.observed_at, "2026-07-11")
        self.assertIn("观望", sample.text)

    def test_load_from_temp_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.md"
            p.write_text(
                "割肉离场了，心态崩了，再也不碰，恐慌没救了，归零风险，FUD。" * 2,
                encoding="utf-8",
            )
            samples = sp.load_samples_from_path(p, source_id="sentiment-paste-a")
            self.assertEqual(len(samples), 1)
            r = sp.classify_rules(samples)
            self.assertIn(r.index_level, sp.INDEX_LEVELS)
            self.assertNotEqual(r.index_level, "信息不足以分级")

    def test_smoke_cli(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--smoke"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["data"]["index_level"], sp.INDEX_LEVELS)
        self.assertTrue(payload["data"].get("smoke"))
        self.assertTrue(proc.stderr.strip())

    def test_cli_missing_input_fails_structured(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)
        self.assertIn("hint", payload)
        # Protocol: diagnostics on stderr for handled failures.
        self.assertTrue(proc.stderr.strip(), msg="stderr must carry human diagnostics")
        self.assertIn("[error]", proc.stderr)

    def test_cli_invalid_method_choice_json(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--method", "bogus"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)
        self.assertIn("hint", payload)
        self.assertTrue(proc.stderr.strip())
        self.assertIn("[args]", proc.stderr)

    def test_cli_unknown_flag_json(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--not-a-real-flag"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("error", payload)
        self.assertTrue(proc.stderr.strip())

    def test_llm_method_reserved(self):
        # Need ≥2 named signals so rules fallback is a concrete level.
        text = (
            "先观望吧不敢追，再等等，轻仓看看市场。"
            "同时也有人割肉离场，心态崩了，再也不碰。"
        ) * 2
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--method", "llm", "--text", text],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["llm_status"], "reserved")
        self.assertIn(payload["data"]["index_level"], sp.INDEX_LEVELS)
        self.assertTrue(proc.stderr.strip())


class PrivacyGuardTests(unittest.TestCase):
    def test_script_has_no_obvious_real_network_names(self):
        # Soft guard: abstract ids only in source skeleton defaults.
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("sentiment-paste-a", src)
        # Real product marketing names should not be hardcoded as defaults.
        for banned in ("discord.com", "x.com/", "xiaohongshu", "xueqiu.com"):
            self.assertNotIn(banned, src.lower())

    def test_non_abstract_source_id_normalized(self):
        sid = sp.normalize_source_id("My Real Trading Room #alpha")
        self.assertEqual(sid, sp.DEFAULT_SOURCE_ID)
        self.assertTrue(sp.ABSTRACT_SOURCE_ID_RE.fullmatch(sid))

    def test_normalize_source_id_stderr_does_not_echo_raw(self):
        """Rejected identities must not appear on stderr diagnostics."""
        import io
        from contextlib import redirect_stderr

        raw = "RealAlphaChat-VIP"
        buf = io.StringIO()
        with redirect_stderr(buf):
            sid = sp.normalize_source_id(raw)
        err = buf.getvalue()
        self.assertEqual(sid, sp.DEFAULT_SOURCE_ID)
        self.assertIn("[privacy]", err)
        self.assertIn("rejected non-abstract source id", err)
        self.assertNotIn(raw, err)
        self.assertNotIn("RealAlphaChat", err)

    def test_abstract_source_ids_preserved(self):
        for good in (
            "sentiment-paste-a",
            "sentiment-room-b",
            "social-a",
            "newsletter-a",
            "research-b",
        ):
            self.assertEqual(sp.normalize_source_id(good), good)

    def test_identity_bearing_fixture_redacted_in_output(self):
        # Realistic identity-bearing paste: channel-ish name + handle + URL + snowflake.
        raw = (
            "---\n"
            "source: RealAlphaChat-VIP\n"
            "observed_at: 2026-07-12\n"
            "---\n\n"
            "@trader_whale 在 https://example-social.test/u/whale 说怕踏空要上车，"
            "频道 id 123456789012345 里也有人割肉离场，心态崩了再也不碰。\n"
        )
        sample = sp.sample_from_text(raw)
        self.assertEqual(sample.source_id, sp.DEFAULT_SOURCE_ID)
        r = sp.classify_rules([sample], as_of=AS_OF)
        data = r.as_dict()
        blob = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("RealAlphaChat-VIP", blob)
        self.assertNotIn("@trader_whale", blob)
        self.assertNotIn("https://example-social.test", blob)
        self.assertNotIn("123456789012345", blob)
        for sid in data["source_ids"]:
            self.assertTrue(sp.ABSTRACT_SOURCE_ID_RE.fullmatch(sid), sid)
        for ev in data["evidence"]:
            self.assertTrue(sp.ABSTRACT_SOURCE_ID_RE.fullmatch(ev["source_id"]))
            self.assertNotIn("@", ev.get("quote_span", ""))
            self.assertNotIn("http", ev.get("quote_span", "").lower())

    def test_cli_rejected_source_id_absent_from_stdout_and_stderr(self):
        """Subprocess regression: --source-id real name must not leak to either stream."""
        identity = "RealAlphaChat-VIP"
        text = (
            "怕踏空要上车，同时有人割肉离场心态崩了再也不碰。" * 2
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--source-id",
                identity,
                "--text",
                text,
                "--as-of",
                AS_OF,
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            check=False,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn(identity, combined)
        self.assertNotIn("RealAlphaChat", combined)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        for sid in payload["data"]["source_ids"]:
            self.assertTrue(sp.ABSTRACT_SOURCE_ID_RE.fullmatch(sid), sid)
        # Diagnostics may note a rejection without echoing the value.
        self.assertIn("[privacy]", proc.stderr)

    def test_parent_dir_non_abstract_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            # Parent dir looks like a real room name — must not leak.
            room = Path(td) / "VIP-Alpha-Room"
            room.mkdir()
            p = room / "sample.md"
            p.write_text(
                "---\nobserved_at: 2026-07-12\n---\n\n"
                "怕踏空上车，同时有人割肉离场心态崩了再也不碰。\n",
                encoding="utf-8",
            )
            samples = sp.load_samples_from_path(p)
            self.assertEqual(samples[0].source_id, sp.DEFAULT_SOURCE_ID)

    def test_redact_text_strips_handles_urls_ids(self):
        text = "见 @alice 与 https://foo.test/x 以及 998877665544 id"
        out = sp.redact_text(text)
        self.assertNotIn("@alice", out)
        self.assertNotIn("https://foo.test", out)
        self.assertNotIn("998877665544", out)
        self.assertIn("[REDACTED]", out)


if __name__ == "__main__":
    unittest.main()
