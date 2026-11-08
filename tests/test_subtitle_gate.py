"""字幕闸门 / 章节锚点的纯函数测试（fixture 驱动，不联网、不读写用户目录）"""

import importlib.util
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "douyin_analyzer.py")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def _load_module():
    spec = importlib.util.spec_from_file_location("douyin_analyzer_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


analyzer = _load_module()


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def detail_with_captions(duration_ms, segments, chapters=None):
    detail = {
        "aweme_id": "1",
        "aweme_type": 0,
        "video": {"duration": duration_ms, "cla_info": {"caption_infos": segments}},
    }
    if chapters:
        detail["chapter_list"] = chapters
    return detail


class SubtitleGateTest(unittest.TestCase):
    def test_hit(self):
        info = analyzer.build_video_info("1", load_fixture("subtitle_hit.json"))
        self.assertTrue(info["subtitle_usable"])
        stats = info["subtitle_stats"]
        self.assertEqual(stats["unit"], "ms")
        self.assertGreaterEqual(stats["coverage"], 0.9)
        self.assertGreaterEqual(stats["density"], 2.0)
        self.assertEqual(len(info["chapters"]), 3)
        self.assertEqual(info["chapter_anchors"][1], "00:07 方法：把流程拆成小步")
        self.assertEqual(info["chapters"][-1]["end_ms"], info["duration_ms"])

    def test_partial_tail_is_rejected_with_reason(self):
        info = analyzer.build_video_info("1", load_fixture("subtitle_partial.json"))
        self.assertFalse(info["subtitle_usable"])
        stats = info["subtitle_stats"]
        self.assertIn("尾部截断", stats["reason"])
        self.assertAlmostEqual(stats["coverage"], 0.36, places=2)
        self.assertGreater(stats["last_end_raw"], 0)

    def test_no_chapters_returns_empty_anchors(self):
        info = analyzer.build_video_info("1", load_fixture("no_chapters.json"))
        self.assertTrue(info["subtitle_usable"])
        self.assertEqual(info["chapters"], [])
        self.assertEqual(info["chapter_anchors"], [])
        self.assertEqual(len(analyzer.assemble_subtitle_text(info).splitlines()), 4)

    def test_seconds_unit_fallback(self):
        segs = [
            {"start_time": 0, "end_time": 9, "text": "秒为单位的字幕第一段内容要写得足够长一些"},
            {"start_time": 9, "end_time": 19, "text": "秒为单位的字幕最后一段内容也要写得足够长"},
        ]
        info = analyzer.build_video_info("1", detail_with_captions(20000, segs))
        self.assertEqual(info["subtitle_stats"]["unit"], "s")
        self.assertTrue(info["subtitle_usable"])
        self.assertEqual(info["subtitle_stats"]["last_end"], 19000)

    def test_overlap_truncates_previous_segment(self):
        segs = [
            {"start_time": 0, "end_time": 5000, "text": "前段字幕文本内容写得足够长一些"},
            {"start_time": 4000, "end_time": 9000, "text": "后段字幕文本内容也写得足够长一些"},
            {"start_time": 9000, "end_time": 19000, "text": "结尾字幕文本内容同样要写得足够长一些"},
        ]
        info = analyzer.build_video_info("1", detail_with_captions(20000, segs))
        self.assertTrue(info["subtitle_usable"])
        self.assertEqual(info["subtitle_segments"][0]["end_ms"], 4000)
        self.assertEqual(len(info["subtitle_segments"]), 3)

    def test_fully_covered_segment_is_dropped(self):
        segs = [
            {"start_time": 0, "end_time": 9000, "text": "被完全覆盖的短段落内容写得足够长一些"},
            {"start_time": 0, "end_time": 9000, "text": "覆盖前段的较长段落内容写得足够长一些而且信息更完整"},
            {"start_time": 9000, "end_time": 19000, "text": "结尾字幕文本内容同样要写得足够长一些"},
        ]
        info = analyzer.build_video_info("1", detail_with_captions(20000, segs))
        texts = [s["text"] for s in info["subtitle_segments"]]
        self.assertEqual(len(texts), 2)
        self.assertIn("覆盖前段的较长段落内容写得足够长一些而且信息更完整", texts)

    def test_missing_caption_infos_reason_is_visible(self):
        info = analyzer.build_video_info("1", {"aweme_id": "1", "video": {"duration": 1029382}})
        self.assertFalse(info["subtitle_usable"])
        self.assertIn("无 caption_infos", info["subtitle_stats"]["reason"])
        self.assertEqual(info["chapters"], [])

    def test_low_density_is_rejected(self):
        segs = [{"start_time": 0, "end_time": 19000, "text": "字"}]
        info = analyzer.build_video_info("1", detail_with_captions(20000, segs))
        self.assertFalse(info["subtitle_usable"])
        self.assertIn("密度过低", info["subtitle_stats"]["reason"])


class ChapterTest(unittest.TestCase):
    def test_chapter_anchor_format(self):
        info = analyzer.build_video_info("1", load_fixture("subtitle_hit.json"))
        for anchor in info["chapter_anchors"]:
            self.assertRegex(anchor, r"^\d{2}:\d{2} .+")
        self.assertEqual(info["chapters"][0]["start_ms"], 0)

    def test_mmss_formats_over_an_hour(self):
        self.assertEqual(analyzer.format_mmss(372724), "06:12")
        self.assertEqual(analyzer.format_mmss(3600000), "60:00")

    def test_untitled_chapter_keeps_bare_timecode(self):
        info = analyzer.build_video_info("1", load_fixture("chapter_untitled.json"))
        self.assertEqual(info["chapter_anchors"][0], "00:00")
        self.assertTrue(info["chapters"][0]["untitled"])
        self.assertEqual(info["chapter_anchors"][1], "00:10 方法：把流程拆成小步")
        types = [w["type"] for w in info["chapter_stats"]["warnings"]]
        self.assertIn("untitled", types)
        self.assertEqual(info["chapter_stats"]["dropped"], [])

    def test_missing_timestamp_is_dropped_with_reason(self):
        info = analyzer.build_video_info("1", load_fixture("chapter_missing_ts.json"))
        stats = info["chapter_stats"]
        self.assertEqual(stats["raw_count"], 2)
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(len(stats["dropped"]), 1)
        self.assertEqual(stats["dropped"][0]["desc"], "风险：预算被临时砍掉")
        self.assertIn("无有效时间戳", stats["dropped"][0]["reason"])

    def test_detail_fallback_title_is_truncated(self):
        long_detail = "这是一段很长的章节说明文字用来测试截断行为是否生效并且超过二十个字"
        detail = detail_with_captions(20000, [], chapters=[
            {"desc": "", "detail": long_detail, "timestamp": 0},
        ])
        info = analyzer.build_video_info("1", detail)
        title = info["chapters"][0]["title"]
        self.assertEqual(title, long_detail[:20] + "…")
        self.assertEqual(len(title), 21)
        self.assertTrue(info["chapter_anchors"][0].startswith("00:00 " + long_detail[:20]))
        types = [w["type"] for w in info["chapter_stats"]["warnings"]]
        self.assertIn("title_truncated", types)

    def test_detail_fallback_long_title_fixture_is_truncated(self):
        info = analyzer.build_video_info("1", load_fixture("chapter_detail_fallback.json"))
        expected = "这一段虚构说明文字稍长一些用来覆盖截断逻…"
        self.assertEqual(info["chapters"][0]["title"], expected)
        self.assertEqual(len(info["chapters"][0]["title"]), 21)
        self.assertEqual(info["chapter_anchors"][0], "00:05 " + expected)
        types = [w["type"] for w in info["chapter_stats"]["warnings"]]
        self.assertIn("title_truncated", types)

    def test_duplicate_timestamp_kept_in_order(self):
        detail = detail_with_captions(20000, [], chapters=[
            {"desc": "第一条", "timestamp": 5000},
            {"desc": "第二条", "timestamp": 5000},
        ])
        info = analyzer.build_video_info("1", detail)
        self.assertEqual([c["title"] for c in info["chapters"]], ["第一条", "第二条"])
        types = [w["type"] for w in info["chapter_stats"]["warnings"]]
        self.assertIn("duplicate_timestamp", types)


if __name__ == "__main__":
    unittest.main(verbosity=2)
