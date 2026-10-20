import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xwlb_build as xb

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = Path(__file__).resolve().parents[2]


class TestParseDayIndex(unittest.TestCase):
    def test_day_20261020(self):
        html_text = (FIXTURES / "xwlb_day_20261020.html").read_text(encoding="utf-8")
        items = xb.parse_day_index(html_text, date(2026, 10, 20))
        titles = [i["title"] for i in items]
        # Full-episode player entry plus every segment; URLs deduped.
        self.assertIn("《新闻联播》 20261020 19:00", titles)
        self.assertIn("李强会见伊朗第一副总统", titles)
        self.assertEqual(len(items), len({i["url"] for i in items}))
        for it in items:
            self.assertTrue(it["url"].startswith("https://tv.cctv.com/2026/10/20/"))
            self.assertFalse(it["title"].startswith("[视频]"))


class TestParseSegment(unittest.TestCase):
    def test_segment_20261020(self):
        html_text = (
            FIXTURES / "xwlb_segment_20261020.html"
        ).read_text(encoding="utf-8")
        title, content = xb.parse_segment(html_text, "fallback")
        self.assertEqual(title, "李强会见伊朗第一副总统")
        self.assertIn("央视网消息", content)
        self.assertIn("伊朗第一副总统", content)
        self.assertNotIn("京ICP备", content)
        self.assertGreater(len(content), 200)


class TestClassify(unittest.TestCase):
    def test_known_titles(self):
        cases = [
            ("国内联播快讯", "brief"),
            ("国际联播快讯", "brief"),
            ("《求是》杂志发表习近平总书记重要文章", "article"),
            ("央视快评：持续加强政治培训", "coverage"),
            ("【赓续长征精神】传承红色血脉", "coverage"),
            ("习近平签署主席令", "decree"),
            ("革命同志逝世", "funeral"),
            ("习近平致电祝贺", "message"),
            ("习近平将出席金砖国家峰会", "ceremony"),  # 出席+峰会
            ("斯洛伐克总统将对中国进行国事访问", "visit_to_china"),
            ("习近平将对塞尔维亚进行国事访问并赴美出席核安全峰会", "foreign_trip"),
            ("李强同德国总理通电话", "phone_call"),
            ("王毅同美国国务卿会谈", "talks"),
            ("李强主持召开国务院常务会议", "chaired_meeting"),
            ("丁薛祥在河北调研", "inspection"),
            ("全国政协召开专题协商会", "deliberation"),
            ("习近平举行仪式欢迎", "ceremony"),
            ("习近平在开幕式上发表讲话", "speech"),
            ("上海合作组织成员国理事会第二十五次会议在京举行", "symposium"),
            ("习近平会见越南总理", "meeting"),
        ]
        for title, expected in cases:
            self.assertEqual(xb.classify_type(title, ""), expected, title)


