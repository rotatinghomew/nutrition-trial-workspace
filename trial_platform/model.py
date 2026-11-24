"""从资料表构造可查询的试验状态。

所有时间一律解析成带时区 ``datetime``。业务代码不依赖记录在文件中的顺序，
跨表关联只使用协议明确字段。编号只作为键，不把演练资料里的说明编号写成产品规则。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from materials import read_bundle


def parse_time(value: str) -> datetime:
    """解析带时区 ISO 8601；不带时区视为资料错误。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"时点缺少时区: {value!r}")
    return parsed


def parse_scope(scope: str) -> tuple[str | None, str | None]:
    """``人群:渠道``；``all`` 表示该维不限。渠道为空时表示不限渠道。"""
    if not scope:
        return (None, None)
    segment, _, channel = scope.partition(":")
    segment = None if segment in ("", "all") else segment
    channel = None if channel in ("", "all") else channel
    return (segment, channel)


@dataclass(frozen=True)
class Experiment:
    id: str
    exclusive_scope: str
    start_at: datetime
    end_at: datetime
    followup_hours: float
    minimum_gain: float
    maximum_false_certainty: float
    look_policy: str
    arms: tuple[str, ...]

    @property
    def followup(self) -> timedelta:
        return timedelta(hours=self.followup_hours)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Experiment":
        return cls(
            id=row["id"],
            exclusive_scope=row["exclusive_scope"],
            start_at=parse_time(row["start_at"]),
            end_at=parse_time(row["end_at"]),
            followup_hours=float(row["followup_hours"]),
            minimum_gain=float(row["minimum_gain"]),
            maximum_false_certainty=float(row["maximum_false_certainty"]),
            look_policy=row["look_policy"],
            arms=tuple(row["arms"]),
        )


@dataclass(frozen=True)
class Household:
    id: str
    segment: str
    region: str
    devices: tuple[str, ...]
    eligible_from: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Household":
        return cls(
            id=row["id"],
            segment=row["segment"],
            region=row["region"],
            devices=tuple(row["devices"]),
            eligible_from=parse_time(row["eligible_from"]),
        )


@dataclass(frozen=True)
class Assignment:
    id: str
    household_id: str
    experiment_id: str
    opportunity_at: datetime
    known_at: datetime
    arm: str
    probability: float | None
    policy_id: str
    request_key: str
    device: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Assignment":
        return cls(
            id=row["id"],
            household_id=row["household_id"],
            experiment_id=row["experiment_id"],
            opportunity_at=parse_time(row["opportunity_at"]),
            known_at=parse_time(row["known_at"]),
            arm=row["arm"],
            probability=None if row["probability"] is None else float(row["probability"]),
            policy_id=row["policy_id"],
            request_key=row["request_key"],
            device=row["device"],
        )


@dataclass(frozen=True)
class Display:
    id: str
    assignment_id: str
    occurred_at: datetime
    known_at: datetime
    channel: str
    status: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Display":
        return cls(
            id=row["id"],
            assignment_id=row["assignment_id"],
            occurred_at=parse_time(row["occurred_at"]),
            known_at=parse_time(row["known_at"]),
            channel=row["channel"],
            status=row["status"],
        )


@dataclass(frozen=True)
class Answer:
    id: str
    assignment_id: str
    answered_at: datetime
    known_at: datetime
    correct: bool
    false_certainty: bool
    revision: float

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Answer":
        return cls(
            id=row["id"],
            assignment_id=row["assignment_id"],
            answered_at=parse_time(row["answered_at"]),
            known_at=parse_time(row["known_at"]),
            correct=bool(row["correct"]),
            false_certainty=bool(row["false_certainty"]),
            revision=float(row["revision"]),
        )


@dataclass(frozen=True)
class Decision:
    id: str
    experiment_id: str
    scope: str
    kind: str
    at: datetime
    known_at: datetime
    reference: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Decision":
        return cls(
            id=row["id"],
            experiment_id=row["experiment_id"],
            scope=row["scope"],
            kind=row["kind"],
            at=parse_time(row["at"]),
            known_at=parse_time(row["known_at"]),
            reference=row["reference"],
        )


@dataclass(frozen=True)
class AccessRequest:
    id: str
    household_id: str
    experiment_id: str
    request_key: str
    at: datetime
    channel: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "AccessRequest":
        return cls(
            id=row["id"],
            household_id=row["household_id"],
            experiment_id=row["experiment_id"],
            request_key=row["request_key"],
            at=parse_time(row["at"]),
            channel=row["channel"],
        )


@dataclass
class DataIntegrityIssue:
    """资料层面的不可判定原因。出现时相关结论只能降级，不能静默删除。"""

    code: str
    detail: str
    assignment_id: str | None = None
    household_id: str | None = None
    experiment_id: str | None = None


