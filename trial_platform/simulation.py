"""可重复的随机合成校验。

两类场景：

1. 无改善（null）：所有说明的真实正确理解比例相同，错误确信比例相同，缺失机制
   相同。按“每天查看一次”的持续查看调度反复判定，统计任何一次错误推广的比例。
   由于推广门槛使用持续查看下的置信序列，这个比例应不超过 ``alpha``（含登记范围
   与候选数拆分），少量样本碰巧领先不会自动过关。
2. 有改善（alternative）：一个说明把正确理解比例提高 ``lift``。统计在最长观察期
   内能识别出改善的比例（识别能力），以及误停风险。

另外用同一套引擎做一次端到端演练：登记 → 政策 → 访问 → 回执 → 答卷 → 分析，
核对重复请求、设备切换、互斥、接管与迟到答卷的归属。

所有随机数由显式种子驱动；同一组参数重复运行结果一致。
"""
from __future__ import annotations

import json
import math
import random
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import inference

UTC8 = timezone(timedelta(hours=8))


def at_time(text: str) -> datetime:
    return datetime.fromisoformat(text)


@dataclass
class Scenario:
    name: str
    seed: int
    arms: list[str]
    reference: str
    daily_per_arm: int
    days: int
    followup_days: int
    minimum_gain: float
    maximum_false_certainty: float
    base_correct: float
    base_false_certainty: float
    lift: float = 0.0
    lifted_arm: str | None = None
    lift_false_certainty: float = 0.0
    answer_probability: float = 0.75
    planned_scopes: int = 1
    alpha: float = 0.05
    minimum_matured_per_arm: int = 20


@dataclass
class TrialResult:
    promoted_at_day: int | None = None
    risk_alert_at_day: int | None = None
    promoted_arm: str | None = None
    looks: int = 0
    final_status: dict[str, str] = field(default_factory=dict)

    @property
    def false_promotion(self) -> bool:
        return self.promoted_at_day is not None


def _draw_arm_outcomes(
    rng: np.random.Generator,
    *,
    arm: str,
    count: int,
    correct_rate: float,
    false_certainty_rate: float,
    missing_rate: float,
) -> inference.ArmOutcome:
    """一次成熟家庭的二项抽样；缺失答卷不计入 answered。"""
    correct = int(rng.binomial(count, correct_rate))
    answered = int(rng.binomial(count, 1.0 - missing_rate))
    # 错误确信在已答家庭中按条件概率抽样，且与正确理解按原始编码允许并存。
    denominator = max(answered, 1)
    conditional = min(max(false_certainty_rate / max(1e-9, 1.0 - missing_rate), 0.0), 1.0)
    false_certainty = int(rng.binomial(answered, conditional)) if answered else 0
    # 正确理解只可能在已答家庭出现；主指标仍以全部成熟家庭为分母。
    correct = min(correct, answered)
    return inference.ArmOutcome(
        arm=arm,
        matured=count,
        answered=answered,
        missing=count - answered,
        correct=correct,
        false_certainty=false_certainty,
    )


def run_trial(scenario: Scenario) -> TrialResult:
    """按日加入新家庭，每天查看一次，模拟一次完整试验。"""
    rng = np.random.default_rng(scenario.seed)
    candidate_arms = [arm for arm in scenario.arms if arm != scenario.reference]
    gate_alpha = inference.multiple_comparison_alpha(
        scenario.alpha, len(candidate_arms), scenario.planned_scopes
    )
    totals: dict[str, inference.ArmOutcome] = {
        arm: inference.ArmOutcome(arm=arm) for arm in scenario.arms
    }
    result = TrialResult()
    for day in range(scenario.days):
        for arm in scenario.arms:
            correct_rate = scenario.base_correct
            fc_rate = scenario.base_false_certainty
            if arm == scenario.lifted_arm:
                correct_rate = min(0.99, correct_rate + scenario.lift)
                fc_rate = min(0.99, fc_rate + scenario.lift_false_certainty)
            draw = _draw_arm_outcomes(
                rng,
                arm=arm,
                count=scenario.daily_per_arm,
                correct_rate=correct_rate,
                false_certainty_rate=fc_rate,
                missing_rate=1.0 - scenario.answer_probability,
            )
            total = totals[arm]
            total.matured += draw.matured
            total.answered += draw.answered
            total.missing += draw.missing
            total.correct += draw.correct
            total.false_certainty += draw.false_certainty
        if day + 1 < scenario.followup_days:
            continue  # 还没有家庭成熟，不能查看结论。
        result.looks += 1
        pairs = [
            inference.evaluate_pair(
                totals[arm],
                totals[scenario.reference],
                minimum_gain=scenario.minimum_gain,
                maximum_false_certainty=scenario.maximum_false_certainty,
                gate_alpha=gate_alpha,
                minimum_matured_per_arm=scenario.minimum_matured_per_arm,
            )
            for arm in candidate_arms
        ]
        result.final_status = {pair.arm: pair.status for pair in pairs}
        if result.risk_alert_at_day is None and any(pair.risk_alert for pair in pairs):
            result.risk_alert_at_day = day + 1
        if result.promoted_at_day is None:
            promotable = [pair for pair in pairs if pair.status == "promotable"]
            if promotable:
                result.promoted_at_day = day + 1
                result.promoted_arm = promotable[0].arm
    return result


