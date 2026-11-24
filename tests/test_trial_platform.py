"""持续试验平台的单元与演练测试。

覆盖：

- 资料完整性检查把问题记录为可降级原因；
- 重复请求、设备切换、互斥、停用与接管；
- 成熟度、提前/逾期答卷、缺失答卷不剔除；
- 推广门槛的双条件、负责人确认、部分接管；
- 快照只使用当时已知资料；
- 可重复随机合成校验的错误推广控制与识别能力；
- HTTP 服务入口。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trial_platform import inference
from trial_platform.engine import Engine, EngineError
from trial_platform.model import load_bundle, parse_scope
from trial_platform.simulation import alternative_scenario, null_scenario, run_trial, run_suite

UTC8 = timezone(timedelta(hours=8))
FIXTURE_ROOT = Path(__file__).resolve().parents[1]


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


class EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 可控时钟：运行时记录的“当时可知”必须落在演练时点，而不是测试机器当前时间。
        self.clock_now = at("2026-03-02T08:00:00+08:00")
        self.engine = Engine(
            root=FIXTURE_ROOT,
            state_dir=Path(self.tmp.name) / "state",
            clock=lambda: self.clock_now,
        )

    def tick(self, when: str) -> None:
        self.clock_now = at(when)

    # -- 登记与分配 -----------------------------------------------------
    def register(self, **overrides) -> dict:
        body = {
            "experiment_id": "trial",
            "exclusive_scope": "label",
            "start_at": "2026-03-01T00:00:00+08:00",
            "end_at": "2026-03-31T00:00:00+08:00",
            "followup_hours": 48,
            "minimum_gain": 0.04,
            "maximum_false_certainty": 0.12,
            "look_policy": "持续查看",
            "arms": ["base", "candidate"],
            "approved_leads": ["lead-a"],
            "reference_arm": "base",
            "planned_scopes": ["trial:t1"],
        }
        body.update(overrides)
        return self.engine.register_experiment(body)

    def register_policy(self, weights=None, effective_at="2026-03-01T00:00:00+08:00") -> dict:
        return self.engine.register_policy(
            "trial",
            {
                "policy_id": "p1",
                "weights": weights or {"base": 0.5, "candidate": 0.5},
                "effective_at": effective_at,
            },
        )

    def register_household(self, household_id: str, devices=("dev-a", "dev-b")) -> None:
        self.engine.register_household(
            {
                "household_id": household_id,
                "segment": "trial",
                "region": "r1",
                "devices": list(devices),
                "eligible_from": "2026-03-01T00:00:00+08:00",
            }
        )
    def access(
        self,
        household_id: str,
        request_key: str,
        when: str = "2026-03-02T09:00:00+08:00",
        device=None,
        channel="t1",
    ):
        return self.engine.access(
            {
                "household_id": household_id,
                "experiment_id": "trial",
                "request_key": request_key,
                "at": when,
                "channel": channel,
                "device": device,
            }
        )

    def test_duplicate_request_and_device_switch_return_same_assignment(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        first = self.access("hh1", "visit-1")
        repeat = self.access("hh1", "visit-1", when="2026-03-02T09:30:00+08:00")
        switched = self.access("hh1", "visit-2", device="dev-b", when="2026-03-02T10:00:00+08:00")
        self.assertEqual(first.status, "assigned")
        self.assertEqual(repeat.status, "duplicate_assignment")
        self.assertEqual(switched.status, "duplicate_household")
        ids = {first.assignment.id, repeat.assignment.id, switched.assignment.id}
        self.assertEqual(len(ids), 1)
        self.assertEqual(len(self.engine.assignments()), 1 + 360)  # 资料 360 条 + 1 条新增

    def test_exclusive_scope_blocks_second_experiment(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        first = self.access("hh1", "visit-1")
        self.assertEqual(first.status, "assigned")
        self.engine.register_experiment(
            {
                "experiment_id": "trial2",
                "exclusive_scope": "label",
                "start_at": "2026-03-01T00:00:00+08:00",
                "end_at": "2026-03-31T00:00:00+08:00",
                "followup_hours": 48,
                "minimum_gain": 0.04,
                "maximum_false_certainty": 0.12,
                "look_policy": "持续查看",
                "arms": ["base", "other"],
                "approved_leads": ["lead-a"],
            }
        )
        self.engine.register_policy(
            "trial2",
            {"policy_id": "p2", "weights": {"base": 0.5, "other": 0.5}, "effective_at": "2026-03-01T00:00:00+08:00"},
        )
        blocked = self.engine.access(
            {
                "household_id": "hh1",
                "experiment_id": "trial2",
                "request_key": "visit-x",
                "at": "2026-03-03T09:00:00+08:00",
                "channel": "t1",
                "device": "dev-b",
            }
        )
        self.assertEqual(blocked.status, "refused")
        self.assertIn("互斥范围", blocked.reason)

    def test_probability_and_reason_are_auditable(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        self.access("hh1", "visit-1")
        log = self.engine.access_log("trial")
        self.assertEqual(len(log), 1)
        runtime = [t for t in self.engine.assignments().values() if t.origin == "runtime"]
        self.assertEqual(len(runtime), 1)
        trial = runtime[0]
        self.assertEqual(trial.assignment.probability, 0.5)
        self.assertEqual(trial.assignment.policy_id, "p1")
        self.assertIn("p1", trial.enrollment_reason)
        self.assertEqual(trial.assignment.request_key, log[0]["request_key"])

    def test_risk_stop_scope_only_blocks_that_segment(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        self.register_household("hh2", devices=("dev2-a",))
        self.tick("2026-03-03T01:00:00+08:00")
        self.engine.risk_stop(
            "trial",
            {
                "scope": "trial:t1",
                "at": "2026-03-03T00:00:00+08:00",
                "actor": "safety",
                "reason": "演练：该范围暂停新增",
            },
        )
        blocked = self.access("hh1", "visit-1", when="2026-03-09T09:00:00+08:00")
        self.assertEqual(blocked.status, "refused")
        self.assertIn("风险停用", blocked.reason)
        other_channel = self.access("hh2", "visit-2", when="2026-03-09T09:00:00+08:00", channel="t9")
        self.assertEqual(other_channel.status, "assigned")

    # -- 回执与答卷 -----------------------------------------------------
    def test_receipt_duplicates_and_unknown_receipts(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        outcome = self.access("hh1", "visit-1")
        aid = outcome.assignment.id
        body = {
            "occurred_at": "2026-03-02T09:10:00+08:00",
            "known_at": "2026-03-02T09:20:00+08:00",
            "channel": "t1",
            "status": "unknown",
        }
        first = self.engine.receipt(aid, body)
        second = self.engine.receipt(aid, body)
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "duplicate_receipt")
        analysis = self.engine.analyse("trial", as_of=at("2026-03-06T00:00:00+08:00"))
        view = next(v for v in analysis.assignments if v.assignment.id == aid)
        self.assertEqual(view.display_state, "unknown")

    def test_answer_window_states(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        outcome = self.access("hh1", "visit-1", when="2026-03-02T09:00:00+08:00")
        aid = outcome.assignment.id
        self.engine.response(
            aid,
            {
                "answered_at": "2026-03-02T12:00:00+08:00",
                "known_at": "2026-03-02T13:00:00+08:00",
                "correct": True,
                "false_certainty": False,
                "revision": 1,
            },
        )
        # 同一分配再报一次晚到答卷：窗口内已有答卷，主分析仍取窗口内。
        self.engine.response(
            aid,
            {
                "answered_at": "2026-03-06T12:00:00+08:00",
                "known_at": "2026-03-06T13:00:00+08:00",
                "correct": False,
                "false_certainty": True,
                "revision": 2,
            },
        )
        analysis = self.engine.analyse("trial", as_of=at("2026-03-10T00:00:00+08:00"))
        view = next(v for v in analysis.assignments if v.assignment.id == aid)
        self.assertEqual(view.answer_state, "answered")
        self.assertTrue(view.correct)
        self.assertIn("late_answer", view.answer_flags)
        self.assertEqual(analysis.arms[view.assignment.arm].missing, 0)

    def test_missing_answers_stay_in_primary_denominator(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        self.register_household("hh2", devices=("dev2-a",))
        self.access("hh1", "visit-1")
        analysis = self.engine.analyse("trial", as_of=at("2026-03-15T00:00:00+08:00"))
        total_matured = sum(outcome.matured for outcome in analysis.arms.values())
        self.assertEqual(total_matured, 1)
        self.assertEqual(sum(outcome.answered for outcome in analysis.arms.values()), 0)
        self.assertEqual(analysis.as_dict()["sensitivity"]["answer_missing"]["missing"], 1)

    # -- 决定 -----------------------------------------------------------
    def test_promotion_requires_both_conditions_and_approved_lead(self):
        self.register(minimum_gain=0.2, maximum_false_certainty=0.0)
        self.register_policy()
        self.register_household("hh1")
        self.access("hh1", "visit-1")
        with self.assertRaises(EngineError) as ctx:
            self.engine.promote_confirm(
                "trial",
                {"scope": "trial:t1", "at": "2026-03-10T00:00:00+08:00", "actor": "not-approved"},
            )
        self.assertEqual(ctx.exception.code, "not_approved_lead")
        with self.assertRaises(EngineError) as ctx:
            self.engine.promote_confirm(
                "trial",
                {"scope": "trial:t1", "at": "2026-03-10T00:00:00+08:00", "actor": "lead-a"},
            )
        self.assertIn(ctx.exception.code, ("undecidable", "not_promotable"))

    def test_takeover_requires_confirmed_promotion_and_only_covers_acked_scope(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        request = self.engine.promote_request(
            "trial", {"scope": "trial:all", "at": "2026-03-10T00:00:00+08:00"}
        )
        with self.assertRaises(EngineError) as ctx:
            self.engine.takeover_ack(
                "trial",
                {"scope": "trial:t1", "at": "2026-03-11T00:00:00+08:00", "reference": request["decision_id"]},
            )
        self.assertEqual(ctx.exception.code, "invalid_reference")

    def test_snapshot_uses_only_information_known_then(self):
        snapshot = self.engine.snapshot("ex-a", at("2026-02-01T12:00:00+08:00"))
        self.assertEqual(snapshot["conclusion"], "undecidable")
        # 2 月 1 日只有当天生效的风险停用；2 月 4 日的推广与 2 月 5 日的接管还不存在。
        self.assertEqual([d["kind"] for d in snapshot["decisions"]], ["risk_stop"])
        self.assertEqual(snapshot["invalid_takeovers"], [])
        later = self.engine.snapshot("ex-a", at("2026-02-06T00:00:00+08:00"))
        self.assertEqual(len(later["invalid_takeovers"]), 1)

    def test_stop_keeps_prior_valid_displays_redeemed_and_marks_revoked(self):
        self.register()
        self.register_policy()
        self.register_household("hh1")
        self.register_household("hh2", devices=("dev2-a",))
        shown = self.access("hh1", "visit-1", when="2026-03-02T09:00:00+08:00")
        self.engine.receipt(
            shown.assignment.id,
            {
                "occurred_at": "2026-03-02T09:10:00+08:00",
                "known_at": "2026-03-02T09:20:00+08:00",
                "channel": "t1",
                "status": "displayed",
            },
        )
        pending = self.access("hh2", "visit-2", when="2026-03-02T10:00:00+08:00")
        self.tick("2026-03-03T01:00:00+08:00")
        self.engine.risk_stop(
            "trial",
            {
                "scope": "trial:t1",
                "at": "2026-03-03T00:00:00+08:00",
                "actor": "safety",
                "reason": "演练：暂停新增展示",
            },
        )
        permits = {permit.assignment_id: permit for permit in self.engine.permits("trial", at("2026-03-10T00:00:00+08:00"))}
        self.assertEqual(permits[shown.assignment.id].state, "redeemed")
        self.assertEqual(permits[pending.assignment.id].state, "revoked")
        self.assertIsNotNone(permits[pending.assignment.id].revoke_decision)

    def test_probability_missing_downgrades_only_affected_scope(self):
        analysis = self.engine.analyse("ex-a")
        self.assertTrue(analysis.undecidable)
        self.assertEqual(analysis.undecidable_reasons[0]["code"], "missing_probability")
        self.assertIn("a71", analysis.undecidable_reasons[0]["detail"])
        # 同一互斥范围内的另一个试验不受影响。
        self.assertFalse(self.engine.analyse("ex-b").undecidable)


class InferenceCase(unittest.TestCase):
    def test_confidence_sequence_widens_with_less_data(self):
        small = inference.binomial_cs(10, 20, 0.05)
        large = inference.binomial_cs(500, 1000, 0.05)
        self.assertGreater(small[1] - small[0], large[1] - large[0])

    def test_missing_probability_does_not_shrink_denominator(self):
        arm = inference.ArmOutcome("x", matured=100, answered=50, correct=30, false_certainty=5, missing=50)
        self.assertAlmostEqual(arm.correct_itt, 0.3)
        self.assertAlmostEqual(arm.correct_answered, 0.6)
        self.assertAlmostEqual(arm.false_certainty_worst, 0.55)

    def test_risk_alert_uses_primary_itt_metric_not_complete_case(self):
        # 完整答卷者中错误确信很高，但主要指标是按原分配、且多数家庭有答卷，
        # 因此不能仅凭 complete-case 就宣布越界。
        arm = inference.ArmOutcome(
            "c", matured=400, answered=200, correct=100, false_certainty=40, missing=200
        )
        reference = inference.ArmOutcome("r", matured=400, answered=380, correct=100, false_certainty=4)
        pair = inference.evaluate_pair(
            arm,
            reference,
            minimum_gain=0.04,
            maximum_false_certainty=0.12,
            gate_alpha=0.0125,
        )
        # 主指标 40/400=0.1，未越过 0.12；complete-case 80/200=0.4 只作敏感性。
        self.assertFalse(pair.risk_alert)
        self.assertLessEqual(pair.risk_itt_cs_gate[1], 1.0)
        self.assertAlmostEqual(arm.false_certainty_worst, 0.6)

    def test_evaluation_requires_effect_and_risk_together(self):
        good = inference.ArmOutcome("c", matured=800, answered=800, correct=520, false_certainty=10)
        reference = inference.ArmOutcome("r", matured=800, answered=800, correct=360, false_certainty=10)
        pair = inference.evaluate_pair(
            good,
            reference,
            minimum_gain=0.04,
            maximum_false_certainty=0.12,
            gate_alpha=0.0125,
        )
        self.assertEqual(pair.status, "promotable")
        risky = inference.ArmOutcome("c", matured=800, answered=800, correct=520, false_certainty=160)
        pair = inference.evaluate_pair(
            risky,
            reference,
            minimum_gain=0.04,
            maximum_false_certainty=0.12,
            gate_alpha=0.0125,
        )
        self.assertEqual(pair.status, "risk_alert")
        self.assertFalse(pair.risk_condition)

    def test_scope_parsing(self):
        self.assertEqual(parse_scope("taste:c1"), ("taste", "c1"))
        self.assertEqual(parse_scope("taste"), ("taste", None))
        self.assertEqual(parse_scope("all:all"), (None, None))
        self.assertEqual(parse_scope(":c1"), (None, "c1"))


class SimulationCase(unittest.TestCase):
    def test_null_trial_does_not_promote_small_sample(self):
        results = [
            run_trial(null_scenario(4242 + i, daily_per_arm=2, days=10, followup_days=5))
            for i in range(8)
        ]
        self.assertFalse(any(result.false_promotion for result in results))

    def test_reproducible(self):
        first = run_trial(alternative_scenario(777, daily_per_arm=40, days=20))
        second = run_trial(alternative_scenario(777, daily_per_arm=40, days=20))
        self.assertEqual(first.promoted_at_day, second.promoted_at_day)
        self.assertEqual(first.final_status, second.final_status)

    def test_alternative_detected(self):
        results = [run_trial(alternative_scenario(9000 + i, lift=0.15, daily_per_arm=120, days=30)) for i in range(6)]
        detected = sum(1 for result in results if result.false_promotion)
        self.assertGreaterEqual(detected, 4)

    def test_suite_controls_false_promotion(self):
        report = run_suite(trials=12)
        self.assertLessEqual(report["null"]["false_promotion_rate"], 0.05)
        self.assertLessEqual(report["short_exposure_null"]["false_promotion_rate"], 0.05)
        self.assertGreater(report["alternative"]["false_promotion_rate"], 0.5)
        self.assertGreater(report["risk_increase"]["risk_alert_rate"], 0.5)


class ServiceCase(unittest.TestCase):
    def test_http_flow(self):
        try:
            from fastapi.testclient import TestClient
        except RuntimeError:  # httpx 未安装时跳过 HTTP 层
            self.skipTest("httpx 不可用")
        from trial_platform import service

        with tempfile.TemporaryDirectory() as tmp:
            import os

            old = os.environ.get("TRIAL_STATE_DIR")
            os.environ["TRIAL_STATE_DIR"] = str(Path(tmp) / "state")
            service._CACHE.clear()
            self.addCleanup(self._restore_env, old)
            self.addCleanup(service._CACHE.clear)
            client = TestClient(service.create_app())
            response = client.get("/experiments")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["experiments"]), 2)
            snapshot = client.get(
                "/experiments/ex-a/snapshot", params={"at": "2026-02-06T00:00:00+08:00"}
            )
            self.assertEqual(snapshot.status_code, 200)
            self.assertEqual(len(snapshot.json()["invalid_takeovers"]), 1)

    @staticmethod
    def _restore_env(old):
        import os

        if old is None:
            os.environ.pop("TRIAL_STATE_DIR", None)
        else:
            os.environ["TRIAL_STATE_DIR"] = old


if __name__ == "__main__":
    unittest.main()