@dataclass
class Bundle:
    experiments: dict[str, Experiment]
    households: dict[str, Household]
    assignments: dict[str, Assignment]
    displays: dict[str, Display]
    answers: dict[str, Answer]
    decisions: dict[str, Decision]
    access_requests: dict[str, AccessRequest]
    issues: list[DataIntegrityIssue] = field(default_factory=list)

    # -- 常用索引 -------------------------------------------------------
    def displays_by_assignment(self, assignment_id: str) -> list[Display]:
        return [d for d in self.displays.values() if d.assignment_id == assignment_id]

    def answers_by_assignment(self, assignment_id: str) -> list[Answer]:
        return [o for o in self.answers.values() if o.assignment_id == assignment_id]

    def assignments_by_experiment(self, experiment_id: str) -> list[Assignment]:
        return [a for a in self.assignments.values() if a.experiment_id == experiment_id]

    def assignments_by_household(self, household_id: str) -> list[Assignment]:
        return [a for a in self.assignments.values() if a.household_id == household_id]

    def decisions_by_experiment(self, experiment_id: str) -> list[Decision]:
        return sorted(
            (d for d in self.decisions.values() if d.experiment_id == experiment_id),
            key=lambda d: (d.at, d.known_at, d.id),
        )

    def effective_display(self, assignment_id: str) -> Display | None:
        """同一分配可能有重复回执；按渠道动作时间取最早，再按可知时间稳定。"""
        displays = self.displays_by_assignment(assignment_id)
        if not displays:
            return None
        return min(displays, key=lambda d: (d.occurred_at, d.known_at, d.id))


def load_bundle(root: str | Path | None = None) -> Bundle:
    _, tables = read_bundle(root)
    experiments = {r["id"]: Experiment.from_row(r) for r in tables["experiments"]}
    households = {r["id"]: Household.from_row(r) for r in tables["households"]}
    assignments = {r["id"]: Assignment.from_row(r) for r in tables["assignments"]}
    displays = {r["id"]: Display.from_row(r) for r in tables["displays"]}
    answers = {r["id"]: Answer.from_row(r) for r in tables["answers"]}
    decisions = {r["id"]: Decision.from_row(r) for r in tables["decisions"]}
    requests = {r["id"]: AccessRequest.from_row(r) for r in tables["access_requests"]}
    bundle = Bundle(
        experiments=experiments,
        households=households,
        assignments=assignments,
        displays=displays,
        answers=answers,
        decisions=decisions,
        access_requests=requests,
    )
    bundle.issues = check_integrity(bundle)
    return bundle


