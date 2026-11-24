"""命令行入口：``python3 -m trial_platform``。

子命令：

- ``serve``    启动本地 FastAPI 服务
- ``simulate`` 运行可重复随机合成校验
- ``summary``  打印资料表的当前分析摘要
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trial_platform", description="持续营养说明试验平台")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动本地服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    sim = sub.add_parser("simulate", help="运行随机合成校验")
    sim.add_argument("--trials", type=int, default=60)
    sim.add_argument("--json-out", default=None)

    summary = sub.add_parser("summary", help="打印当前分析摘要")
    summary.add_argument("--as-of", default=None)
    summary.add_argument("--state-dir", default=None)

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("trial_platform.service:app", host=args.host, port=args.port, reload=False)
        return 0
    if args.command == "simulate":
        from .simulation import run_suite, end_to_end_demo

        payload = run_suite(trials=args.trials)
        payload["end_to_end"] = end_to_end_demo()
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json_out:
            from pathlib import Path

            Path(args.json_out).write_text(text, encoding="utf-8")
        print(text)
        return 0
    if args.command == "summary":
        from datetime import datetime

        from .engine import Engine
        from .model import parse_time

        engine = Engine(state_dir=args.state_dir) if args.state_dir else Engine()
        as_of = parse_time(args.as_of) if args.as_of else None
        payload = {}
        for experiment_id in engine.experiments():
            analysis = engine.analyse_all(experiment_id, as_of=as_of)
            for scope in analysis["scopes"]:
                scope.pop("assignments", None)
            payload[experiment_id] = analysis
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
