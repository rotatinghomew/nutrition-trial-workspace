"""试验运行引擎：登记、分配、回执、许可、成熟度、范围决定与投放接管。

状态 = 原始资料表 + 只追加的本地事件流。引擎不修改原始表；``reset`` 只删除本地
事件流。所有结论都带 ``as_of``，即“当时能使用的资料”，以便回看某天当时能作的
决定。

要点：

- 合资格访问在展示之前登记为分配，重复请求与设备切换返回同一随机单位；
- 家庭不能同时加入同一互斥范围的两个试验；
- 每次分配记录当时政策、实际采用概率与入组原因；概率缺失原样保留并降级结论；
- 展示许可先发放后兑现；回执可重复、可长期不明；停止新增分配时未兑现许可转为
  明确的撤销状态，已经发生的展示仍可兑现；
- 答卷窗以机会形成时间起算，提前答卷、窗口内答卷、逾期答卷、仍在等待分开；
- 主要比较按原分配；缺失答卷不悄悄剔除；
- 推广必须同时满足效果与风险条件、未被降级，并由批准负责人确认；接管只覆盖
  已回执的范围，未接管范围继续试验。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from . import inference
from .events import EventStore, default_state_dir
from .model import (
    Assignment,
    Decision,
    Experiment,
    Household,
    issues_for,
    load_bundle,
    parse_scope,
    parse_time,
)

BASE_ALPHA = 0.05
MIN_MATURED_PER_ARM = 20

STRUCTURAL_ISSUES = {
    "missing_probability",
    "invalid_probability",
    "exclusive_conflict",
    "arm_not_registered",
    "device_not_of_household",
    "opportunity_before_eligibility",
    "repeat_household_assignment",
    "answer_before_opportunity",
    "display_before_opportunity",
    "answer_known_before_answered",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _stable_uniform(*parts: str) -> float:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _covered(scope: tuple[str | None, str | None], segment: str, channel: str | None) -> bool:
    """范围是否覆盖该人群与渠道。渠道未知时，只有不限定渠道的范围才算覆盖。"""
    want_segment, want_channel = scope
    if want_segment is not None and want_segment != segment:
        return False
    if want_channel is None:
        return True
    return channel is not None and want_channel == channel


def _scope_is_subset(inner: str, outer: str) -> bool:
    """``inner`` 的生效范围是否被 ``outer`` 完全覆盖。

    ``all`` 或空渠道表示不限渠道，属于更宽的范围；它不能被只覆盖某一个渠道的
    外层范围完全覆盖。
    """
    inner_segment, inner_channel = parse_scope(inner)
    outer_segment, outer_channel = parse_scope(outer)
    if outer_segment is not None and inner_segment != outer_segment:
        return False
    if outer_channel is not None and inner_channel != outer_channel:
        return False
    return True


class EngineError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


@dataclass
class ExperimentConfig:
    experiment: Experiment
    target_segment: str | None = None
    reference_arm: str | None = None
    approved_leads: tuple[str, ...] = ()
    look_dates: tuple[str, ...] = ()
    planned_scopes: tuple[str, ...] = ()
    registered_by: str | None = None
    registered_at: datetime | None = None
    source: str = "materials"

    def as_dict(self) -> dict:
        return {
            "experiment_id": self.experiment.id,
            "exclusive_scope": self.experiment.exclusive_scope,
            "start_at": to_iso(self.experiment.start_at),
            "end_at": to_iso(self.experiment.end_at),
            "followup_hours": self.experiment.followup_hours,
            "minimum_gain": self.experiment.minimum_gain,
            "maximum_false_certainty": self.experiment.maximum_false_certainty,
            "look_policy": self.experiment.look_policy,
            "arms": list(self.experiment.arms),
            "target_segment": self.target_segment,
            "reference_arm": self.reference_arm or (self.experiment.arms[0] if self.experiment.arms else None),
            "approved_leads": list(self.approved_leads),
            "look_dates": list(self.look_dates),
            "planned_scopes": list(self.planned_scopes),
            "registered_by": self.registered_by,
            "registered_at": to_iso(self.registered_at),
            "source": self.source,
        }


@dataclass
class PolicyVersion:
    policy_id: str
    experiment_id: str
    weights: dict[str, float]
    effective_at: datetime
    created_at: datetime
    created_by: str | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "experiment_id": self.experiment_id,
            "weights": self.weights,
            "effective_at": to_iso(self.effective_at),
            "created_at": to_iso(self.created_at),
            "created_by": self.created_by,
            "note": self.note,
        }


@dataclass
class TrialAssignment:
    assignment: Assignment
    origin: str
    enrollment_reason: str
    known_at: datetime

    def as_dict(self) -> dict:
        a = self.assignment
        return {
            "assignment_id": a.id,
            "household_id": a.household_id,
            "experiment_id": a.experiment_id,
            "arm": a.arm,
            "probability": a.probability,
            "policy_id": a.policy_id,
            "opportunity_at": to_iso(a.opportunity_at),
            "known_at": to_iso(self.known_at),
            "device": a.device,
            "request_key": a.request_key,
            "origin": self.origin,
            "enrollment_reason": self.enrollment_reason,
        }


@dataclass
class Receipt:
    receipt_id: str
    assignment_id: str
    occurred_at: datetime
    known_at: datetime
    channel: str
    status: str
    source: str


@dataclass
class Response:
    response_id: str
    assignment_id: str
    answered_at: datetime
    known_at: datetime
    correct: bool
    false_certainty: bool
    revision: float
    source: str


@dataclass
class Handover:
    id: str
    household_id: str
    experiment_id: str
    request_key: str
    at: datetime
    channel: str
    promotion_id: str
    served_by: str
    known_at: datetime


@dataclass
class AccessOutcome:
    status: str  # assigned / duplicate_assignment / duplicate_household / refused / handed_over
    assignment: Assignment | None = None
    reason: str | None = None
    handover: Handover | None = None

    def as_dict(self) -> dict:
        body = {"status": self.status, "reason": self.reason}
        if self.assignment is not None:
            a = self.assignment
            body["assignment"] = {
                "assignment_id": a.id,
                "household_id": a.household_id,
                "experiment_id": a.experiment_id,
                "arm": a.arm,
                "probability": a.probability,
                "policy_id": a.policy_id,
                "opportunity_at": to_iso(a.opportunity_at),
                "device": a.device,
                "request_key": a.request_key,
            }
        if self.handover is not None:
            body["handover"] = {
                "id": self.handover.id,
                "promotion_id": self.handover.promotion_id,
                "served_by": self.handover.served_by,
            }
        return body


@dataclass
class AssignmentView:
    assignment: Assignment
    segment: str
    channel: str | None
    channel_source: str
    matured: bool
    window_closes_at: datetime
    display_state: str  # displayed / unknown / none / revoked_before_show / post_stop_display
    answer_state: str  # answered / waiting / missing / outside_window
    answer_flags: list[str]
    correct: bool
    false_certainty: bool
    revision: float | None
    known_at: datetime
    late_response: Response | None = None

    def as_dict(self) -> dict:
        a = self.assignment
        return {
            "assignment_id": a.id,
            "household_id": a.household_id,
            "segment": self.segment,
            "channel": self.channel,
            "channel_source": self.channel_source,
            "arm": a.arm,
            "probability": a.probability,
            "policy_id": a.policy_id,
            "opportunity_at": to_iso(a.opportunity_at),
            "window_closes_at": to_iso(self.window_closes_at),
            "matured": self.matured,
            "display_state": self.display_state,
            "answer_state": self.answer_state,
            "answer_flags": self.answer_flags,
            "correct": self.correct,
            "false_certainty": self.false_certainty,
            "revision": self.revision,
            "known_at": to_iso(self.known_at),
        }


@dataclass
class ScopeAnalysis:
    experiment_id: str
    as_of: datetime
    scope: str
    segment: str | None
    channel: str | None
    assignments: list[AssignmentView]
    arms: dict[str, inference.ArmOutcome]
    pairs: list[inference.PairEvaluation]
    conclusion: str
    undecidable: bool
    undecidable_reasons: list[dict]
    channel_attribution_incomplete: bool
    planned_scope_count: int
    gate_alpha: float
    notes: list[str] = field(default_factory=list)
    display_deviation: dict = field(default_factory=dict)
    answer_missing: dict = field(default_factory=dict)
    policy_versions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "as_of": to_iso(self.as_of),
            "scope": self.scope,
            "segment": self.segment,
            "channel": self.channel,
            "conclusion": self.conclusion,
            "undecidable": self.undecidable,
            "undecidable_reasons": self.undecidable_reasons,
            "channel_attribution_incomplete": self.channel_attribution_incomplete,
            "planned_scope_count": self.planned_scope_count,
            "gate_alpha": self.gate_alpha,
            "notes": self.notes,
            "assignment_probability_model": {
                "policy_versions": self.policy_versions,
                "note": "分配概率随获批政策版本变化；主要比较按原分配，不按实际展示调整",
            },
            "sensitivity": {
                "display_deviation": self.display_deviation,
                "answer_missing": self.answer_missing,
            },
            "arms": {arm: inference._summary(outcome) for arm, outcome in self.arms.items()},
            "comparisons": [pair.as_dict() for pair in self.pairs],
            "assignments": [view.as_dict() for view in self.assignments],
        }


@dataclass
class PermitView:
    permit_id: str
    assignment_id: str
    household_id: str
    experiment_id: str
    segment: str
    channel: str | None
    issued_at: datetime
    state: str
    receipt_uncertain: bool
    scope_uncertain: bool
    redeemed_at: datetime | None
    revoked_at: datetime | None
    revoke_decision: str | None
    post_stop_display: bool

    def as_dict(self) -> dict:
        return {
            "permit_id": self.permit_id,
            "assignment_id": self.assignment_id,
            "household_id": self.household_id,
            "experiment_id": self.experiment_id,
            "segment": self.segment,
            "channel": self.channel,
            "issued_at": to_iso(self.issued_at),
            "state": self.state,
            "receipt_uncertain": self.receipt_uncertain,
            "scope_uncertain": self.scope_uncertain,
            "redeemed_at": to_iso(self.redeemed_at),
            "revoked_at": to_iso(self.revoked_at),
            "revoke_decision": self.revoke_decision,
            "post_stop_display": self.post_stop_display,
        }


class Engine:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        state_dir: str | Path | None = None,
        replay: bool = True,
        clock=None,
        fsync: bool = True,
    ):
        self.root = Path(root or Path(__file__).resolve().parents[1])
        self._clock = clock or now_utc
        self.base = load_bundle(self.root)
        self.state_dir = Path(state_dir) if state_dir else default_state_dir(self.root)
        self.store = EventStore(self.state_dir / "events.jsonl", fsync=fsync)
        self._config_overrides: dict[str, ExperimentConfig] = {}
        self._runtime_households: dict[str, Household] = {}
        self._runtime_assignments: dict[str, TrialAssignment] = {}
        self._runtime_receipts: dict[str, Receipt] = {}
        self._runtime_responses: dict[str, Response] = {}
        self._runtime_decisions: dict[str, Decision] = {}
        self._policies: dict[str, PolicyVersion] = {}
        self._handovers: dict[str, Handover] = {}
        self._access_index: dict[tuple[str, str], TrialAssignment] = {}
        self._household_experiment: dict[tuple[str, str], TrialAssignment] = {}
        self._receipt_index: dict[tuple[str, str], Receipt] = {}
        self._response_index: dict[tuple[str, float], Response] = {}
        self._refused_access: list[dict] = []
        self._base_request_channels: dict[tuple[str, str], str] = {
            (request.experiment_id, request.request_key): request.channel
            for request in self.base.access_requests.values()
        }
        self._runtime_request_channels: dict[tuple[str, str], str] = {}
        self._base_trials: dict[str, TrialAssignment] = {}
        for assignment in self.base.assignments.values():
            reason = (
                "historical_assignment_probability_missing"
                if assignment.probability is None
                else f"historical_assignment_policy:{assignment.policy_id}"
            )
            self._base_trials[assignment.id] = TrialAssignment(
                assignment=assignment,
                origin="materials",
                enrollment_reason=reason,
                known_at=assignment.known_at,
            )
        self._lock = RLock()
        self._index_cache: dict = {}
        self._household_index: dict[str, list[TrialAssignment]] = {}
        for trial in self._base_trials.values():
            self._household_index.setdefault(trial.assignment.household_id, []).append(trial)
        self._install_historical_indexes()
        if replay:
            self._replay()

    # ------------------------------------------------------------------
    # 重放
    # ------------------------------------------------------------------
    def _install_historical_indexes(self) -> None:
        for trial in self._base_trials.values():
            self._access_index.setdefault((trial.assignment.experiment_id, trial.assignment.request_key), trial)
            self._household_experiment.setdefault(
                (trial.assignment.household_id, trial.assignment.experiment_id), trial
            )

    def _historical_assignments(self) -> dict[str, TrialAssignment]:
        return dict(self._base_trials)

    def _replay(self) -> None:
        for event in self.store.iter_events():
            try:
                self._apply_event(event)
            except EngineError as exc:
                self._refused_access.append(
                    {"seq": event["seq"], "type": event["type"], "error": exc.code, "detail": exc.detail}
                )

    def _apply_event(self, event: dict) -> AccessOutcome | None:
        kind = event["type"]
        payload = event["payload"]
        recorded_at = parse_time(event["recorded_at"])
        if kind == "experiment_registered":
            experiment = Experiment(
                id=payload["experiment_id"],
                exclusive_scope=payload["exclusive_scope"],
                start_at=parse_time(payload["start_at"]),
                end_at=parse_time(payload["end_at"]),
                followup_hours=float(payload["followup_hours"]),
                minimum_gain=float(payload["minimum_gain"]),
                maximum_false_certainty=float(payload["maximum_false_certainty"]),
                look_policy=payload["look_policy"],
                arms=tuple(payload["arms"]),
            )
            self._config_overrides[experiment.id] = ExperimentConfig(
                experiment=experiment,
                target_segment=payload.get("target_segment"),
                reference_arm=payload.get("reference_arm") or experiment.arms[0],
                approved_leads=tuple(payload.get("approved_leads") or ()),
                look_dates=tuple(payload.get("look_dates") or ()),
                planned_scopes=tuple(payload.get("planned_scopes") or ()),
                registered_by=payload.get("registered_by"),
                registered_at=parse_time(payload["registered_at"]) if payload.get("registered_at") else recorded_at,
                source="runtime",
            )
        elif kind == "experiment_configured":
            config = self.config(payload["experiment_id"])
            self._config_overrides[config.experiment.id] = ExperimentConfig(
                experiment=config.experiment,
                target_segment=payload.get("target_segment", config.target_segment),
                reference_arm=payload.get("reference_arm", config.reference_arm),
                approved_leads=tuple(payload.get("approved_leads") or config.approved_leads),
                look_dates=tuple(payload.get("look_dates") or config.look_dates),
                planned_scopes=tuple(payload.get("planned_scopes") or config.planned_scopes),
                registered_by=config.registered_by,
                registered_at=config.registered_at,
                source=config.source,
            )
        elif kind == "household_registered":
            self._runtime_households[payload["household_id"]] = Household(
                id=payload["household_id"],
                segment=payload["segment"],
                region=payload["region"],
                devices=tuple(payload["devices"]),
                eligible_from=parse_time(payload["eligible_from"]),
            )
        elif kind == "policy_registered":
            policy = PolicyVersion(
                policy_id=payload["policy_id"],
                experiment_id=payload["experiment_id"],
                weights={k: float(v) for k, v in payload["weights"].items()},
                effective_at=parse_time(payload["effective_at"]),
                created_at=parse_time(payload["created_at"]),
                created_by=payload.get("created_by"),
                note=payload.get("note"),
            )
            self._policies[policy.policy_id] = policy
        elif kind == "access_requested":
            # 事件里保存的是当时判定出的结果；重放只采纳它，不按今天的资料重新判定。
            status = payload.get("outcome_status", "assigned")
            if status == "assigned" and "arm" not in payload:
                # 兼容旧事件：没有保存判定结果时只能按当前状态重算，并留下审计提示。
                outcome = self._resolve_access(payload, recorded_at=recorded_at)
                status = outcome.status
            if payload.get("channel"):
                self._runtime_request_channels[
                    (payload["experiment_id"], payload["request_key"])
                ] = payload["channel"]
            if status == "assigned":
                assignment = self._assignment_from_payload(payload, recorded_at)
                trial = TrialAssignment(
                    assignment=assignment,
                    origin="runtime",
                    enrollment_reason=payload.get("enrollment_reason", "new_access"),
                    known_at=recorded_at,
                )
                self._install_assignment(trial)
                return AccessOutcome("assigned", assignment=assignment, reason=payload.get("outcome_reason"))
            if status == "handed_over":
                handover = Handover(
                    id="handover-" + hashlib.sha256(
                        f"{payload['experiment_id']}|{payload['request_key']}".encode()
                    ).hexdigest()[:16],
                    household_id=payload["household_id"],
                    experiment_id=payload["experiment_id"],
                    request_key=payload["request_key"],
                    at=parse_time(payload["at"]),
                    channel=payload["channel"],
                    promotion_id=payload.get("promotion_id", ""),
                    served_by=payload.get("served_by", ""),
                    known_at=recorded_at,
                )
                self._handovers[handover.id] = handover
                return AccessOutcome("handed_over", handover=handover, reason=payload.get("outcome_reason"))
            if status == "refused":
                self._refused_access.append(payload)
            return AccessOutcome(status, reason=payload.get("outcome_reason"))
        elif kind == "access_refused":
            # 兼容旧事件：直接记录的拒绝请求。
            self._refused_access.append(payload)
        elif kind == "receipt_received":
            receipt = Receipt(
                receipt_id=payload["receipt_id"],
                assignment_id=payload["assignment_id"],
                occurred_at=parse_time(payload["occurred_at"]),
                known_at=parse_time(payload["known_at"]),
                channel=payload["channel"],
                status=payload["status"],
                source="runtime",
            )
            self._runtime_receipts[receipt.receipt_id] = receipt
            if payload.get("receipt_key"):
                self._receipt_index[(receipt.assignment_id, payload["receipt_key"])] = receipt
        elif kind == "response_received":
            response = Response(
                response_id=payload["response_id"],
                assignment_id=payload["assignment_id"],
                answered_at=parse_time(payload["answered_at"]),
                known_at=parse_time(payload["known_at"]),
                correct=bool(payload["correct"]),
                false_certainty=bool(payload["false_certainty"]),
                revision=float(payload["revision"]),
                source="runtime",
            )
            self._runtime_responses[response.response_id] = response
            self._response_index[(response.assignment_id, response.revision)] = response
        elif kind == "decision_recorded":
            self._runtime_decisions[payload["decision_id"]] = Decision(
                id=payload["decision_id"],
                experiment_id=payload["experiment_id"],
                scope=payload["scope"],
                kind=payload["kind"],
                at=parse_time(payload["at"]),
                known_at=parse_time(payload["known_at"]),
                reference=payload.get("reference", ""),
            )
        else:
            raise EngineError(400, "unknown_event", f"未知事件类型 {kind}")
        return None

    def _assignment_from_payload(self, payload: dict, recorded_at: datetime) -> Assignment:
        experiment_id = payload["experiment_id"]
        household_id = payload["household_id"]
        request_key = payload["request_key"]
        expected_id = "a-" + hashlib.sha256(f"{experiment_id}|{household_id}|{request_key}".encode()).hexdigest()[:16]
        household = self.households().get(household_id)
        return Assignment(
            id=expected_id,
            household_id=household_id,
            experiment_id=experiment_id,
            opportunity_at=parse_time(payload["at"]),
            known_at=recorded_at,
            arm=payload["arm"],
            probability=payload.get("probability"),
            policy_id=payload["policy_id"],
            request_key=request_key,
            device=payload.get("device") or (household.devices[0] if household and household.devices else ""),
        )

    def _install_assignment(self, trial: TrialAssignment) -> None:
        self._runtime_assignments[trial.assignment.id] = trial
        self._household_index.setdefault(trial.assignment.household_id, []).append(trial)
        self._access_index[(trial.assignment.experiment_id, trial.assignment.request_key)] = trial
        self._household_experiment[(trial.assignment.household_id, trial.assignment.experiment_id)] = trial

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------
    def experiments(self) -> dict[str, ExperimentConfig]:
        rows = {
            eid: ExperimentConfig(
                experiment=exp,
                reference_arm=exp.arms[0] if exp.arms else None,
                source="materials",
            )
            for eid, exp in self.base.experiments.items()
        }
        rows.update(self._config_overrides)
        return rows

    def config(self, experiment_id: str) -> ExperimentConfig:
        configs = self.experiments()
        if experiment_id not in configs:
            raise EngineError(404, "unknown_experiment", f"未知试验 {experiment_id}")
        return configs[experiment_id]

    def households(self) -> dict[str, Household]:
        rows = dict(self.base.households)
        rows.update(self._runtime_households)
        return rows

    def assignments(self) -> dict[str, TrialAssignment]:
        rows = self._historical_assignments()
        rows.update(self._runtime_assignments)
        return rows

    def assignments_by_household(self, household_id: str) -> list[TrialAssignment]:
        rows: list[TrialAssignment] = list(self._household_index.get(household_id, ()))
        return rows

    def receipts(self) -> list[Receipt]:
        cached = self._index_cache.get("receipts")
        if cached is not None and cached[0] == (len(self.base.displays), len(self._runtime_receipts)):
            return list(cached[1])
        rows = [
            Receipt(
                receipt_id=d.id,
                assignment_id=d.assignment_id,
                occurred_at=d.occurred_at,
                known_at=d.known_at,
                channel=d.channel,
                status=d.status,
                source="materials",
            )
            for d in self.base.displays.values()
        ]
        rows.extend(self._runtime_receipts.values())
        self._index_cache["receipts"] = ((len(self.base.displays), len(self._runtime_receipts)), rows)
        return list(rows)

    def responses(self) -> list[Response]:
        cached = self._index_cache.get("responses")
        if cached is not None and cached[0] == (len(self.base.answers), len(self._runtime_responses)):
            return list(cached[1])
        rows = [
            Response(
                response_id=o.id,
                assignment_id=o.assignment_id,
                answered_at=o.answered_at,
                known_at=o.known_at,
                correct=o.correct,
                false_certainty=o.false_certainty,
                revision=o.revision,
                source="materials",
            )
            for o in self.base.answers.values()
        ]
        rows.extend(self._runtime_responses.values())
        self._index_cache["responses"] = ((len(self.base.answers), len(self._runtime_responses)), rows)
        return list(rows)

    def decisions(self) -> dict[str, Decision]:
        rows = dict(self.base.decisions)
        rows.update(self._runtime_decisions)
        return rows

    def policies(self) -> dict[str, PolicyVersion]:
        return dict(self._policies)

    def handovers(self) -> list[Handover]:
        return sorted(self._handovers.values(), key=lambda h: (h.at, h.id))

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def register_experiment(self, body: dict) -> dict:
        required = [
            "experiment_id",
            "exclusive_scope",
            "start_at",
            "end_at",
            "followup_hours",
            "minimum_gain",
            "maximum_false_certainty",
            "look_policy",
            "arms",
        ]
        missing = [key for key in required if key not in body]
        if missing:
            raise EngineError(422, "missing_fields", f"缺少登记字段: {missing}")
        experiment_id = str(body["experiment_id"]).strip()
        if not experiment_id:
            raise EngineError(422, "missing_experiment_id", "缺少 experiment_id")
        if experiment_id in self.experiments():
            raise EngineError(409, "duplicate_experiment", f"试验 {experiment_id} 已存在")
        arms = list(dict.fromkeys(str(a) for a in body["arms"]))
        if len(arms) < 2:
            raise EngineError(422, "too_few_arms", "至少登记两个候选说明")
        start_at = parse_time(body["start_at"])
        end_at = parse_time(body["end_at"])
        if end_at <= start_at:
            raise EngineError(422, "invalid_window", "最长观察期必须晚于开始时间")
        if not 0 <= float(body["minimum_gain"]) <= 1:
            raise EngineError(422, "invalid_minimum_gain", "最低有意义的理解改善必须在 0 至 1")
        if not 0 <= float(body["maximum_false_certainty"]) <= 1:
            raise EngineError(422, "invalid_risk_limit", "错误确信上限必须在 0 至 1")
        reference_arm = body.get("reference_arm")
        if reference_arm is not None and reference_arm not in arms:
            raise EngineError(422, "invalid_reference_arm", "参照说明必须在候选内")
        approved = [str(x) for x in body.get("approved_leads") or []]
        if not approved:
            raise EngineError(422, "missing_approved_leads", "必须登记至少一位获批负责人")
        payload = {
            "experiment_id": experiment_id,
            "exclusive_scope": str(body["exclusive_scope"]),
            "start_at": to_iso(start_at),
            "end_at": to_iso(end_at),
            "followup_hours": float(body["followup_hours"]),
            "minimum_gain": float(body["minimum_gain"]),
            "maximum_false_certainty": float(body["maximum_false_certainty"]),
            "look_policy": str(body["look_policy"]),
            "arms": arms,
            "target_segment": body.get("target_segment"),
            "reference_arm": reference_arm or arms[0],
            "approved_leads": approved,
            "look_dates": list(body.get("look_dates") or []),
            "planned_scopes": list(body.get("planned_scopes") or []),
            "registered_by": body.get("registered_by"),
            "registered_at": to_iso(self._clock()),
            "source": "runtime",
        }
        self._apply_event(
            self.store.append("experiment_registered", payload, recorded_at=to_iso(self._clock()), actor=body.get("registered_by"))
        )
        return self.config(experiment_id).as_dict()

    def configure_experiment(self, experiment_id: str, body: dict) -> dict:
        config = self.config(experiment_id)
        reference_arm = body.get("reference_arm", config.reference_arm)
        if reference_arm is not None and reference_arm not in config.experiment.arms:
            raise EngineError(422, "invalid_reference_arm", "参照说明必须在候选内")
        payload = {
            "experiment_id": experiment_id,
            "target_segment": body.get("target_segment", config.target_segment),
            "reference_arm": reference_arm,
            "approved_leads": list(body.get("approved_leads") or config.approved_leads),
            "look_dates": list(body.get("look_dates") or config.look_dates),
            "planned_scopes": list(body.get("planned_scopes") or config.planned_scopes),
        }
        self._apply_event(
            self.store.append("experiment_configured", payload, recorded_at=to_iso(self._clock()), actor=body.get("actor"))
        )
        return self.config(experiment_id).as_dict()

    def register_household(self, body: dict) -> dict:
        household_id = str(body.get("household_id", "")).strip()
        if not household_id:
            raise EngineError(422, "missing_household", "缺少 household_id")
        if household_id in self.households():
            raise EngineError(409, "duplicate_household", f"家庭 {household_id} 已登记")
        devices = [str(d) for d in body.get("devices") or []]
        if not devices:
            raise EngineError(422, "missing_devices", "至少登记一个设备键")
        for existing in self.households().values():
            for device in devices:
                if device in existing.devices:
                    raise EngineError(409, "device_conflict", f"设备 {device} 已属于家庭 {existing.id}")
        payload = {
            "household_id": household_id,
            "segment": str(body["segment"]),
            "region": str(body["region"]),
            "devices": devices,
            "eligible_from": to_iso(parse_time(body["eligible_from"])),
        }
        self._apply_event(
            self.store.append("household_registered", payload, recorded_at=to_iso(self._clock()), actor=body.get("actor"))
        )
        return {"household_id": household_id, "devices": devices}

    def register_policy(self, experiment_id: str, body: dict) -> dict:
        config = self.config(experiment_id)
        policy_id = str(body.get("policy_id", "")).strip()
        if not policy_id:
            raise EngineError(422, "missing_policy_id", "缺少 policy_id")
        if policy_id in self._policies:
            raise EngineError(409, "duplicate_policy", f"分流政策 {policy_id} 已存在")
        weights = {str(k): float(v) for k, v in (body.get("weights") or {}).items()}
        if set(weights) != set(config.experiment.arms):
            raise EngineError(422, "policy_arm_mismatch", "分流权重必须覆盖且只覆盖已登记候选说明")
        if any(w < 0 or w > 1 for w in weights.values()):
            raise EngineError(422, "invalid_weight", "权重必须在 0 至 1")
        if not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-6):
            raise EngineError(422, "weights_not_normalized", "权重之和必须为 1")
        effective_at = parse_time(body["effective_at"])
        if effective_at < config.experiment.start_at:
            raise EngineError(422, "policy_before_start", "政策生效时间不能早于试验开始")
        payload = {
            "policy_id": policy_id,
            "experiment_id": experiment_id,
            "weights": weights,
            "effective_at": to_iso(effective_at),
            "created_at": to_iso(self._clock()),
            "created_by": body.get("created_by"),
            "note": body.get("note"),
        }
        self._apply_event(
            self.store.append("policy_registered", payload, recorded_at=to_iso(self._clock()), actor=body.get("created_by"))
        )
        return self._policies[policy_id].as_dict()

    # ------------------------------------------------------------------
    # 访问
    # ------------------------------------------------------------------
    def _active_policy(self, experiment_id: str, at: datetime) -> PolicyVersion | None:
        candidates = [
            p
            for p in self._policies.values()
            if p.experiment_id == experiment_id and p.effective_at <= at
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p.effective_at, p.created_at, p.policy_id))

    def _active_stops(self, experiment_id: str, at: datetime, *, known_by: datetime | None = None) -> list[Decision]:
        """在该时点生效的停用。

        ``at`` 用于分配判定：按事实生效时间拦截新分配。
        ``known_by`` 用于回溯视图：只计入当时已被项目知道的决定。
        """
        horizon = known_by or at
        rows = [
            d
            for d in self.decisions().values()
            if d.experiment_id == experiment_id
            and d.kind == "risk_stop"
            and d.at <= at
            and d.known_at <= horizon
        ]
        return sorted(rows, key=lambda d: (d.at, d.known_at, d.id))

    def _confirmed_promotions(self, experiment_id: str, at: datetime) -> list[Decision]:
        return sorted(
            (
                d
                for d in self.decisions().values()
                if d.experiment_id == experiment_id
                and d.kind == "promote_confirm"
                and d.at <= at
                and d.known_at <= at
            ),
            key=lambda d: (d.at, d.known_at, d.id),
        )

    def _effective_handover(
        self, experiment_id: str, segment: str, channel: str, at: datetime
    ) -> tuple[Decision, Decision] | None:
        promotions = {d.id: d for d in self._confirmed_promotions(experiment_id, at)}
        acks = sorted(
            (
                d
                for d in self.decisions().values()
                if d.experiment_id == experiment_id
                and d.kind == "takeover_ack"
                and d.at <= at
                and d.known_at <= at
            ),
            key=lambda d: (d.at, d.known_at, d.id),
        )
        for ack in acks:
            promotion = promotions.get(ack.reference)
            if promotion is None:
                continue
            if not _scope_is_subset(ack.scope, promotion.scope):
                continue
            if _covered(parse_scope(ack.scope), segment, channel):
                return (ack, promotion)
        return None

    def _resolve_access(self, payload: dict, *, recorded_at: datetime) -> AccessOutcome:
        household_id = payload["household_id"]
        experiment_id = payload["experiment_id"]
        request_key = payload["request_key"]
        at = parse_time(payload["at"])
        channel = payload.get("channel")
        device = payload.get("device")
        households = self.households()
        if household_id not in households:
            raise EngineError(404, "unknown_household", f"未知家庭 {household_id}")
        if channel is None or not str(channel).strip():
            raise EngineError(422, "missing_channel", "访问请求必须带渠道，用于范围与接管判定")
        channel = str(channel)
        household = households[household_id]
        if experiment_id not in self.experiments():
            raise EngineError(404, "unknown_experiment", f"未知试验 {experiment_id}")
        config = self.config(experiment_id)
        experiment = config.experiment
        if device and device not in household.devices:
            raise EngineError(409, "device_not_of_household", f"设备 {device} 不属于家庭 {household_id}")

        existing = self._access_index.get((experiment_id, request_key))
        if existing is not None:
            if existing.assignment.household_id != household_id:
                raise EngineError(409, "request_key_conflict", "同一请求键被不同家庭使用")
            return AccessOutcome("duplicate_assignment", assignment=existing.assignment, reason="重复请求返回原分配")

        same_household = self._household_experiment.get((household_id, experiment_id))
        if same_household is not None:
            return AccessOutcome(
                "duplicate_household",
                assignment=same_household.assignment,
                reason="同一家庭在同一试验只形成一个随机单位，设备切换不重新分配",
            )

        handover = self._effective_handover(experiment_id, household.segment, channel, at)
        if handover is not None:
            _ack, promotion = handover
            return AccessOutcome(
                "handed_over",
                handover=Handover(
                    id="handover-" + hashlib.sha256(f"{experiment_id}|{request_key}".encode()).hexdigest()[:16],
                    household_id=household_id,
                    experiment_id=experiment_id,
                    request_key=request_key,
                    at=at,
                    channel=channel,
                    promotion_id=promotion.id,
                    served_by=f"promotion:{promotion.id}",
                    known_at=recorded_at,
                ),
                reason="该范围已由推广政策接管，访问不进入试验",
            )

        for other in self.assignments_by_household(household_id):
            if other.assignment.experiment_id == experiment_id:
                continue
            other_config = self.experiments().get(other.assignment.experiment_id)
            if other_config is None:
                continue
            if other_config.experiment.exclusive_scope == experiment.exclusive_scope:
                return AccessOutcome(
                    "refused",
                    reason=(
                        f"家庭已在互斥范围 {experiment.exclusive_scope!r} 的试验 "
                        f"{other.assignment.experiment_id} 中"
                    ),
                )

        for stop in self._active_stops(experiment_id, at):
            if _covered(parse_scope(stop.scope), household.segment, channel):
                return AccessOutcome("refused", reason=f"该范围已因风险停用（决定 {stop.id}），不再新增分配")

        if at < experiment.start_at:
            return AccessOutcome("refused", reason="试验尚未开始")
        if at >= experiment.end_at:
            return AccessOutcome("refused", reason="试验最长观察期已结束，不再新增分配")
        if at < household.eligible_from:
            return AccessOutcome("refused", reason="家庭尚未合资格")
        if not household.devices:
            return AccessOutcome("refused", reason="家庭没有可用设备")

        policy = self._active_policy(experiment_id, at)
        if policy is None:
            return AccessOutcome("refused", reason="没有在该时点生效的分流政策")
        arms = list(experiment.arms)
        if set(policy.weights) != set(arms):
            raise EngineError(409, "policy_broken", f"分流政策 {policy.policy_id} 与登记候选不一致")
        draw = _stable_uniform(experiment_id, household_id, request_key)
        cumulative = 0.0
        chosen = arms[-1]
        for arm in arms:
            cumulative += policy.weights[arm]
            if draw < cumulative:
                chosen = arm
                break
        assignment = Assignment(
            id="a-" + hashlib.sha256(f"{experiment_id}|{household_id}|{request_key}".encode()).hexdigest()[:16],
            household_id=household_id,
            experiment_id=experiment_id,
            opportunity_at=at,
            known_at=recorded_at,
            arm=chosen,
            probability=policy.weights[chosen],
            policy_id=policy.policy_id,
            request_key=request_key,
            device=device or household.devices[0],
        )
        return AccessOutcome("assigned", assignment=assignment, reason=f"按政策 {policy.policy_id} 入组")

    def access(self, body: dict) -> AccessOutcome:
        required = ["household_id", "experiment_id", "request_key", "at"]
        missing = [key for key in required if key not in body]
        if missing:
            raise EngineError(422, "missing_fields", f"缺少访问字段: {missing}")
        payload = {
            "household_id": str(body["household_id"]),
            "experiment_id": str(body["experiment_id"]),
            "request_key": str(body["request_key"]),
            "at": body["at"],
            "channel": body.get("channel"),
            "device": body.get("device"),
        }
        recorded_at = self._clock()
        # 先算结果再落事件：事件里的分配与当下返回完全一致，重放得到同样状态。
        outcome = self._resolve_access(payload, recorded_at=recorded_at)
        if outcome.status == "assigned":
            payload["enrollment_reason"] = outcome.reason
            payload["arm"] = outcome.assignment.arm
            payload["probability"] = outcome.assignment.probability
            payload["policy_id"] = outcome.assignment.policy_id
        elif outcome.status == "handed_over":
            payload["served_by"] = outcome.handover.served_by
            payload["promotion_id"] = outcome.handover.promotion_id
        # 访问记录同时保存当时判定结果，审计时不必依赖重放推断。
        payload["outcome_status"] = outcome.status
        payload["outcome_reason"] = outcome.reason
        self._apply_event(
            self.store.append("access_requested", payload, recorded_at=to_iso(recorded_at), actor=body.get("actor"))
        )
        return outcome

    # ------------------------------------------------------------------
    # 回执与答卷
    # ------------------------------------------------------------------
    def receipt(self, assignment_id: str, body: dict) -> dict:
        required = ["occurred_at", "known_at", "channel", "status"]
        missing = [key for key in required if key not in body]
        if missing:
            raise EngineError(422, "missing_fields", f"缺少回执字段: {missing}")
        assignments = self.assignments()
        if assignment_id not in assignments:
            raise EngineError(404, "unknown_assignment", f"未知分配 {assignment_id}")
        if body["status"] not in ("displayed", "unknown"):
            raise EngineError(422, "invalid_status", "回执状态只能是 displayed 或 unknown")
        occurred_at = parse_time(body["occurred_at"])
        known_at = parse_time(body["known_at"])
        trial = assignments[assignment_id]
        if occurred_at < trial.assignment.opportunity_at:
            raise EngineError(409, "receipt_before_opportunity", "回执不能早于机会形成时间")
        if known_at < occurred_at:
            raise EngineError(422, "known_before_occurred", "回执可知时间不能早于渠道动作")
        receipt_key = str(body.get("receipt_key") or "").strip()
        if receipt_key:
            existing = self._receipt_index.get((assignment_id, receipt_key))
            if existing is not None:
                return {"status": "duplicate_receipt", "receipt_id": existing.receipt_id}
        for existing in self.receipts():
            if existing.assignment_id != assignment_id:
                continue
            if (
                existing.occurred_at == occurred_at
                and existing.channel == body["channel"]
                and existing.status == body["status"]
            ):
                return {"status": "duplicate_receipt", "receipt_id": existing.receipt_id}
        receipt_id = "r-" + hashlib.sha256(
            f"{assignment_id}|{to_iso(occurred_at)}|{body['channel']}|{body['status']}|{receipt_key}".encode()
        ).hexdigest()[:16]
        payload = {
            "receipt_id": receipt_id,
            "assignment_id": assignment_id,
            "receipt_key": receipt_key,
            "occurred_at": to_iso(occurred_at),
            "known_at": to_iso(known_at),
            "channel": body["channel"],
            "status": body["status"],
        }
        self._apply_event(
            self.store.append("receipt_received", payload, recorded_at=to_iso(self._clock()), actor=body.get("actor"))
        )
        return {"status": "recorded", "receipt_id": receipt_id}

    def response(self, assignment_id: str, body: dict) -> dict:
        required = ["answered_at", "known_at", "correct", "false_certainty", "revision"]
        missing = [key for key in required if key not in body]
        if missing:
            raise EngineError(422, "missing_fields", f"缺少答卷字段: {missing}")
        assignments = self.assignments()
        if assignment_id not in assignments:
            raise EngineError(404, "unknown_assignment", f"未知分配 {assignment_id}")
        trial = assignments[assignment_id]
        answered_at = parse_time(body["answered_at"])
        known_at = parse_time(body["known_at"])
        if answered_at < trial.assignment.opportunity_at:
            raise EngineError(409, "answer_before_opportunity", "答卷不能早于机会形成时间")
        if known_at < answered_at:
            raise EngineError(422, "known_before_answered", "答卷可知时间不能早于作答时间")
        revision = float(body["revision"])
        existing = self._response_index.get((assignment_id, revision))
        if existing is not None:
            same = (
                existing.answered_at == answered_at
                and existing.known_at == known_at
                and existing.correct == bool(body["correct"])
                and existing.false_certainty == bool(body["false_certainty"])
            )
            if same:
                return {"status": "duplicate_response", "response_id": existing.response_id}
            raise EngineError(409, "revision_conflict", "同一修订序号内容不一致")
        response_id = "o-" + hashlib.sha256(f"{assignment_id}|{revision}".encode()).hexdigest()[:16]
        payload = {
            "response_id": response_id,
            "assignment_id": assignment_id,
            "answered_at": to_iso(answered_at),
            "known_at": to_iso(known_at),
            "correct": bool(body["correct"]),
            "false_certainty": bool(body["false_certainty"]),
            "revision": revision,
        }
        self._apply_event(
            self.store.append("response_received", payload, recorded_at=to_iso(self._clock()), actor=body.get("actor"))
        )
        return {"status": "recorded", "response_id": response_id}

    # ------------------------------------------------------------------
    # 决定
    # ------------------------------------------------------------------
    def _decision_id(self, kind: str, experiment_id: str, scope: str) -> str:
        base = f"{kind}|{experiment_id}|{scope}"
        candidate = "d-" + hashlib.sha256(base.encode()).hexdigest()[:16]
        suffix = 1
        existing = self.decisions()
        while candidate in existing:
            suffix += 1
            candidate = "d-" + hashlib.sha256(f"{base}|{suffix}".encode()).hexdigest()[:16]
        return candidate

    def _record_decision(self, payload: dict, *, actor: str | None) -> Decision:
        self._apply_event(
            self.store.append("decision_recorded", payload, recorded_at=to_iso(self._clock()), actor=actor)
        )
        return self._runtime_decisions[payload["decision_id"]]

    def risk_stop(self, experiment_id: str, body: dict) -> dict:
        self.config(experiment_id)
        scope = str(body.get("scope", "")).strip()
        if not scope:
            raise EngineError(422, "missing_scope", "风险停用必须给出适用范围")
        segment, channel = parse_scope(scope)
        if segment is None:
            raise EngineError(422, "scope_too_broad", "风险停用需要明确人群，不能用 all")
        at = parse_time(body["at"])
        actor = str(body.get("actor", "")).strip()
        if not actor:
            raise EngineError(422, "missing_actor", "风险停用必须记录操作人")
        analysis = self.analyse(experiment_id, as_of=at, segment=segment, channel=channel)
        risk_alerts = [pair.arm for pair in analysis.pairs if pair.status == "risk_alert"]
        risk_signals = [pair.arm for pair in analysis.pairs if pair.status == "risk_alert" or pair.risk_watch]
        if not risk_signals and not str(body.get("reason") or "").strip():
            raise EngineError(
                422,
                "reason_required",
                "当前没有错误确信越界或“尚不能排除风险”的信号，停用需要说明原因",
            )
        decision_id = self._decision_id("risk_stop", experiment_id, scope)
        payload = {
            "decision_id": decision_id,
            "experiment_id": experiment_id,
            "scope": scope,
            "kind": "risk_stop",
            "at": to_iso(at),
            "known_at": to_iso(self._clock()),
            "reference": "",
            "actor": actor,
            "reason": body.get("reason"),
            "evidence": {
                "at_stop": analysis.as_dict(),
                "risk_alerts": risk_alerts,
                "risk_signals": risk_signals,
                "undecidable": analysis.undecidable,
            },
        }
        decision = self._record_decision(payload, actor=actor)
        return {
            "decision_id": decision.id,
            "scope": scope,
            "noted_risk_alerts": risk_alerts,
            "noted_risk_signals": risk_signals,
            "evidence_conclusion": analysis.conclusion,
        }

    def promote_request(self, experiment_id: str, body: dict) -> dict:
        self.config(experiment_id)
        scope = str(body.get("scope", "")).strip()
        if not scope:
            raise EngineError(422, "missing_scope", "推广申请必须给出范围")
        at = parse_time(body["at"])
        segment, channel = parse_scope(scope)
        analysis = self.analyse(experiment_id, as_of=at, segment=segment, channel=channel)
        decision_id = self._decision_id("promote_request", experiment_id, scope)
        payload = {
            "decision_id": decision_id,
            "experiment_id": experiment_id,
            "scope": scope,
            "kind": "promote_request",
            "at": to_iso(at),
            "known_at": to_iso(self._clock()),
            "reference": "",
            "actor": body.get("actor"),
            "reason": body.get("reason"),
            "evidence": analysis.as_dict(),
        }
        decision = self._record_decision(payload, actor=body.get("actor"))
        return {
            "decision_id": decision.id,
            "scope": scope,
            "conclusion": analysis.conclusion,
            "promotable": analysis.conclusion == "promotable",
            "undecidable": analysis.undecidable,
            "comparisons": [pair.as_dict() for pair in analysis.pairs],
        }

    def promote_confirm(self, experiment_id: str, body: dict) -> dict:
        config = self.config(experiment_id)
        scope = str(body.get("scope", "")).strip()
        if not scope:
            raise EngineError(422, "missing_scope", "推广确认必须给出范围")
        actor = str(body.get("actor", "")).strip()
        if not actor:
            raise EngineError(422, "missing_actor", "推广确认必须记录负责人")
        if actor not in config.approved_leads:
            raise EngineError(403, "not_approved_lead", f"{actor} 不是该试验的获批负责人")
        at = parse_time(body["at"])
        segment, channel = parse_scope(scope)
        reference = str(body.get("reference") or "")
        if reference:
            parent = self.decisions().get(reference)
            if parent is None or parent.kind != "promote_request" or parent.experiment_id != experiment_id:
                raise EngineError(422, "invalid_reference", "推广确认必须引用本试验的推广申请")
            if not _scope_is_subset(scope, parent.scope):
                raise EngineError(422, "scope_mismatch", "确认范围不能超出推广申请范围")
        analysis = self.analyse(experiment_id, as_of=at, segment=segment, channel=channel)
        if analysis.undecidable:
            raise EngineError(409, "undecidable", f"当前范围结论不可判定：{analysis.undecidable_reasons}")
        promotable = [pair for pair in analysis.pairs if pair.status == "promotable"]
        if not promotable:
            raise EngineError(409, "not_promotable", "效果与风险条件未在同一有效证据范围同时成立")
        decision_id = self._decision_id("promote_confirm", experiment_id, scope)
        payload = {
            "decision_id": decision_id,
            "experiment_id": experiment_id,
            "scope": scope,
            "kind": "promote_confirm",
            "at": to_iso(at),
            "known_at": to_iso(self._clock()),
            "reference": reference,
            "actor": actor,
            "reason": body.get("reason"),
            "evidence": analysis.as_dict(),
            "promoted_arm": promotable[0].arm,
        }
        decision = self._record_decision(payload, actor=actor)
        return {
            "decision_id": decision.id,
            "scope": scope,
            "promoted_arm": promotable[0].arm,
            "evidence": analysis.as_dict(),
        }

    def takeover_ack(self, experiment_id: str, body: dict) -> dict:
        self.config(experiment_id)
        scope = str(body.get("scope", "")).strip()
        if not scope:
            raise EngineError(422, "missing_scope", "接管回执必须给出范围")
        at = parse_time(body["at"])
        reference = str(body.get("reference") or "")
        parent = self.decisions().get(reference)
        if parent is None:
            raise EngineError(422, "invalid_reference", "接管回执必须引用已确认的推广")
        if parent.kind != "promote_confirm" or parent.experiment_id != experiment_id:
            raise EngineError(
                422,
                "invalid_reference",
                "接管回执必须引用已确认的推广；未确认的推广申请不能声称渠道已接管",
            )
        if not _scope_is_subset(scope, parent.scope):
            raise EngineError(422, "scope_mismatch", "接管范围不能超出已确认的推广范围")
        if at < parent.at:
            raise EngineError(422, "takeover_before_promotion", "接管回执不能早于推广确认")
        decision_id = self._decision_id("takeover_ack", experiment_id, scope)
        payload = {
            "decision_id": decision_id,
            "experiment_id": experiment_id,
            "scope": scope,
            "kind": "takeover_ack",
            "at": to_iso(at),
            "known_at": to_iso(self._clock()),
            "reference": reference,
            "actor": body.get("actor"),
            "reason": body.get("reason"),
        }
        decision = self._record_decision(payload, actor=body.get("actor"))
        return {
            "decision_id": decision.id,
            "scope": scope,
            "promotion": reference,
            "partial": not _scope_is_subset(parent.scope, scope),
        }

    def permit_revoke_ack(self, experiment_id: str, body: dict) -> dict:
        self.config(experiment_id)
        scope = str(body.get("scope", "")).strip()
        if not scope:
            raise EngineError(422, "missing_scope", "许可撤销回执必须给出范围")
        at = parse_time(body["at"])
        reference = str(body.get("reference") or "")
        parent = self.decisions().get(reference)
        if (
            parent is None
            or parent.kind not in ("risk_stop", "promote_confirm")
            or parent.experiment_id != experiment_id
        ):
            raise EngineError(422, "invalid_reference", "许可撤销回执必须引用风险停用或推广确认")
        if not _scope_is_subset(scope, parent.scope):
            raise EngineError(422, "scope_mismatch", "撤销回执范围超出原决定")
        decision_id = self._decision_id("permit_revoke_ack", experiment_id, scope)
        payload = {
            "decision_id": decision_id,
            "experiment_id": experiment_id,
            "scope": scope,
            "kind": "permit_revoke_ack",
            "at": to_iso(at),
            "known_at": to_iso(self._clock()),
            "reference": reference,
            "actor": body.get("actor"),
            "reason": body.get("reason"),
        }
        decision = self._record_decision(payload, actor=body.get("actor"))
        return {"decision_id": decision.id, "scope": scope, "reference": reference}

    # ------------------------------------------------------------------
    # 分配视图
    # ------------------------------------------------------------------
    def _indexes(self) -> dict:
        """回执与答卷按分配的索引；只在新事件到达后重建。"""
        key = (len(self.base.displays), len(self.base.answers), len(self._runtime_receipts), len(self._runtime_responses))
        cached = self._index_cache.get("key")
        if cached == key:
            return self._index_cache["value"]
        receipts_by_assignment: dict[str, list[Receipt]] = {}
        for receipt in self.receipts():
            receipts_by_assignment.setdefault(receipt.assignment_id, []).append(receipt)
        responses_by_assignment: dict[str, list[Response]] = {}
        for response in self.responses():
            responses_by_assignment.setdefault(response.assignment_id, []).append(response)
        value = {
            "receipts_by_assignment": receipts_by_assignment,
            "responses_by_assignment": responses_by_assignment,
            "request_channels": self._request_channels(),
        }
        self._index_cache = {"key": key, "value": value}
        return value

    def _request_channels(self) -> dict[tuple[str, str], str]:
        rows: dict[tuple[str, str], str] = dict(self._base_request_channels)
        rows.update(self._runtime_request_channels)
        return {key: value for key, value in rows.items() if value}

    def _channel_for(
        self,
        assignment: Assignment,
        as_of: datetime,
        receipts_by_assignment: dict[str, list[Receipt]],
        request_channels: dict[tuple[str, str], str],
    ) -> tuple[str | None, str]:
        receipts = [r for r in receipts_by_assignment.get(assignment.id, []) if r.known_at <= as_of]
        displayed = [r for r in receipts if r.status == "displayed"]
        if displayed:
            first = min(displayed, key=lambda r: (r.occurred_at, r.known_at, r.receipt_id))
            return first.channel, "receipt"
        if receipts:
            first = min(receipts, key=lambda r: (r.occurred_at, r.known_at, r.receipt_id))
            return first.channel, "receipt_unknown"
        channel = request_channels.get((assignment.experiment_id, assignment.request_key))
        if channel:
            return channel, "request"
        return None, "unknown"

    def _views(
        self,
        experiment_id: str,
        as_of: datetime,
        *,
        segment: str | None = None,
        channel: str | None = None,
    ) -> list[AssignmentView]:
        config = self.config(experiment_id)
        experiment = config.experiment
        households = self.households()
        indexes = self._indexes()
        receipts_by_assignment = indexes["receipts_by_assignment"]
        responses_by_assignment = indexes["responses_by_assignment"]
        request_channels = indexes["request_channels"]
        stops = self._active_stops(experiment_id, as_of, known_by=as_of)

        views: list[AssignmentView] = []
        for trial in self.assignments().values():
            assignment = trial.assignment
            if assignment.experiment_id != experiment_id:
                continue
            household = households.get(assignment.household_id)
            if household is None:
                continue
            home_segment = household.segment
            assigned_channel, channel_source = self._channel_for(
                assignment, as_of, receipts_by_assignment, request_channels
            )
            if segment is not None and home_segment != segment:
                continue
            if channel is not None and assigned_channel != channel:
                continue
            close_at = assignment.opportunity_at + experiment.followup
            matured = trial.known_at <= as_of and close_at <= as_of

            receipts = [r for r in receipts_by_assignment.get(assignment.id, []) if r.known_at <= as_of]
            displayed = [r for r in receipts if r.status == "displayed"]
            revoked_at = None
            revoke_decision = None
            for stop in stops:
                if _covered(parse_scope(stop.scope), home_segment, assigned_channel):
                    revoked_at = stop.at
                    revoke_decision = stop.id
                    break
            if displayed:
                first = min(displayed, key=lambda r: (r.occurred_at, r.known_at, r.receipt_id))
                if revoked_at is not None and first.occurred_at >= revoked_at:
                    display_state = "post_stop_display"
                else:
                    display_state = "displayed"
            elif receipts:
                display_state = "unknown"
            elif revoked_at is not None:
                display_state = "revoked_before_show"
            else:
                display_state = "none"

            responses = [o for o in responses_by_assignment.get(assignment.id, []) if o.known_at <= as_of]
            in_window = [o for o in responses if assignment.opportunity_at <= o.answered_at < close_at]
            early = [o for o in responses if o.answered_at < assignment.opportunity_at]
            late = [o for o in responses if o.answered_at >= close_at]
            flags: list[str] = []
            if early:
                flags.append("early_answer")
            if late:
                flags.append("late_answer")
            if in_window:
                chosen = max(in_window, key=lambda o: (o.revision, o.known_at, o.response_id))
                answer_state = "answered"
                correct = chosen.correct
                false_certainty = chosen.false_certainty
                revision = chosen.revision
            elif matured:
                answer_state = "outside_window" if late else "missing"
                correct = False
                false_certainty = False
                revision = None
            else:
                answer_state = "waiting"
                correct = False
                false_certainty = False
                revision = None

            views.append(
                AssignmentView(
                    assignment=assignment,
                    segment=home_segment,
                    channel=assigned_channel,
                    channel_source=channel_source,
                    matured=matured,
                    window_closes_at=close_at,
                    display_state=display_state,
                    answer_state=answer_state,
                    answer_flags=flags,
                    correct=correct,
                    false_certainty=false_certainty,
                    revision=revision,
                    known_at=trial.known_at,
                    late_response=late[0] if late else None,
                )
            )
        return sorted(views, key=lambda v: (v.assignment.opportunity_at, v.assignment.id))

    def permits(self, experiment_id: str, as_of: datetime) -> list[PermitView]:
        config = self.config(experiment_id)
        households = self.households()
        indexes = self._indexes()
        receipts_by_assignment = indexes["receipts_by_assignment"]
        request_channels = indexes["request_channels"]
        stops = self._active_stops(experiment_id, as_of, known_by=as_of)
        revoke_acks = [
            d
            for d in self.decisions().values()
            if d.experiment_id == experiment_id
            and d.kind == "permit_revoke_ack"
            and d.known_at <= as_of
        ]
        rows: list[PermitView] = []
        for trial in self.assignments().values():
            assignment = trial.assignment
            if assignment.experiment_id != experiment_id or trial.known_at > as_of:
                continue
            household = households.get(assignment.household_id)
            if household is None:
                continue
            channel, _ = self._channel_for(assignment, as_of, receipts_by_assignment, request_channels)
            receipts = [r for r in receipts_by_assignment.get(assignment.id, []) if r.known_at <= as_of]
            displayed = [r for r in receipts if r.status == "displayed"]
            receipt_uncertain = bool(receipts) and not displayed
            redeemed_at = min((r.occurred_at for r in displayed), default=None)
            revoked_at = None
            revoke_decision = None
            scope_uncertain = False
            for stop in stops:
                stop_scope = parse_scope(stop.scope)
                if stop_scope[1] is not None and channel is None and stop_scope[0] in (None, household.segment):
                    # 渠道未知时不能断言该渠道被停用，也不能断言未停用。
                    scope_uncertain = True
                    continue
                if _covered(stop_scope, household.segment, channel):
                    revoked_at = stop.at
                    revoke_decision = stop.id
                    break
            post_stop_display = bool(
                revoked_at is not None and any(r.occurred_at >= revoked_at for r in displayed)
            )
            if redeemed_at is not None and (revoked_at is None or redeemed_at < revoked_at):
                state = "redeemed"
            elif revoked_at is not None and revoked_at <= as_of:
                acknowledged = any(
                    a.kind == "permit_revoke_ack"
                    and _scope_is_subset(
                        f"{household.segment}:{channel}" if channel else f"{household.segment}",
                        a.scope,
                    )
                    for a in revoke_acks
                )
                state = "revoked_acknowledged" if acknowledged else "revoked"
            elif as_of >= config.experiment.end_at:
                state = "expired"
            else:
                state = "outstanding"
            if post_stop_display:
                state = "post_stop_display"
            rows.append(
                PermitView(
                    permit_id="permit:" + assignment.id,
                    assignment_id=assignment.id,
                    household_id=assignment.household_id,
                    experiment_id=experiment_id,
                    segment=household.segment,
                    channel=channel,
                    issued_at=assignment.opportunity_at,
                    state=state,
                    receipt_uncertain=receipt_uncertain,
                    scope_uncertain=scope_uncertain,
                    redeemed_at=redeemed_at,
                    revoked_at=revoked_at,
                    revoke_decision=revoke_decision,
                    post_stop_display=post_stop_display,
                )
            )
        return sorted(rows, key=lambda p: (p.issued_at, p.assignment_id))

    # ------------------------------------------------------------------
    # 分析
    # ------------------------------------------------------------------
    def analyse(
        self,
        experiment_id: str,
        *,
        as_of: datetime | None = None,
        segment: str | None = None,
        channel: str | None = None,
    ) -> ScopeAnalysis:
        config = self.config(experiment_id)
        as_of = as_of or self._clock()
        if as_of.tzinfo is None:
            raise EngineError(422, "naive_time", "分析时点必须带时区")
        views = self._views(experiment_id, as_of, segment=segment, channel=channel)
        arms: dict[str, inference.ArmOutcome] = {
            arm: inference.ArmOutcome(arm=arm) for arm in config.experiment.arms
        }
        for view in views:
            outcome = arms.setdefault(view.assignment.arm, inference.ArmOutcome(arm=view.assignment.arm))
            if not view.matured:
                outcome.waiting += 1
                continue
            outcome.matured += 1
            if view.answer_state == "answered":
                outcome.answered += 1
                outcome.correct += int(view.correct)
                outcome.false_certainty += int(view.false_certainty)
            else:
                outcome.missing += 1
                if view.answer_state == "outside_window":
                    outcome.late_only += 1
            if "early_answer" in view.answer_flags:
                outcome.early_answer += 1
            if "late_answer" in view.answer_flags:
                outcome.late_answer += 1
            if view.display_state == "displayed":
                outcome.displayed += 1
                if view.answer_state == "answered":
                    outcome.displayed_answered += 1
                    outcome.displayed_correct += int(view.correct)
            elif view.display_state == "unknown":
                outcome.display_unknown += 1
            elif view.display_state == "none":
                outcome.no_receipt += 1
            else:
                outcome.display_deviation += 1

        reference = config.reference_arm or config.experiment.arms[0]
        undecidable_reasons: list[dict] = []
        undecidable = False

        assignments_in_scope = {view.assignment.id for view in views}
        household_ids = {view.assignment.household_id for view in views}
        for issue in issues_for(
            self.base.issues,
            experiment_id=experiment_id,
            assignment_ids=assignments_in_scope,
            household_ids=household_ids,
        ):
            if issue.code in STRUCTURAL_ISSUES:
                undecidable = True
                undecidable_reasons.append({"code": issue.code, "detail": issue.detail})

        counts_by_arm = {arm: sum(1 for v in views if v.assignment.arm == arm) for arm in arms}
        if counts_by_arm.get(reference, 0) == 0:
            undecidable = True
            undecidable_reasons.append(
                {"code": "no_common_support", "detail": f"参照说明 {reference} 在该范围没有分配"}
            )
        candidate_arms = [arm for arm in config.experiment.arms if arm != reference]
        if not any(counts_by_arm.get(arm, 0) > 0 for arm in candidate_arms):
            undecidable = True
            undecidable_reasons.append(
                {"code": "no_common_support", "detail": "该范围没有任何候选说明的分配"}
            )

        planned_scopes = len(config.planned_scopes) if config.planned_scopes else 1
        gate_alpha = inference.multiple_comparison_alpha(BASE_ALPHA, len(candidate_arms), planned_scopes)
        pairs = [
            inference.evaluate_pair(
                arms[arm],
                arms[reference],
                minimum_gain=config.experiment.minimum_gain,
                maximum_false_certainty=config.experiment.maximum_false_certainty,
                gate_alpha=gate_alpha,
                minimum_matured_per_arm=MIN_MATURED_PER_ARM,
            )
            for arm in candidate_arms
            if arm in arms
        ]
        conclusion = inference.scope_conclusion(pairs, undecidable=undecidable)
        notes: list[str] = []
        if channel is not None:
            known_in_scope = sum(1 for view in views if view.channel is not None)
            notes.append(
                "渠道分析只计入能归到该渠道的家庭；渠道未知的家庭不计入该渠道结论"
                f"（本范围可归属 {known_in_scope}/{len(views)}）"
            )
        if channel is None and any(view.channel is None for view in views):
            notes.append("部分家庭没有任何已知渠道，渠道层结论只能部分解释")
        if any(view.answer_state == "waiting" for view in views):
            notes.append("仍有家庭在答卷观察窗内，尚未成熟")
        if undecidable:
            notes.append("存在资料结构问题，结论只能降为不可判定；相关家庭未被删除")
        scope_label = f"{segment or 'all'}:{channel or 'all'}"
        matured_views = [view for view in views if view.matured]
        missing_views = [view for view in matured_views if view.answer_state != "answered"]
        displayed_answered = [
            view
            for view in matured_views
            if view.display_state == "displayed" and view.answer_state == "answered"
        ]
        display_deviation = {
            "matured": len(matured_views),
            "displayed": sum(1 for view in matured_views if view.display_state == "displayed"),
            "receipt_unknown": sum(1 for view in matured_views if view.display_state == "unknown"),
            "no_receipt": sum(1 for view in matured_views if view.display_state == "none"),
            "revoked_or_late_display": sum(
                1 for view in matured_views if view.display_state in ("revoked_before_show", "post_stop_display")
            ),
            "displayed_answered_correct_rate": (
                sum(view.correct for view in displayed_answered) / len(displayed_answered)
                if displayed_answered
                else None
            ),
            "primary_basis": "assignment_to_report",
            "note": "按原分配解释；实际显示偏离只作敏感性，不用于挑选有利家庭",
        }
        answer_missing = {
            "matured": len(matured_views),
            "answered_in_window": len(matured_views) - len(missing_views),
            "missing": len(missing_views),
            "late_only": sum(1 for view in matured_views if view.answer_state == "outside_window"),
            "waiting_not_matured": sum(1 for view in views if view.answer_state == "waiting"),
            "primary_basis": "all_matured_assignments",
            "note": "窗口内无答卷按未正确理解 / 未错误确信计入主要比较，不剔除家庭",
        }
        policy_versions = sorted({view.assignment.policy_id for view in views})
        return ScopeAnalysis(
            experiment_id=experiment_id,
            as_of=as_of,
            scope=scope_label,
            segment=segment,
            channel=channel,
            assignments=views,
            arms=arms,
            pairs=pairs,
            conclusion=conclusion,
            undecidable=undecidable,
            undecidable_reasons=undecidable_reasons,
            channel_attribution_incomplete=(
                channel is not None
                or any(view.channel is None for view in views)
            ),
            planned_scope_count=planned_scopes,
            gate_alpha=gate_alpha,
            notes=notes,
            display_deviation=display_deviation,
            answer_missing=answer_missing,
            policy_versions=policy_versions,
        )

    def _segments(self, experiment_id: str) -> list[str]:
        config = self.config(experiment_id)
        segments = {h.segment for h in self.households().values()}
        if config.target_segment:
            segments.add(config.target_segment)
        if config.planned_scopes:
            planned = {parse_scope(scope)[0] for scope in config.planned_scopes}
            segments |= {segment for segment in planned if segment}
        return sorted(segments)

    def analyse_all(self, experiment_id: str, *, as_of: datetime | None = None) -> dict:
        config = self.config(experiment_id)
        as_of = as_of or self._clock()
        scopes = [self.analyse(experiment_id, as_of=as_of).as_dict()]
        for seg in self._segments(experiment_id):
            scopes.append(self.analyse(experiment_id, as_of=as_of, segment=seg).as_dict())
        return {
            "experiment": config.as_dict(),
            "as_of": to_iso(as_of),
            "scopes": scopes,
        }

    def promotion_coverage(self, experiment_id: str, at: datetime) -> list[dict]:
        """已确认推广的实际生效范围：哪些人群×渠道已回执接管、哪些仍在试验。

        只依据 ``at`` 时点已知的推广确认与接管回执。接管范围来自渠道回执，而不是
        推广登记的意图；未回执的部分继续按原分配进入试验。
        """
        promotions = self._confirmed_promotions(experiment_id, at)
        acks = sorted(
            (
                d
                for d in self.decisions().values()
                if d.experiment_id == experiment_id
                and d.kind == "takeover_ack"
                and d.at <= at
                and d.known_at <= at
            ),
            key=lambda d: (d.at, d.known_at, d.id),
        )
        segments: set[str] = set()
        channels: set[str] = set()
        for household in self.households().values():
            segments.add(household.segment)
        for trial in self.assignments().values():
            if trial.assignment.experiment_id != experiment_id:
                continue
            household = self.households().get(trial.assignment.household_id)
            if household is not None:
                segments.add(household.segment)
            for receipt in self.receipts():
                if receipt.assignment_id == trial.assignment.id:
                    channels.add(receipt.channel)
        for ack in acks:
            _segment, channel = parse_scope(ack.scope)
            if channel is not None:
                channels.add(channel)
        rows: list[dict] = []
        for promotion in promotions:
            for segment in sorted(segments):
                for channel in sorted(channels):
                    promotion_scope = parse_scope(promotion.scope)
                    if promotion_scope[0] is not None and promotion_scope[0] != segment:
                        continue
                    if promotion_scope[1] is not None and promotion_scope[1] != channel:
                        continue
                    covering = [
                        ack
                        for ack in acks
                        if ack.reference == promotion.id
                        and _scope_is_subset(ack.scope, promotion.scope)
                        and _covered(parse_scope(ack.scope), segment, channel)
                    ]
                    rows.append(
                        {
                            "promotion_id": promotion.id,
                            "segment": segment,
                            "channel": channel,
                            "handover_state": "taken_over" if covering else "still_trial",
                            "takeover_ack": covering[-1].id if covering else None,
                        }
                    )
        return rows

    def snapshot(self, experiment_id: str, at: datetime) -> dict:
        config = self.config(experiment_id)
        overall = self.analyse(experiment_id, as_of=at)
        scopes = [self.analyse(experiment_id, as_of=at, segment=seg).as_dict() for seg in self._segments(experiment_id)]
        decisions_at = sorted(
            (d for d in self.decisions().values() if d.experiment_id == experiment_id and d.known_at <= at),
            key=lambda d: (d.at, d.known_at, d.id),
        )
        promotions = [d for d in decisions_at if d.kind == "promote_confirm"]
        stops = [d for d in decisions_at if d.kind == "risk_stop"]
        promotion_ids = {d.id for d in promotions}
        invalid_takeovers = [
            d for d in decisions_at if d.kind == "takeover_ack" and d.reference not in promotion_ids
        ]
        valid_takeovers = [
            d for d in decisions_at if d.kind == "takeover_ack" and d.reference in promotion_ids
        ]
        permits = self.permits(experiment_id, at)
        return {
            "experiment": config.as_dict(),
            "at": to_iso(at),
            "conclusion": overall.conclusion,
            "overall": overall.as_dict(),
            "scopes": scopes,
            "decisions": [
                {
                    "decision_id": d.id,
                    "kind": d.kind,
                    "scope": d.scope,
                    "at": to_iso(d.at),
                    "known_at": to_iso(d.known_at),
                    "reference": d.reference,
                }
                for d in decisions_at
            ],
            "effective_promotions": [d.id for d in promotions],
            "risk_stops": [d.id for d in stops],
            "invalid_takeovers": [
                {
                    "decision_id": d.id,
                    "scope": d.scope,
                    "reference": d.reference,
                    "reason": "引用的推广尚未确认，不能认为渠道已经接管",
                }
                for d in invalid_takeovers
            ],
            "valid_takeovers": [
                {"decision_id": d.id, "scope": d.scope, "reference": d.reference} for d in valid_takeovers
            ],
            "promotion_coverage": self.promotion_coverage(experiment_id, at),
            "permits": {
                "summary": {
                    state: sum(1 for p in permits if p.state == state)
                    for state in sorted({p.state for p in permits})
                },
                "outstanding": [p.as_dict() for p in permits if p.state == "outstanding"],
                "revoked": [p.as_dict() for p in permits if p.state.startswith("revoked")],
                "not_redeemed": [p.as_dict() for p in permits if p.state in ("outstanding", "revoked", "revoked_acknowledged")],
            },
        }

    def explain_change(self, experiment_id: str, scope: str, from_at: datetime, to_at: datetime) -> dict:
        segment, channel = parse_scope(scope)
        before = self.analyse(experiment_id, as_of=from_at, segment=segment, channel=channel)
        after = self.analyse(experiment_id, as_of=to_at, segment=segment, channel=channel)
        before_by_id = {view.assignment.id: view for view in before.assignments}
        newly_matured = [
            view.assignment.id
            for view in after.assignments
            if view.matured and (view.assignment.id not in before_by_id or not before_by_id[view.assignment.id].matured)
        ]
        newly_answered = [
            view.assignment.id
            for view in after.assignments
            if view.answer_state == "answered"
            and (
                view.assignment.id not in before_by_id
                or before_by_id[view.assignment.id].answer_state != "answered"
            )
        ]
        new_decisions = [
            {
                "decision_id": d.id,
                "kind": d.kind,
                "scope": d.scope,
                "at": to_iso(d.at),
                "known_at": to_iso(d.known_at),
            }
            for d in sorted(self.decisions().values(), key=lambda d: (d.at, d.known_at, d.id))
            if d.experiment_id == experiment_id and from_at < d.known_at <= to_at
        ]
        return {
            "experiment_id": experiment_id,
            "scope": scope,
            "from": to_iso(from_at),
            "to": to_iso(to_at),
            "before": {
                "conclusion": before.conclusion,
                "undecidable": before.undecidable,
                "reasons": before.undecidable_reasons,
                "comparisons": [pair.as_dict() for pair in before.pairs],
            },
            "after": {
                "conclusion": after.conclusion,
                "undecidable": after.undecidable,
                "reasons": after.undecidable_reasons,
                "comparisons": [pair.as_dict() for pair in after.pairs],
            },
            "newly_matured_assignments": newly_matured,
            "newly_answered_assignments": newly_answered,
            "new_decisions": new_decisions,
        }

    # ------------------------------------------------------------------
    # 审计与重置
    # ------------------------------------------------------------------
    def access_log(self, experiment_id: str | None = None) -> list[dict]:
        """访问与判定结果，含重复请求、设备切换、拒绝、接管；不重新解释历史。"""
        rows = []
        for event in self.store.iter_events():
            if event["type"] not in ("access_requested", "access_refused"):
                continue
            payload = event["payload"]
            if experiment_id and payload.get("experiment_id") != experiment_id:
                continue
            rows.append(
                {
                    "seq": event["seq"],
                    "recorded_at": event["recorded_at"],
                    "type": event["type"],
                    "household_id": payload.get("household_id"),
                    "experiment_id": payload.get("experiment_id"),
                    "request_key": payload.get("request_key"),
                    "at": payload.get("at"),
                    "channel": payload.get("channel"),
                    "device": payload.get("device"),
                    "outcome_status": payload.get("outcome_status") or payload.get("status"),
                    "outcome_reason": payload.get("outcome_reason") or payload.get("reason"),
                }
            )
        return rows

    def audit(self, experiment_id: str) -> dict:
        config = self.config(experiment_id)
        events = list(self.store.iter_events())
        return {
            "experiment_id": experiment_id,
            "config": config.as_dict(),
            "event_count": len(events),
            "policies": [p.as_dict() for p in sorted(self.policies().values(), key=lambda p: (p.effective_at, p.policy_id))],
            "decisions": [
                {
                    "decision_id": d.id,
                    "kind": d.kind,
                    "scope": d.scope,
                    "at": to_iso(d.at),
                    "known_at": to_iso(d.known_at),
                    "reference": d.reference,
                }
                for d in sorted(
                    (d for d in self.decisions().values() if d.experiment_id == experiment_id),
                    key=lambda d: (d.at, d.known_at, d.id),
                )
            ],
            "refused_access": list(self._refused_access),
            "integrity_issues": [
                {
                    "code": issue.code,
                    "detail": issue.detail,
                    "assignment_id": issue.assignment_id,
                    "household_id": issue.household_id,
                    "experiment_id": issue.experiment_id,
                }
                for issue in self.base.issues
            ],
        }

    def reset(self) -> None:
        path = self.store.path
        if path.exists():
            path.unlink()
        self.__init__(self.root, state_dir=self.state_dir, replay=True)