def check_integrity(bundle: Bundle) -> list[DataIntegrityIssue]:
    """跨表一致性。每一处都记录成明确问题，供推断层降级。"""
    issues: list[DataIntegrityIssue] = []

    for a in bundle.assignments.values():
        exp = bundle.experiments.get(a.experiment_id)
        hh = bundle.households.get(a.household_id)
        if exp is None:
            issues.append(
                DataIntegrityIssue(
                    "unknown_experiment", f"分配 {a.id} 引用未知试验 {a.experiment_id}",
                    assignment_id=a.id, household_id=a.household_id, experiment_id=a.experiment_id,
                )
            )
            continue
        if hh is None:
            issues.append(
                DataIntegrityIssue(
                    "unknown_household", f"分配 {a.id} 引用未知家庭 {a.household_id}",
                    assignment_id=a.id, household_id=a.household_id, experiment_id=a.experiment_id,
                )
            )
            continue
        if a.arm not in exp.arms:
            issues.append(
                DataIntegrityIssue(
                    "arm_not_registered", f"分配 {a.id} 的说明 {a.arm!r} 不在试验登记 {exp.id} 的候选内",
                    assignment_id=a.id, household_id=a.household_id, experiment_id=exp.id,
                )
            )
        if a.device not in hh.devices:
            issues.append(
                DataIntegrityIssue(
                    "device_not_of_household", f"分配 {a.id} 设备 {a.device!r} 不属于家庭 {hh.id}",
                    assignment_id=a.id, household_id=hh.id, experiment_id=exp.id,
                )
            )
        if a.opportunity_at < hh.eligible_from:
            issues.append(
                DataIntegrityIssue(
                    "opportunity_before_eligibility", f"分配 {a.id} 机会早于家庭合资格时间",
                    assignment_id=a.id, household_id=hh.id, experiment_id=exp.id,
                )
            )
        if a.opportunity_at < exp.start_at or a.opportunity_at >= exp.end_at:
            issues.append(
                DataIntegrityIssue(
                    "opportunity_outside_window", f"分配 {a.id} 机会不在试验收集窗内",
                    assignment_id=a.id, household_id=hh.id, experiment_id=exp.id,
                )
            )
        if a.probability is None:
            issues.append(
                DataIntegrityIssue(
                    "missing_probability", f"分配 {a.id} 缺少当时实际采用的分流概率",
                    assignment_id=a.id, household_id=hh.id, experiment_id=exp.id,
                )
            )
        elif not (0.0 <= a.probability <= 1.0):
            issues.append(
                DataIntegrityIssue(
                    "invalid_probability", f"分配 {a.id} 概率不在 0 至 1",
                    assignment_id=a.id, household_id=hh.id, experiment_id=exp.id,
                )
            )

    # 同一家庭在同一互斥范围内不能同时进入两个试验。
    by_household: dict[str, list[Assignment]] = {}
    for a in bundle.assignments.values():
        by_household.setdefault(a.household_id, []).append(a)
    for household_id, rows in by_household.items():
        by_scope: dict[str, list[Assignment]] = {}
        for a in rows:
            exp = bundle.experiments.get(a.experiment_id)
            if exp is not None:
                by_scope.setdefault(exp.exclusive_scope, []).append(a)
        for scope, scoped in by_scope.items():
            experiments = {a.experiment_id for a in scoped}
            if len(experiments) > 1:
                issues.append(
                    DataIntegrityIssue(
                        "exclusive_conflict",
                        f"家庭 {household_id} 同时进入互斥范围 {scope!r} 的试验 {sorted(experiments)}",
                        household_id=household_id,
                    )
                )
        if len(rows) > 1:
            # 同一家庭同一试验的多次记录也可能是重复请求；保留但标出。
            per_exp: dict[str, list[Assignment]] = {}
            for a in rows:
                per_exp.setdefault(a.experiment_id, []).append(a)
            for exp_id, exp_rows in per_exp.items():
                if len(exp_rows) > 1:
                    issues.append(
                        DataIntegrityIssue(
                            "repeat_household_assignment",
                            f"家庭 {household_id} 在试验 {exp_id} 有多条分配；只以原始分配为准",
                            household_id=household_id, experiment_id=exp_id,
                        )
                    )

    for d in bundle.displays.values():
        a = bundle.assignments.get(d.assignment_id)
        if a is None:
            issues.append(
                DataIntegrityIssue("orphan_display", f"回执 {d.id} 引用未知分配 {d.assignment_id}")
            )
            continue
        if d.occurred_at < a.opportunity_at:
            issues.append(
                DataIntegrityIssue(
                    "display_before_opportunity", f"回执 {d.id} 早于分配机会",
                    assignment_id=a.id, household_id=a.household_id, experiment_id=a.experiment_id,
                )
            )
        if d.status not in ("displayed", "unknown"):
            issues.append(
                DataIntegrityIssue(
                    "invalid_display_status", f"回执 {d.id} 状态 {d.status!r} 不在协议内",
                    assignment_id=a.id,
                )
            )

    for o in bundle.answers.values():
        a = bundle.assignments.get(o.assignment_id)
        if a is None:
            issues.append(
                DataIntegrityIssue("orphan_answer", f"答卷 {o.id} 引用未知分配 {o.assignment_id}")
            )
            continue
        if o.answered_at < a.opportunity_at:
            issues.append(
                DataIntegrityIssue(
                    "answer_before_opportunity", f"答卷 {o.id} 早于分配机会",
                    assignment_id=a.id, household_id=a.household_id, experiment_id=a.experiment_id,
                )
            )
        if o.known_at < o.answered_at:
            issues.append(
                DataIntegrityIssue(
                    "answer_known_before_answered", f"答卷 {o.id} 的可知时间早于作答时间",
                    assignment_id=a.id,
                )
            )

    for r in bundle.access_requests.values():
        if r.household_id not in bundle.households:
            issues.append(
                DataIntegrityIssue(
                    "request_unknown_household", f"访问请求 {r.id} 引用未知家庭",
                    household_id=r.household_id, experiment_id=r.experiment_id,
                )
            )
        if r.experiment_id not in bundle.experiments:
            issues.append(
                DataIntegrityIssue(
                    "request_unknown_experiment", f"访问请求 {r.id} 引用未知试验",
                    household_id=r.household_id, experiment_id=r.experiment_id,
                )
            )
    return issues


def issues_for(
    issues: Iterable[DataIntegrityIssue],
    *,
    experiment_id: str | None = None,
    assignment_ids: set[str] | None = None,
    household_ids: set[str] | None = None,
) -> list[DataIntegrityIssue]:
    """筛选会影响某个结论范围的问题。

    只保留该范围实际使用的分配或家庭上的问题；范围之外的问题不能顺带把结论降级，
    范围之内的问题也不能被忽略。``experiment_id`` 为空的问题（例如互斥冲突）按其
    涉及的家庭是否在使用范围内判断。
    """
    selected = []
    for issue in issues:
        if experiment_id is not None and issue.experiment_id not in (None, experiment_id):
            continue
        if issue.assignment_id is not None:
            if assignment_ids is not None and issue.assignment_id not in assignment_ids:
                continue
        elif issue.household_id is not None:
            if household_ids is not None and issue.household_id not in household_ids:
                continue
        elif experiment_id is not None:
            # 既不指向家庭也不指向分配的问题（目前没有）不牵连具体试验。
            continue
        selected.append(issue)
    return selected
