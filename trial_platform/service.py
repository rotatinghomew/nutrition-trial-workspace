"""FastAPI 本地服务：让工作人员通过 HTTP 操作同一个试验。

服务只做参数校验和调用引擎；所有业务判定在 ``trial_platform.engine``。原始资料
只读，运行时状态写 ``local-state``（可用 ``TRIAL_STATE_DIR`` 覆盖）。

多个工作人员可能同时访问：服务进程内只保留一个引擎实例，并用进程级锁串行化
读写，避免两个并发请求为同一请求键各自产生分配，也避免读到半更新状态。
"""
from __future__ import annotations

import os
from datetime import datetime
from threading import RLock

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import Engine, EngineError, now_utc, parse_time, to_iso

_CACHE: dict[tuple[str | None, str | None], Engine] = {}
_CACHE_LOCK = RLock()


def _engine() -> Engine:
    root = os.environ.get("TRIAL_ROOT")
    state_dir = os.environ.get("TRIAL_STATE_DIR")
    key = (root, state_dir)
    with _CACHE_LOCK:
        engine = _CACHE.get(key)
        if engine is None:
            engine = Engine(root=root, state_dir=state_dir)
            _CACHE[key] = engine
        return engine


class ExperimentBody(BaseModel):
    experiment_id: str
    exclusive_scope: str
    start_at: str
    end_at: str
    followup_hours: float
    minimum_gain: float
    maximum_false_certainty: float
    look_policy: str
    arms: list[str]
    reference_arm: str | None = None
    target_segment: str | None = None
    approved_leads: list[str] = Field(default_factory=list)
    look_dates: list[str] = Field(default_factory=list)
    planned_scopes: list[str] = Field(default_factory=list)
    registered_by: str | None = None


class HouseholdBody(BaseModel):
    household_id: str
    segment: str
    region: str
    devices: list[str]
    eligible_from: str
    actor: str | None = None


class PolicyBody(BaseModel):
    policy_id: str
    weights: dict[str, float]
    effective_at: str
    created_by: str | None = None
    note: str | None = None


class AccessBody(BaseModel):
    household_id: str
    experiment_id: str
    request_key: str
    at: str
    channel: str
    device: str | None = None
    actor: str | None = None


class ReceiptBody(BaseModel):
    occurred_at: str
    known_at: str
    channel: str
    status: str
    receipt_key: str | None = None
    actor: str | None = None


class ResponseBody(BaseModel):
    answered_at: str
    known_at: str
    correct: bool
    false_certainty: bool
    revision: float = 1
    actor: str | None = None


class DecisionBody(BaseModel):
    scope: str
    at: str
    actor: str | None = None
    reference: str | None = None
    reason: str | None = None


class ConfirmBody(DecisionBody):
    actor: str


class ConfigBody(BaseModel):
    reference_arm: str | None = None
    target_segment: str | None = None
    approved_leads: list[str] | None = None
    look_dates: list[str] | None = None
    planned_scopes: list[str] | None = None
    actor: str | None = None


def _time(value: str | None) -> datetime:
    return parse_time(value) if value else now_utc()


