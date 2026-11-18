"""分析产品的业务级测试：负荷切换、共享变送器、候选解释与现场结果。"""

import datetime
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from steamcheck import anomaly, decisions, inspection, sources
from steamcheck.engine import analyse
from steamcheck.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[1]
TZ = datetime.timezone(datetime.timedelta(hours=8))


def at(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text)


class LoadSwitchTests(unittest.TestCase):
    """负荷切换不能被当成多只表同时坏掉。"""

    @classmethod
    def setUpClass(cls):
        cls.snapshot = analyse(ROOT)
        cls.data = cls.snapshot.as_dict()

    def test_naive_control_chart_flags_regime_switch(self):
        # 单测点控制线在档位切换当天会同时报多只表。
        naive = self.data["naive"]
        day = "2025-01-24"
        flagged = [
            meter
            for meter, rows in naive.items()
            if any(row["day"] == day and abs(row["z"]) > 3 for row in rows)
        ]
        self.assertGreaterEqual(len(flagged), 4)

    def test_adjusted_evidence_has_single_episode(self):
        episodes = self.data["episodes"]
        self.assertEqual(len(episodes), 1)
        episode = episodes[0]
        self.assertEqual(episode["transmitter"], "T1")
        self.assertEqual(episode["boundary"], "HP")
        # 台阶起点必须在档位切换之后，不是切换当天。
        self.assertGreater(episode["start"], "2025-01-24")

    def test_regime_runs_are_separated(self):
        self.assertEqual([r["regime"] for r in self.data["regimes"]], ["normal", "high"])

    def test_reference_days_cover_both_regimes(self):
        self.assertIn("2025-01-06", self.data["reference_days"])
        self.assertIn("2025-01-24", self.data["reference_days"])


class SharedTransmitterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = analyse(ROOT).as_dict()
        cls.episode = cls.data["episodes"][0]

    def test_two_meters_move_together(self):
        shifts = self.episode["per_meter_shift"]
        self.assertEqual(set(shifts), {"M1", "M2"})
        self.assertGreater(shifts["M1"], 3)
        self.assertGreater(shifts["M2"], 3)
        self.assertLess(abs(shifts["M1"] - shifts["M2"]), 2.0)

    def test_transmitter_hypothesis_is_kept(self):
        by_kind = {h["kind"]: h for h in self.episode["hypotheses"]}
        self.assertEqual(by_kind["transmitter-bias"]["fit"], "possible")
        self.assertEqual(by_kind["transmitter-bias"]["covers"], ["M1", "M2"])

    def test_single_meter_hypotheses_are_contradicted_not_deleted(self):
        meters = [h for h in self.episode["hypotheses"] if h["kind"] == "meter-bias"]
        self.assertEqual({h["label"] for h in meters}, {"M1 单表偏移", "M2 单表偏移"})
        self.assertTrue(all(h["fit"] == "contradicted" for h in meters))

    def test_balance_does_not_account_for_the_step(self):
        # 守恒量只变化了 1.37 t，远小于 4.92 t 的读数台阶。
        self.assertLess(abs(self.episode["balance_delta"]), abs(self.episode["shift"]) / 2)

    def test_alternatives_remain(self):
        kinds = {h["kind"] for h in self.episode["hypotheses"] if h["fit"] == "possible"}
        self.assertIn("reallocation", kinds)
        self.assertIn("flow-unknown", kinds)
        self.assertGreaterEqual(len(kinds), 3, "不得强行指定唯一故障")