class TestCounterpart(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(
            xb.extract_counterpart("李强会见伊朗第一副总统"), "伊朗第一副总统"
        )
        self.assertEqual(
            xb.extract_counterpart("习近平同巴西总统通电话"), "巴西总统"
        )
        self.assertEqual(xb.extract_counterpart("国内联播快讯"), "")


class TestTagging(unittest.TestCase):
    def test_boilerplate_exclusion(self):
        self.assertFalse(xb.has_real_mention("习近平新时代中国特色社会主义思想", "习近平"))
        self.assertTrue(xb.has_real_mention("习近平出席开幕会", "习近平"))

    def test_actor_vs_mention(self):
        leaders = json.loads(
            (REPO / "docs" / "data" / "leaders.json").read_text(encoding="utf-8")
        )
        record = {
            "title_zh": "李强会见伊朗第一副总统",
            "content_zh": "习近平主席就维护和促进中东和平稳定提出一系列重大倡议。",
        }
        xb.tag_leaders(record, leaders)
        self.assertIn("li-qiang", record["leaders"])
        self.assertNotIn("li-qiang", record["mentions"])
        self.assertIn("xi-jinping", record["mentions"])


class TestMergeBuild(unittest.TestCase):
    def _seed_published(self, out: Path) -> None:
        (out / "index.json").write_text(json.dumps({
            "columns": xb.INDEX_COLUMNS,
            "types": {"meeting": "Meeting with guests", "other": "Other"},
            "events": [
                ["20260802-00", "2026-08-02", "article", 0, "Old headline",
                 "Old Headline EN", "Old summary.", "", "", "", ""],
            ],
        }, ensure_ascii=False), encoding="utf-8")
        (out / "events-2026.json").write_text(json.dumps({
            "20260802-00": {"content_zh": "old content", "url": "https://old"},
        }, ensure_ascii=False), encoding="utf-8")
        (out / "meta.json").write_text(json.dumps({
            "built_at": "2026-08-03T01:01:15+00:00",
            "first_date": "2016-02-04",
            "last_date": "2026-08-02",
            "n_events": 1, "n_translated": 1, "n_days": 1, "years": ["2026"],
        }), encoding="utf-8")

    def test_merge_preserves_old_rows(self):
        records = [{
            "id": "20260803-00", "date": "2026-08-03",
            "title_zh": "李强会见测试客人",
            "url": "https://tv.cctv.com/x.shtml",
            "content_zh": "new content", "type": "meeting",
            "counterpart": "测试客人", "location": "",
            "leaders": ["li-qiang"], "mentions": [],
        }]
        translations = {
            "20260803-00": {"id": "20260803-00", "title_en": "Li Meets Test Guest",
                            "summary_en": "Li met a guest."},
        }
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self._seed_published(out)
            xb.build_outputs(records, translations, out)
            index = json.loads((out / "index.json").read_text(encoding="utf-8"))
            rows = {r[0]: r for r in index["events"]}
            self.assertIn("20260802-00", rows)
            self.assertIn("20260803-00", rows)
            self.assertEqual(rows["20260803-00"][5], "Li Meets Test Guest")
            self.assertEqual(rows["20260803-00"][3], 1)  # meeting is active
            self.assertEqual(rows["20260803-00"][9], "测试客人")
            self.assertEqual(index["types"]["meeting"], "Meeting with guests")
            shard = json.loads((out / "events-2026.json").read_text(encoding="utf-8"))
            self.assertIn("20260802-00", shard)
            self.assertIn("20260803-00", shard)
            self.assertEqual(shard["20260803-00"]["url"],
                             "https://tv.cctv.com/x.shtml")
            meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["n_events"], 2)
            self.assertEqual(meta["n_translated"], 2)
            self.assertEqual(meta["first_date"], "2026-08-02")  # min over merged rows
            self.assertEqual(meta["last_date"], "2026-08-03")

    def test_untranslated_row_is_refilled(self):
        records = [{
            "id": "20260803-00", "date": "2026-08-03", "title_zh": "t",
            "url": "u", "content_zh": "c", "type": "brief",
            "counterpart": "", "location": "", "leaders": [], "mentions": [],
        }]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            xb.build_outputs(records, {}, out)          # published untranslated
            xb.build_outputs(records, {
                "20260803-00": {"id": "20260803-00", "title_en": "Late EN",
                                "summary_en": "Late summary."},
            }, out)                                      # translation lands later
            rows = {r[0]: r for r in json.loads(
                (out / "index.json").read_text(encoding="utf-8"))["events"]}
            self.assertEqual(rows["20260803-00"][5], "Late EN")
            self.assertEqual(rows["20260803-00"][6], "Late summary.")
            self.assertEqual(rows["20260803-00"][4], "t")  # zh untouched

    def test_idempotent(self):
        records = [{
            "id": "20260803-00", "date": "2026-08-03", "title_zh": "t",
            "url": "u", "content_zh": "c", "type": "brief",
            "counterpart": "", "location": "", "leaders": [], "mentions": [],
        }]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            xb.build_outputs(records, {}, out)
            n1 = json.loads((out / "meta.json").read_text())["n_events"]
            xb.build_outputs(records, {}, out)
            n2 = json.loads((out / "meta.json").read_text())["n_events"]
            self.assertEqual(n1, n2)

    def test_default_since_follows_published(self):
        self.assertEqual(
            xb.default_since({"last_date": "2026-08-02"}), "2026-08-03"
        )
        self.assertEqual(xb.default_since({}), xb.DEFAULT_SINCE)


class TestState(unittest.TestCase):
    def test_jsonl_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "records.jsonl"
            xb.save_jsonl(p, [{"id": "a", "x": 1}])
            self.assertEqual(xb.load_jsonl(p), [{"id": "a", "x": 1}])

    def test_plan_missing_dates(self):
        now = datetime.now(timezone.utc)
        since, until = date(2026, 8, 3), date(2026, 8, 6)
        known = {"2026-08-04"}
        checked = {"2026-08-05": (now - timedelta(days=2)).isoformat()}  # fresh
        missing = xb.plan_missing_dates(since, until, known, checked, 7, 10)
        self.assertEqual(
            [d.isoformat() for d in missing], ["2026-08-03", "2026-08-06"]
        )


class TestMainNoNetwork(unittest.TestCase):
    def test_dry_run_rebuilds_from_records(self):
        with tempfile.TemporaryDirectory() as td:
            records_dir = Path(td) / "records"
            records_dir.mkdir()
            out_dir = Path(td) / "out"
            out_dir.mkdir()
            (records_dir / "records.jsonl").write_text(json.dumps({
                "id": "20260803-00", "date": "2026-08-03",
                "title_zh": "李强会见测试客人",
                "url": "u", "content_zh": "c", "type": "meeting",
                "counterpart": "", "location": "",
                "leaders": [], "mentions": [],
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            rc = xb.main([
                "--dry-run", "--records-dir", str(records_dir),
                "--out-dir", str(out_dir),
            ])
            self.assertEqual(rc, 0)
            meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["n_events"], 1)
            self.assertEqual(meta["last_date"], "2026-08-03")


if __name__ == "__main__":
    unittest.main()
