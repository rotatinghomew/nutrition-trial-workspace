"""只追加的本地事件流。

原始资料表不被改写。运行时产生的登记、分流政策、访问、回执、答卷和决定都写入
``local-state/events.jsonl``；引擎启动时先读资料，再按事件顺序重放。事件带自增序号
和记录时间，便于审计与复现。删除本地状态目录即可回到资料原貌。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any, Iterator


class EventStore:
    def __init__(self, path: str | Path, *, fsync: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._fsync = fsync
        self._seq = 0
        if self.path.exists():
            for event in self.iter_events():
                self._seq = max(self._seq, int(event.get("seq", 0)))

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def append(self, event_type: str, payload: dict[str, Any], *, recorded_at: str, actor: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "type": event_type,
                "recorded_at": recorded_at,
                "actor": actor,
                "payload": payload,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
            return event


def default_state_dir(root: str | Path) -> Path:
    override = os.environ.get("TRIAL_STATE_DIR")
    if override:
        return Path(override)
    return Path(root) / "local-state"