class KnownAtTests(unittest.TestCase):
    """事后确认的事实不能用来评价更早的告警。"""

    def test_operation_note_not_visible_before_known_at(self):
        before = analyse(ROOT, at("2025-02-03T08:00:00+08:00")).as_dict()
        labels = [
            h["label"]
            for episode in before["episodes"]
            for h in episode["hypotheses"]
            if h["kind"] == "operation"
        ]
        self.assertEqual(labels, [])

        after = analyse(ROOT, at("2025-02-04T08:00:00+08:00")).as_dict()
        labels = [
            h["label"]
            for episode in after["episodes"]
            for h in episode["hypotheses"]
            if h["kind"] == "operation"
        ]
        self.assertEqual(labels, ["已确认放空 N1"])

    def test_replacement_not_visible_before_known_at(self):
        before = analyse(ROOT, at("2025-02-02T08:00:00+08:00")).as_dict()
        self.assertFalse(
            any(h["kind"] == "replacement" for e in before["episodes"] for h in e["hypotheses"])
        )

    def test_result_not_visible_before_known_at(self):
        before = analyse(ROOT, at("2025-02-11T08:00:00+08:00")).as_dict()
        self.assertIsNone(before["episodes"][0]["resolved"])
        after = analyse(ROOT, at("2025-02-13T08:00:00+08:00")).as_dict()
        self.assertEqual(after["episodes"][0]["resolved"]["status"], "bias-confirmed")

    def test_windows_are_untouched_before_results(self):
        before = analyse(ROOT, at("2025-02-11T08:00:00+08:00")).as_dict()
        self.assertEqual(before["usage"]["W1"]["slots_used"], 0)
        self.assertEqual(before["usage"]["W1"]["cost_used"], 0)


class ImpactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = analyse(ROOT, at("2025-02-11T08:00:00+08:00")).as_dict()
        cls.after = analyse(ROOT, at("2025-02-13T08:00:00+08:00")).as_dict()

    def test_hp_intensity_is_over_limit(self):
        hp = next(i for i in self.before["impacts"] if i["boundary"] == "HP")
        self.assertEqual(hp["measured_verdict"], "over")
        self.assertTrue(hp["threshold_invariant"], "阈值判断不应随解释改变")

    def test_bias_correction_changes_intensity_not_verdict(self):
        hp_before = next(i for i in self.before["impacts"] if i["boundary"] == "HP")
        hp_after = next(i for i in self.after["impacts"] if i["boundary"] == "HP")
        self.assertLess(hp_after["corrected"], hp_before["corrected"])
        self.assertEqual(hp_after["measured_verdict"], "over")
        self.assertEqual(hp_after["attribution"], "resolved")

    def test_what_changes_is_attribution(self):
        hp = next(i for i in self.before["impacts"] if i["boundary"] == "HP")
        self.assertGreater(len(hp["actions"]), 1)
        self.assertIn("correct-measurement", hp["actions"])

    def test_clarity_separates_numeric_from_decisive(self):
        uncertain = self.before["clarity"]["uncertain"]
        self.assertEqual([item["episode"] for item in uncertain], ["E1-T1-2025-01-31"])

    def test_undone_result_does_not_clear_episode(self):
        after = analyse(ROOT, at("2025-02-14T08:00:00+08:00")).as_dict()
        episode = after["episodes"][0]
        # R1 已确认，R2 未完成；未完成不改变已确认结论。
        self.assertEqual(episode["resolved"]["status"], "bias-confirmed")
        self.assertIn("I3（变送器 T3）未完成", " ".join(after["caveats"]))
        usage = after["usage"]["W2"]
        self.assertEqual(usage["slots_used"], 1)
        self.assertEqual(usage["cost_used"], 1600.0)


class RankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = analyse(ROOT, at("2025-02-11T08:00:00+08:00")).as_dict()

    def test_one_check_covers_both_meters(self):
        chosen = self.data["candidates"][0]
        self.assertEqual(chosen["transmitter"], "T1")
        self.assertEqual(chosen["covers"], ["M1", "M2"])

    def test_same_transmitter_not_treated_as_two_tasks(self):
        # I1 与 I4 覆盖同一变送器；结果里只应出现一个 T1 检查。
        t1 = [c for c in self.data["candidates"] if c["transmitter"] == "T1"]
        self.assertEqual(len(t1), 1)

    def test_ranked_by_value_not_by_alarm_count(self):
        chosen = self.data["candidates"][0]
        self.assertGreater(chosen["marginal_value"], 0)
        self.assertTrue(any("错误决定代价" in r for r in chosen["reason"]))

    def test_unselected_is_not_called_normal(self):
        coverage = self.data["coverage"]
        self.assertTrue(coverage["note"])
        joined = " ".join(self.data["caveats"])
        self.assertIn("不得视为正常", joined)

    def test_budget_can_force_unresolved_conclusion(self):
        # 把窗口预算压到买不起任何检查，必须明确保留未决结论。
        with tempfile.TemporaryDirectory() as folder:
            root = _copy_pack(Path(folder))
            windows = root / "data/windows.csv"
            text = windows.read_text(encoding="utf-8").splitlines()
            out = [text[0]]
            for line in text[1:]:
                parts = line.split(",")
                parts[2] = "100"  # budget
                out.append(",".join(parts))
            windows.write_text("\n".join(out) + "\n", encoding="utf-8")
            _rehash(root, "windows")
            data = analyse(root, at("2025-02-11T08:00:00+08:00")).as_dict()
        self.assertEqual(data["candidates"], [])
        self.assertIn("E1-T1-2025-01-31", data["coverage"]["cannot_safely_conclude"])
        self.assertTrue(any("不足以区分关键解释" in c for c in data["caveats"]))


class SourceTests(unittest.TestCase):
    def test_loader_reads_all_tables(self):
        loaded = sources.load(ROOT)
        self.assertEqual(len(loaded.meters), 6)
        self.assertEqual(len(loaded.readings), 288)
        self.assertEqual(loaded.transmitter_of("M2"), "T1")

    def test_replacement_changes_contract_only_after_effective(self):
        loaded = sources.load(ROOT)
        before = loaded.contracts_at("M6", at("2025-02-02T08:00:00+08:00"))
        after = loaded.contracts_at("M6", at("2025-02-04T08:00:00+08:00"))
        self.assertEqual(before, (200.0, 2.2))
        self.assertEqual(after, (140.0, 1.1))

    def test_proportional_load_ratio_is_not_polluted_by_single_transmitter(self):
        loaded = sources.load(ROOT)
        as_of = at("2025-02-11T08:00:00+08:00")
        days = anomaly.build_days(loaded, as_of)
        ref = anomaly.reference_days(days)
        baselines, _ = anomaly.build_baselines(loaded, days, ref)
        with_t1 = anomaly.load_ratio(days[-1], baselines, excluded=set())
        without_t1 = anomaly.load_ratio(days[-1], baselines, excluded={"T1"})
        # T1 的 M1/M2 同时抬高，若按测点平均会把负荷指数抬高约 4%；
        # 用中位数后，单台变送器最多影响一个测点份额。
        self.assertLess(abs(with_t1 - without_t1), 0.035)
        self.assertAlmostEqual(anomaly.load_ratio(
            days[-1], baselines, excluded={"T1", "T2", "T3"}), 1.0)


class InterfaceTests(unittest.TestCase):
    def test_cli_summary_runs(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--root", str(ROOT), "--summary"])
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("复核建议", text)
        self.assertIn("变送器 T1 共享加法偏移", text)

    def test_server_serves_snapshot(self):
        from steamcheck.serve import handler_factory

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(ROOT))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(base + "/api/snapshot") as response:
                payload = json.loads(response.read())
            self.assertEqual(payload["episodes"][0]["transmitter"], "T1")
            with urllib.request.urlopen(base + "/") as response:
                html = response.read().decode("utf-8")
            self.assertIn("复核优先级", html)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + "/api/snapshot?as_of=bad")
            self.assertEqual(caught.exception.code, 400)
        finally:
            server.shutdown()


def _copy_pack(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    (target / "data").mkdir(exist_ok=True)
    for path in (ROOT / "data").iterdir():
        (target / "data" / path.name).write_bytes(path.read_bytes())
    return target


def _rehash(root: Path, table: str) -> None:
    import hashlib

    catalog_path = root / "data/catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    for entry in catalog["tables"]:
        if entry["name"] == table:
            path = root / entry["path"]
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