def create_app() -> FastAPI:
    app = FastAPI(
        title="持续营养说明试验平台",
        description="登记、分配、理解问答、持续分析与投放接管。资料为合成记录。",
    )

    @app.exception_handler(EngineError)
    async def _engine_error(_request, exc: EngineError):
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code, "detail": exc.detail},
        )

    def with_engine(fn, *args, **kwargs):
        engine = _engine()
        with engine._lock:
            return fn(engine, *args, **kwargs)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "now": to_iso(now_utc())}

    @app.get("/experiments")
    def list_experiments() -> dict:
        return with_engine(lambda e: {"experiments": [c.as_dict() for c in e.experiments().values()]})

    @app.post("/experiments", status_code=201)
    def register_experiment(body: ExperimentBody) -> dict:
        return with_engine(Engine.register_experiment, body.model_dump())

    @app.patch("/experiments/{experiment_id}")
    def configure_experiment(experiment_id: str, body: ConfigBody) -> dict:
        return with_engine(Engine.configure_experiment, experiment_id, body.model_dump(exclude_none=True))

    @app.post("/experiments/{experiment_id}/policies", status_code=201)
    def register_policy(experiment_id: str, body: PolicyBody) -> dict:
        return with_engine(Engine.register_policy, experiment_id, body.model_dump())

    @app.get("/experiments/{experiment_id}/policies")
    def list_policies(experiment_id: str) -> dict:
        def run(engine: Engine) -> dict:
            engine.config(experiment_id)
            return {
                "policies": [
                    p.as_dict()
                    for p in sorted(engine.policies().values(), key=lambda p: (p.effective_at, p.policy_id))
                    if p.experiment_id == experiment_id
                ]
            }

        return with_engine(run)

    @app.post("/households", status_code=201)
    def register_household(body: HouseholdBody) -> dict:
        return with_engine(Engine.register_household, body.model_dump())

    @app.post("/access")
    def access(body: AccessBody) -> dict:
        return with_engine(lambda engine: engine.access(body.model_dump()).as_dict())

    @app.get("/experiments/{experiment_id}/access-log")
    def access_log(experiment_id: str) -> dict:
        return with_engine(lambda engine: {"requests": engine.access_log(experiment_id)})

    @app.post("/assignments/{assignment_id}/receipts", status_code=201)
    def receipt(assignment_id: str, body: ReceiptBody) -> dict:
        return with_engine(Engine.receipt, assignment_id, body.model_dump())

    @app.post("/assignments/{assignment_id}/responses", status_code=201)
    def response(assignment_id: str, body: ResponseBody) -> dict:
        return with_engine(Engine.response, assignment_id, body.model_dump())

    @app.get("/experiments/{experiment_id}/analysis")
    def analysis(
        experiment_id: str,
        as_of: str | None = Query(default=None),
        segment: str | None = Query(default=None),
        channel: str | None = Query(default=None),
        include_assignments: bool = Query(default=False),
    ) -> dict:
        def run(engine: Engine) -> dict:
            result = engine.analyse(
                experiment_id, as_of=_time(as_of), segment=segment, channel=channel
            )
            payload = result.as_dict()
            if not include_assignments:
                payload.pop("assignments", None)
            return payload

        return with_engine(run)

    @app.get("/experiments/{experiment_id}/scopes")
    def scopes(experiment_id: str, as_of: str | None = Query(default=None)) -> dict:
        def run(engine: Engine) -> dict:
            payload = engine.analyse_all(experiment_id, as_of=_time(as_of))
            for scope in payload["scopes"]:
                scope.pop("assignments", None)
            return payload

        return with_engine(run)

    @app.get("/experiments/{experiment_id}/snapshot")
    def snapshot(experiment_id: str, at: str = Query(...)) -> dict:
        def run(engine: Engine) -> dict:
            payload = engine.snapshot(experiment_id, parse_time(at))
            payload["overall"].pop("assignments", None)
            for scope in payload["scopes"]:
                scope.pop("assignments", None)
            return payload

        return with_engine(run)

    @app.get("/experiments/{experiment_id}/permits")
    def permits(experiment_id: str, as_of: str | None = Query(default=None)) -> dict:
        def run(engine: Engine) -> dict:
            rows = engine.permits(experiment_id, _time(as_of))
            return {
                "permits": [row.as_dict() for row in rows],
                "summary": {
                    state: sum(1 for row in rows if row.state == state)
                    for state in sorted({row.state for row in rows})
                },
            }

        return with_engine(run)

    @app.get("/experiments/{experiment_id}/explain-change")
    def explain_change(
        experiment_id: str,
        scope: str = Query(default="all:all"),
        from_at: str = Query(...),
        to_at: str = Query(...),
    ) -> dict:
        return with_engine(
            Engine.explain_change, experiment_id, scope, parse_time(from_at), parse_time(to_at)
        )

    @app.get("/experiments/{experiment_id}/audit")
    def audit(experiment_id: str) -> dict:
        return with_engine(Engine.audit, experiment_id)

    @app.get("/experiments/{experiment_id}/promotion-coverage")
    def promotion_coverage(experiment_id: str, at: str = Query(...)) -> dict:
        return with_engine(
            lambda engine: {
                "coverage": engine.promotion_coverage(experiment_id, parse_time(at))
            }
        )

    @app.get("/experiments/{experiment_id}/handovers")
    def handovers(experiment_id: str) -> dict:
        def run(engine: Engine) -> dict:
            return {
                "handovers": [
                    {
                        "id": h.id,
                        "household_id": h.household_id,
                        "request_key": h.request_key,
                        "at": to_iso(h.at),
                        "channel": h.channel,
                        "promotion_id": h.promotion_id,
                        "served_by": h.served_by,
                    }
                    for h in engine.handovers()
                    if h.experiment_id == experiment_id
                ]
            }

        return with_engine(run)

    @app.post("/experiments/{experiment_id}/risk-stops", status_code=201)
    def risk_stop(experiment_id: str, body: DecisionBody) -> dict:
        return with_engine(Engine.risk_stop, experiment_id, body.model_dump())

    @app.post("/experiments/{experiment_id}/promotion-requests", status_code=201)
    def promotion_request(experiment_id: str, body: DecisionBody) -> dict:
        return with_engine(Engine.promote_request, experiment_id, body.model_dump())

    @app.post("/experiments/{experiment_id}/promotion-confirmations", status_code=201)
    def promotion_confirm(experiment_id: str, body: ConfirmBody) -> dict:
        return with_engine(Engine.promote_confirm, experiment_id, body.model_dump())

    @app.post("/experiments/{experiment_id}/takeover-acks", status_code=201)
    def takeover_ack(experiment_id: str, body: DecisionBody) -> dict:
        return with_engine(Engine.takeover_ack, experiment_id, body.model_dump())

    @app.post("/experiments/{experiment_id}/permit-revoke-acks", status_code=201)
    def permit_revoke_ack(experiment_id: str, body: DecisionBody) -> dict:
        return with_engine(Engine.permit_revoke_ack, experiment_id, body.model_dump())

    @app.post("/admin/reset")
    def reset() -> dict:
        def run(engine: Engine) -> dict:
            engine.reset()
            return {"status": "reset"}

        return with_engine(run)

    return app


app = create_app()
