"""按原分配的持续推断。

主要估计量（事前登记）：

- 正确理解：分母是全部已成熟的原分配家庭，窗口内没有答卷按“未正确理解”计入；
- 错误确信：分母同样是全部已成熟家庭，窗口内没有答卷按“未错误确信”计入；
- 效果 = 候选说明的正确理解比例 − 参照说明的正确理解比例。

两条敏感性单独报告：只算已答家庭（complete case）和把缺失答卷都当作错误确信
（worst case）。推广门槛只使用主要估计量，不用敏感性结果挑选有利算法。

区间使用二项比例的混合鞅置信序列（confidence sequence）。它允许在任意时点查看
累积数据而不需要事先固定样本量，因此“每天看一次”不会按查看次数放大错误推广
概率。差值区间由两个比例的区间取并集界。推广判定的错误水平再按候选说明数与
事前登记范围数拆分，控制整个登记范围内的错误推广机会。

这里只做统计，不读资料表。引擎负责范围、成熟度、支持性和资料完整性的判定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import log, log1p
from typing import Iterable

import numpy as np
from scipy.optimize import brentq
from scipy.special import betaln

DEFAULT_PRIOR = (0.5, 0.5)  # Jeffreys 先验，小样本下比均匀先验更窄。
GRID_POINTS = 1001


def binomial_cs(
    successes: int,
    trials: int,
    alpha: float = 0.05,
    prior: tuple[float, float] = DEFAULT_PRIOR,
) -> tuple[float, float]:
    """二项比例在持续查看下的 ``1-alpha`` 置信序列。

    返回当前数据下不被似然比证据排除的比例区间。``trials == 0`` 时返回 ``(0, 1)``，
    表示还没有可作结论的信息。``alpha`` 是单条序列在整个查看过程中的错误水平。
    """
    if trials < 0 or successes < 0 or successes > trials:
        raise ValueError("successes 必须在 0 与 trials 之间")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha 必须在 0 与 1 之间")
    if trials == 0:
        return (0.0, 1.0)

    threshold = -log(alpha)
    a, b = prior
    constant = betaln(a, b) - betaln(successes + a, trials - successes + b)

    def score(mu: float) -> float:
        # 大于 0 表示 mu 仍未被排除：log M_n(mu) < log(1/alpha)。
        return constant + successes * log(mu) + (trials - successes) * log1p(-mu) + threshold

    grid = np.linspace(1e-9, 1.0 - 1e-9, GRID_POINTS)
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = (
            constant
            + successes * np.log(grid)
            + (trials - successes) * np.log1p(-grid)
            + threshold
        )
    inside = scores >= 0.0
    if not inside.any():
        return (0.0, 1.0)
    first = int(np.argmax(inside))
    last = int(len(inside) - 1 - np.argmax(inside[::-1]))

    if first == 0:
        low = 0.0
    else:
        low = brentq(score, float(grid[first - 1]), float(grid[first]), xtol=1e-12)
    if last == len(grid) - 1:
        high = 1.0
    else:
        high = brentq(score, float(grid[last]), float(grid[last + 1]), xtol=1e-12)
    return (max(0.0, low), min(1.0, high))


def difference_cs(first: tuple[float, float], second: tuple[float, float]) -> tuple[float, float]:
    """两个独立比例之差 ``first - second`` 的区间（并集界）。"""
    return (first[0] - second[1], first[1] - second[0])


@dataclass
class ArmOutcome:
    """一个原分配说明在分析时点的已成熟结果。

    ``answered`` 是到期窗内回报了答卷的家庭；``late_only`` 是只有到期后答卷的
    已成熟家庭（主分析按缺失处理，另报敏感性）；``missing`` 是已成熟但没有任何
    窗内答卷的家庭，包含 ``late_only``。
    """

    arm: str
    matured: int = 0
    correct: int = 0
    answered: int = 0
    false_certainty: int = 0
    missing: int = 0
    late_only: int = 0
    early_answer: int = 0
    late_answer: int = 0
    waiting: int = 0
    displayed: int = 0
    displayed_answered: int = 0
    displayed_correct: int = 0
    display_unknown: int = 0
    no_receipt: int = 0
    display_deviation: int = 0

    @property
    def correct_itt(self) -> float:
        return self.correct / self.matured if self.matured else 0.0

    @property
    def correct_answered(self) -> float:
        return self.correct / self.answered if self.answered else 0.0

    @property
    def false_certainty_itt(self) -> float:
        return self.false_certainty / self.matured if self.matured else 0.0

    @property
    def false_certainty_complete_case(self) -> float:
        return self.false_certainty / self.answered if self.answered else 0.0

    @property
    def false_certainty_worst(self) -> float:
        if not self.matured:
            return 0.0
        return (self.false_certainty + self.missing) / self.matured


def _cs_dict(bounds: tuple[float, float], alpha: float) -> dict:
    return {"alpha": alpha, "lower": bounds[0], "upper": bounds[1]}


@dataclass
class PairEvaluation:
    arm: str
    reference: str
    effect: float
    effect_cs_marginal: tuple[float, float]
    effect_cs_gate: tuple[float, float]
    risk_itt_cs_marginal: tuple[float, float]
    risk_itt_cs_gate: tuple[float, float]
    risk_complete_cs_gate: tuple[float, float]
    risk_worst_cs_gate: tuple[float, float]
    gate_alpha: float
    marginal_alpha: float
    minimum_gain: float
    maximum_false_certainty: float
    effect_condition: bool
    risk_condition: bool
    risk_alert: bool
    risk_watch: bool
    status: str
    reasons: list[str] = field(default_factory=list)
    arm_summary: dict = field(default_factory=dict)
    reference_summary: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "arm": self.arm,
            "reference": self.reference,
            "effect": self.effect,
            "effect_cs_marginal": {"level": 1 - self.marginal_alpha, **_cs_dict(self.effect_cs_marginal, self.marginal_alpha / 4)},
            "effect_cs_gate": {**_cs_dict(self.effect_cs_gate, self.gate_alpha / 4), "role": "promotion_gate"},
            "risk_cs_marginal": {
                "level": 1 - self.marginal_alpha,
                **_cs_dict(self.risk_itt_cs_marginal, self.marginal_alpha),
                "basis": "itt_missing_as_not_false_certainty",
            },
            "risk_cs_gate": {
                **_cs_dict(self.risk_itt_cs_gate, self.gate_alpha / 2),
                "basis": "itt_missing_as_not_false_certainty",
                "role": "promotion_gate",
            },
            "risk_sensitivity": {
                "complete_case": _cs_dict(self.risk_complete_cs_gate, self.gate_alpha / 2),
                "worst_case_missing_as_false_certainty": _cs_dict(self.risk_worst_cs_gate, self.gate_alpha / 2),
            },
            "minimum_gain": self.minimum_gain,
            "maximum_false_certainty": self.maximum_false_certainty,
            "effect_condition": self.effect_condition,
            "risk_condition": self.risk_condition,
            "risk_alert": self.risk_alert,
            "risk_watch": self.risk_watch,
            "status": self.status,
            "reasons": self.reasons,
            "arm_summary": self.arm_summary,
            "reference_summary": self.reference_summary,
        }


def _summary(outcome: ArmOutcome) -> dict:
    return {
        "arm": outcome.arm,
        "matured": outcome.matured,
        "answered": outcome.answered,
        "missing": outcome.missing,
        "late_only": outcome.late_only,
        "early_answer": outcome.early_answer,
        "late_answer": outcome.late_answer,
        "waiting": outcome.waiting,
        "correct": outcome.correct,
        "correct_itt": outcome.correct_itt,
        "correct_answered": outcome.correct_answered,
        "false_certainty": outcome.false_certainty,
        "false_certainty_itt": outcome.false_certainty_itt,
        "false_certainty_complete_case": outcome.false_certainty_complete_case,
        "false_certainty_worst": outcome.false_certainty_worst,
        "displayed": outcome.displayed,
        "displayed_answered_correct_rate": (
            outcome.displayed_correct / outcome.displayed_answered if outcome.displayed_answered else None
        ),
        "display_unknown": outcome.display_unknown,
        "no_receipt": outcome.no_receipt,
        "display_deviation": outcome.display_deviation,
    }


def evaluate_pair(
    arm: ArmOutcome,
    reference: ArmOutcome,
    *,
    minimum_gain: float,
    maximum_false_certainty: float,
    gate_alpha: float,
    minimum_matured_per_arm: int = 20,
    marginal_alpha: float = 0.05,
) -> PairEvaluation:
    """比较一个说明与原分配参照，给出推广所需的两项条件。

    推广判定把 ``gate_alpha`` 再对半分给“效果”和“风险”两项条件；效果差值用并集
    界，故每个说明的推广错误率不超过 ``gate_alpha``。
    """
    effect = arm.correct_itt - reference.correct_itt

    arm_cs_gate = binomial_cs(arm.correct, arm.matured, gate_alpha / 4.0)
    ref_cs_gate = binomial_cs(reference.correct, reference.matured, gate_alpha / 4.0)
    effect_gate = difference_cs(arm_cs_gate, ref_cs_gate)

    arm_cs_marginal = binomial_cs(arm.correct, arm.matured, marginal_alpha / 4.0)
    ref_cs_marginal = binomial_cs(reference.correct, reference.matured, marginal_alpha / 4.0)
    effect_marginal = difference_cs(arm_cs_marginal, ref_cs_marginal)

    risk_itt_gate = binomial_cs(arm.false_certainty, arm.matured, gate_alpha / 2.0)
    risk_itt_marginal = binomial_cs(arm.false_certainty, arm.matured, marginal_alpha)
    risk_complete_gate = binomial_cs(arm.false_certainty, arm.answered, gate_alpha / 2.0)
    risk_worst_gate = binomial_cs(arm.false_certainty + arm.missing, arm.matured, gate_alpha / 2.0)

    reasons: list[str] = []
    matured_ok = arm.matured >= minimum_matured_per_arm and reference.matured >= minimum_matured_per_arm
    if not matured_ok:
        reasons.append(
            f"成熟家庭不足：{arm.arm}={arm.matured}，{reference.arm}={reference.matured}，"
            f"至少需要 {minimum_matured_per_arm}"
        )

    effect_condition = matured_ok and effect_gate[0] >= minimum_gain
    if matured_ok and not effect_condition:
        reasons.append(f"效果下界 {effect_gate[0]:.3f} 未达到最低有意义增益 {minimum_gain:.3f}")

    if arm.answered == 0:
        reasons.append("该说明还没有窗口内答卷，风险无法判定")
    risk_condition = (
        matured_ok
        and arm.answered > 0
        and risk_itt_gate[1] <= maximum_false_certainty
    )
    if matured_ok and arm.answered > 0 and not risk_condition:
        reasons.append(
            f"按原分配的错误确信上界 {risk_itt_gate[1]:.3f} 超过可接受上限 {maximum_false_certainty:.3f}"
        )
    if risk_worst_gate[1] > maximum_false_certainty and arm.missing:
        reasons.append(
            "敏感性：把缺失答卷当作错误确信后上界 "
            f"{risk_worst_gate[1]:.3f}，推广需说明缺失原因"
        )

    # 风险信号只使用事先登记的主要估计量（按原分配，缺失不计阳性）：
    #   risk_alert：区间下界已越过上限，是确定的越界证据；
    #   risk_watch：区间上界仍高于上限，尚不能排除风险。
    # 只算已答家庭的 complete-case 结果仅作敏感性，不能单独用来停用某一范围，
    # 否则等于按“看过并答题的人”挑选范围。
    risk_alert = matured_ok and arm.answered > 0 and risk_itt_gate[0] > maximum_false_certainty
    risk_watch = (
        matured_ok
        and arm.answered > 0
        and not risk_alert
        and risk_itt_gate[1] > maximum_false_certainty
    )

    if not matured_ok or arm.answered == 0:
        status = "collecting"
    elif effect_condition and risk_condition:
        status = "promotable"
    elif risk_alert:
        status = "risk_alert"
        reasons.append("已有证据显示该说明的错误确信比例越过可接受上限")
    elif effect_gate[1] < minimum_gain:
        status = "not_supported"
        reasons.append("效果区间上界低于最低有意义增益，继续收集也难以支持推广")
    else:
        status = "collecting"

    return PairEvaluation(
        arm=arm.arm,
        reference=reference.arm,
        effect=effect,
        effect_cs_marginal=effect_marginal,
        effect_cs_gate=effect_gate,
        risk_itt_cs_marginal=risk_itt_marginal,
        risk_itt_cs_gate=risk_itt_gate,
        risk_complete_cs_gate=risk_complete_gate,
        risk_worst_cs_gate=risk_worst_gate,
        gate_alpha=gate_alpha,
        marginal_alpha=marginal_alpha,
        minimum_gain=minimum_gain,
        maximum_false_certainty=maximum_false_certainty,
        effect_condition=effect_condition,
        risk_condition=risk_condition,
        risk_alert=risk_alert,
        risk_watch=risk_watch,
        status=status,
        reasons=reasons,
        arm_summary=_summary(arm),
        reference_summary=_summary(reference),
    )


def scope_conclusion(pairs: Iterable[PairEvaluation], *, undecidable: bool) -> str:
    pairs = list(pairs)
    if undecidable:
        return "undecidable"
    if not pairs:
        return "collecting"
    if any(pair.status == "promotable" for pair in pairs):
        return "promotable"
    if any(pair.status == "risk_alert" for pair in pairs):
        return "risk_alert"
    if any(pair.risk_watch for pair in pairs):
        # 尚不能排除风险，不算“继续收集”，也不允许推广；明确单独标出。
        return "risk_watch"
    if all(pair.status == "not_supported" for pair in pairs):
        return "not_supported"
    return "collecting"


def multiple_comparison_alpha(base_alpha: float, arms_compared: int, planned_scopes: int) -> float:
    """把整个登记范围的错误推广机会拆分到各说明和各登记范围。"""
    divisor = max(1, arms_compared) * max(1, planned_scopes)
    return base_alpha / divisor