@dataclass
class SimulationSummary:
    scenario: str
    trials: int
    alpha: float
    false_promotion_rate: float
    false_promotion_upper: float
    median_days_to_promotion: float | None
    mean_looks: float
    risk_alert_rate: float
    empirical_promotion_rate: float

    def as_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "trials": self.trials,
            "alpha": self.alpha,
            "false_promotion_rate": self.false_promotion_rate,
            "false_promotion_upper": self.false_promotion_upper,
            "median_days_to_promotion": self.median_days_to_promotion,
            "mean_looks": self.mean_looks,
            "risk_alert_rate": self.risk_alert_rate,
            "empirical_promotion_rate": self.empirical_promotion_rate,
        }


def summarize(name: str, results: list[TrialResult], alpha: float) -> SimulationSummary:
    trials = len(results)
    false_promotions = sum(1 for r in results if r.false_promotion)
    rate = false_promotions / trials if trials else 0.0
    # 二项比例的 95% 上界（Wilson），用于确认错误推广没有明显超出名义水平。
    z = 1.959963984540054
    denominator = 1 + z * z / trials if trials else 1
    centre = (rate + z * z / (2 * trials)) / denominator if trials else 0.0
    margin = (
        z * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials)) / denominator
        if trials
        else 0.0
    )
    days = sorted(r.promoted_at_day for r in results if r.promoted_at_day is not None)
    median_days = days[len(days) // 2] if days else None
    return SimulationSummary(
        scenario=name,
        trials=trials,
        alpha=alpha,
        false_promotion_rate=rate,
        false_promotion_upper=min(1.0, centre + margin),
        median_days_to_promotion=median_days,
        mean_looks=float(np.mean([r.looks for r in results])) if results else 0.0,
        risk_alert_rate=sum(1 for r in results if r.risk_alert_at_day is not None) / trials if trials else 0.0,
        empirical_promotion_rate=rate,
    )


def null_scenario(seed: int, **overrides: Any) -> Scenario:
    base = dict(
        name="null",
        seed=seed,
        arms=["reference", "candidate"],
        reference="reference",
        daily_per_arm=10,
        days=40,
        followup_days=5,
        minimum_gain=0.04,
        maximum_false_certainty=0.12,
        base_correct=0.45,
        base_false_certainty=0.06,
        answer_probability=0.8,
    )
    base.update(overrides)
    return Scenario(**base)


def alternative_scenario(seed: int, lift: float = 0.12, **overrides: Any) -> Scenario:
    base = dict(
        name=f"alternative_lift_{lift:.2f}",
        seed=seed,
        arms=["reference", "candidate"],
        reference="reference",
        daily_per_arm=80,
        days=40,
        followup_days=5,
        minimum_gain=0.04,
        maximum_false_certainty=0.12,
        base_correct=0.45,
        base_false_certainty=0.06,
        lift=lift,
        lifted_arm="candidate",
        answer_probability=0.8,
    )
    base.update(overrides)
    return Scenario(**base)


def run_suite(*, trials: int = 60, base_seed: int = 20260101) -> dict:
    """默认校验套件：无改善控制 + 有改善识别 + 风险停用触发。"""
    null_results = [run_trial(null_scenario(base_seed + i)) for i in range(trials)]
    alt_results = [
        run_trial(alternative_scenario(base_seed + 10000 + i, lift=0.12)) for i in range(trials)
    ]
    small_results = [
        run_trial(null_scenario(base_seed + 20000 + i, daily_per_arm=2, days=12, followup_days=5))
        for i in range(trials)
    ]
    # 少量样本“碰巧好看”：候选在早期随机领先，但没有真实改善。
    lucky_results = []
    for i in range(trials):
        scenario = null_scenario(
            base_seed + 40000 + i,
            daily_per_arm=25,
            days=10,
            followup_days=5,
            base_correct=0.45,
        )
        lucky_results.append(run_trial(scenario))
    risk_results = [
        run_trial(
            null_scenario(
                base_seed + 30000 + i,
                lift=0.0,
                lifted_arm="candidate",
                lift_false_certainty=0.14,
                base_correct=0.45,
            )
        )
        for i in range(trials)
    ]
    return {
        "schedule": "每天一次持续查看；无改善场景的错误推广率应不超过 alpha",
        "null": summarize("null", null_results, 0.05).as_dict(),
        "small_sample_null": summarize("small_sample_null", small_results, 0.05).as_dict(),
        "short_exposure_null": summarize("short_exposure_null", lucky_results, 0.05).as_dict(),
        "alternative": summarize("alternative", alt_results, 0.05).as_dict(),
        "risk_increase": summarize("risk_increase", risk_results, 0.05).as_dict(),
        "interpretation": {
            "null": "无真实改善时被推广的比例；应不超过 alpha",
            "small_sample_null": "每天仅少量样本、观察期短：不应因碰巧领先而推广",
            "short_exposure_null": "较早结束的试验也没有更多推广机会",
            "alternative": "存在真实改善时在观察期内被识别的比例",
            "risk_increase": "错误确信超限时触发风险警示的比例",
        },
    }


# ----------------------------------------------------------------------
# 端到端演练：同一套引擎跑通登记、分配、回执、答卷与接管
# ----------------------------------------------------------------------
def end_to_end_demo(path: str | Path | None = None) -> dict:
    """确定性演练：同一套引擎跑通从登记到部分接管与风险停用。

    不依赖机器当前时间：引擎注入固定时钟，所有“当时可知”与演练时点一致。
    合成家庭直接写入结果，用于核对治理流程，不作为业务效果证据。
    """
    from .engine import Engine, EngineError

    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(path) if path else Path(tmp) / "state"
        clock_now = at_time("2026-03-01T00:00:00+08:00")
        # 演练会写入约一万条事件；关闭逐条 fsync 只为速度，真实服务保持默认开启。
        engine = Engine(state_dir=state_dir, clock=lambda: clock_now, fsync=False)
        experiment_id = "sim-trial"
        engine.register_experiment(
            {
                "experiment_id": experiment_id,
                "exclusive_scope": "sim-label",
                "start_at": "2026-03-01T00:00:00+08:00",
                "end_at": "2026-04-30T00:00:00+08:00",
                "followup_hours": 48,
                "minimum_gain": 0.04,
                "maximum_false_certainty": 0.12,
                "look_policy": "持续查看",
                "arms": ["base", "candidate"],
                "reference_arm": "base",
                "approved_leads": ["lead-a"],
                "planned_scopes": ["trial:t1", "trial:t2"],
            }
        )
        engine.register_policy(
            experiment_id,
            {
                "policy_id": "sim-policy-1",
                "weights": {"base": 0.5, "candidate": 0.5},
                "effective_at": "2026-03-01T00:00:00+08:00",
            },
        )
        # 两个渠道各 200 个家庭；真实改善 0.12，错误确信保持在阈值内。
        rng = np.random.default_rng(20260301)
        households = []
        # 两个渠道各 1500 个家庭：足以在 5% 总错误水平下识别 15 个百分点的改善。
        for channel, count in (("t1", 1500), ("t2", 1500)):
            for index in range(count):
                household_id = f"sim-{channel}-{index:03d}"
                households.append((household_id, channel))
                engine.register_household(
                    {
                        "household_id": household_id,
                        "segment": "trial",
                        "region": channel,
                        "devices": [f"{household_id}-dev-a", f"{household_id}-dev-b"],
                        "eligible_from": "2026-03-01T00:00:00+08:00",
                    }
                )
        assignment_ids = []
        for index, (household_id, channel) in enumerate(households):
            opportunity = at_time("2026-03-02T09:00:00+08:00") + timedelta(minutes=index % 1440)
            outcome = engine.access(
                {
                    "household_id": household_id,
                    "experiment_id": experiment_id,
                    "request_key": f"visit-{household_id}",
                    "at": opportunity.isoformat(),
                    "channel": channel,
                    "device": f"{household_id}-dev-a",
                }
            )
            if outcome.status != "assigned":
                continue
            assignment_ids.append(outcome.assignment.id)
            clock_now = opportunity + timedelta(minutes=10)
            engine.receipt(
                outcome.assignment.id,
                {
                    "occurred_at": (opportunity + timedelta(minutes=5)).isoformat(),
                    "known_at": (opportunity + timedelta(minutes=20)).isoformat(),
                    "channel": channel,
                    "status": "displayed",
                },
            )
            clock_now = opportunity + timedelta(hours=30)
            true_rate = 0.60 + (0.20 if outcome.assignment.arm == "candidate" else 0.0)
            engine.response(
                outcome.assignment.id,
                {
                    "answered_at": (opportunity + timedelta(hours=30)).isoformat(),
                    "known_at": (opportunity + timedelta(hours=31)).isoformat(),
                    "correct": bool(rng.random() < true_rate),
                    "false_certainty": bool(rng.random() < 0.02),
                    "revision": 1,
                },
            )
        clock_now = at_time("2026-03-20T00:00:00+08:00")
        analysis = engine.analyse(experiment_id, as_of=clock_now)
        promotion = engine.promote_request(
            experiment_id,
            {"scope": "trial:all", "at": clock_now.isoformat(), "actor": "ops"},
        )
        scopes_at_decision = {
            channel: engine.analyse(experiment_id, as_of=clock_now, segment="trial", channel=channel)
            for channel in ("t1", "t2")
        }
        confirmation = None
        confirmation_error = None
        try:
            confirmation = engine.promote_confirm(
                experiment_id,
                {
                    "scope": "trial:all",
                    "at": clock_now.isoformat(),
                    "actor": "lead-a",
                    "reference": promotion["decision_id"],
                },
            )
        except EngineError as exc:
            confirmation_error = {"code": exc.code, "detail": exc.detail}
        takeover = None
        if confirmation is not None:
            takeover = engine.takeover_ack(
                experiment_id,
                {
                    "scope": "trial:t1",
                    "at": clock_now.isoformat(),
                    "reference": confirmation["decision_id"],
                },
            )
        # 接管之后到达的新访问：已接管渠道由新政策服务，未接管渠道继续进入试验。
        for household_id, channel in (("sim-late-t1", "t1"), ("sim-late-t2", "t2")):
            engine.register_household(
                {
                    "household_id": household_id,
                    "segment": "trial",
                    "region": channel,
                    "devices": [f"{household_id}-dev-a"],
                    "eligible_from": "2026-03-01T00:00:00+08:00",
                }
            )
        new_access = engine.access(
            {
                "household_id": "sim-late-t1",
                "experiment_id": experiment_id,
                "request_key": "post-takeover-visit",
                "at": at_time("2026-03-21T09:00:00+08:00").isoformat(),
                "channel": "t1",
                "device": "sim-late-t1-dev-a",
            }
        )
        still_trial = engine.access(
            {
                "household_id": "sim-late-t2",
                "experiment_id": experiment_id,
                "request_key": "post-takeover-visit-t2",
                "at": at_time("2026-03-21T09:00:00+08:00").isoformat(),
                "channel": "t2",
                "device": "sim-late-t2-dev-a",
            }
        )
        return {
            "assignments": len(assignment_ids),
            "analysis_at_decision": {
                "conclusion": analysis.conclusion,
                "by_channel": {channel: scope.conclusion for channel, scope in scopes_at_decision.items()},
                "comparisons": [pair.as_dict() for pair in analysis.pairs],
            },
            "promotion_request": promotion["conclusion"],
            "promotion_confirmation": confirmation_error or confirmation["promoted_arm"],
            "takeover_scope": takeover["scope"] if takeover else None,
            "takeover_partial": takeover["partial"] if takeover else None,
            "access_after_partial_takeover": {
                "taken_over_channel": new_access.status,
                "not_taken_over_channel": still_trial.status,
            },
            "handover_count": len(engine.handovers()),
            "trial_answers_after_handover": engine.analyse(experiment_id, as_of=clock_now).arms[
                "candidate"
            ].answered,
            "handover_assignments_are_separate": all(
                not any(
                    trial.assignment.request_key == handover.request_key
                    for trial in engine.assignments().values()
                )
                for handover in engine.handovers()
            ),
        }


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    trials = 60
    if "--trials" in argv:
        trials = int(argv[argv.index("--trials") + 1])
    payload = run_suite(trials=trials)
    payload["end_to_end"] = end_to_end_demo()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
