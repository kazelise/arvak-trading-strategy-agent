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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "sentiment_panel.py"


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
        r = sp.classify_rules([])
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIsNotNone(r.escape_reason)
        self.assertEqual(r.sample_quality, "不可用")
        self.assertEqual(r.dominant_mode, "信息不足")

    def test_too_short_sample(self):
        samples = [sp.Sample("sentiment-paste-a", "短", "2026-07-12")]
        r = sp.classify_rules(samples)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIn("过短", r.escape_reason or "")

    def test_long_but_no_pattern(self):
        text = "今天天气不错，大家讨论了午餐吃什么。" * 5
        samples = [sp.Sample("sentiment-paste-a", text, "2026-07-12")]
        r = sp.classify_rules(samples)
        self.assertEqual(r.index_level, "信息不足以分级")
        self.assertIsNotNone(r.escape_reason)


class BoundaryClassificationTests(unittest.TestCase):
    def test_strong_fud_maps_to_ice_or_low(self):
        text = (
            "彻底割肉了，再也不碰，心态崩了，市场没救了，永久退出。"
            "又爆仓了，恐慌盘出不来，血亏清仓跑。"
            "归零风险太大，泡沫破裂闪崩，FUD 满天飞，投降了。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, "2026-07-12")])
        self.assertIn(r.index_level, {"冰点", "低迷"})
        self.assertEqual(r.dominant_mode, "FUD")
        self.assertGreaterEqual(len(r.evidence), 1)

    def test_strong_fomo_maps_to_hot_or_mania(self):
        text = (
            "怕踏空了赶紧上车，干就完了 all in 加杠杆梭哈。"
            "不看估值要起飞，人生翻身财富自由，moon 了。"
            "排队入金狂飙，疯了一样买，必涨叙事。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, "2026-07-12")])
        self.assertIn(r.index_level, {"亢奋", "狂热"})
        self.assertEqual(r.dominant_mode, "FOMO")

    def test_mixed_signals_map_to_neutral_or_escape(self):
        text = (
            "一边有人割肉投降说再也不碰，"
            "一边有人怕踏空要上车追涨，"
            "还有人说先观望不敢追，再等等。"
            "多空都很吵，谁也没压过谁。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, "2026-07-12")])
        # Mixed → 中性 is preferred; escape only if signal math fails.
        self.assertIn(r.index_level, {"中性", "信息不足以分级", "低迷", "亢奋"})
        data = r.as_dict()
        self.assertIn(data["index_level"], sp.INDEX_LEVELS)

    def test_watch_only_not_extreme(self):
        text = (
            "今天先观望吧，不敢追，再等等，轻仓看看，空仓等待，静观其变。"
            "没有人喊必涨，也没有人说归零。"
        )
        r = sp.classify_rules([sp.Sample("sentiment-paste-a", text, "2026-07-12")])
        self.assertIn(r.index_level, {"中性", "低迷"})
        self.assertNotIn(r.index_level, {"冰点", "狂热"})


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
                "割肉离场了，心态崩了，再也不碰。" * 3,
                encoding="utf-8",
            )
            samples = sp.load_samples_from_path(p, source_id="sentiment-paste-a")
            self.assertEqual(len(samples), 1)
            r = sp.classify_rules(samples)
            self.assertIn(r.index_level, sp.INDEX_LEVELS)

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

    def test_llm_method_reserved(self):
        text = "先观望吧不敢追，再等等，轻仓看看市场。" * 3
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


class PrivacyGuardTests(unittest.TestCase):
    def test_script_has_no_obvious_real_network_names(self):
        # Soft guard: abstract ids only in source skeleton defaults.
        src = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("sentiment-paste-a", src)
        # Real product marketing names should not be hardcoded as defaults.
        for banned in ("discord.com", "x.com/", "xiaohongshu", "xueqiu.com"):
            self.assertNotIn(banned, src.lower())


if __name__ == "__main__":
    unittest.main()
