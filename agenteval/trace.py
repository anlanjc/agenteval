"""Trace/Span 事件模型 + Tracer 上下文管理器 + SQLite 持久化。

设计要点：
- run（一次评测/一次对话）→ span 树（root span = run 本身）→ event 流（span 内离散事件）。
- 全部 dataclass，纯标准库；SQLite 三张表：runs / spans / events。
- token 估算用启发式（ASCII 约 4 字符/token，CJK 约 1 字/token），只用于观测不承诺精确。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator


# ---------------------------------------------------------------- token 估算

_CJK_RANGES = (
    (0x4E00, 0x9FFF),    # CJK 统一表意文字
    (0x3400, 0x4DBF),    # 扩展 A
    (0x3000, 0x303F),    # CJK 标点
    (0xFF00, 0xFFEF),    # 全角字符
)


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：CJK 字符近似 1 字/token，其余按 4 字符/token。

    只做量级估计（观测用），不针对具体 tokenizer。
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _CJK_RANGES):
            cjk += 1
    other = len(text) - cjk
    return cjk + (other + 3) // 4


# ---------------------------------------------------------------- 数据模型


@dataclass
class Span:
    """一个有起止时间的执行单元，可嵌套成树。"""

    span_id: str
    name: str
    parent_span_id: str | None = None
    started_at: float = 0.0  # time.time()
    ended_at: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok | error
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        if self.ended_at and self.ended_at >= self.started_at:
            return (self.ended_at - self.started_at) * 1000.0
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "name": self.name,
            "parent_span_id": self.parent_span_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": round(self.duration_ms, 3),
            "attributes": self.attributes,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class Event:
    """span 内的离散事件（工具调用、决策点、告警……）。"""

    event_id: str
    run_id: str
    span_id: str | None
    name: str
    ts: float
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "span_id": self.span_id,
            "name": self.name,
            "ts": self.ts,
            "attributes": self.attributes,
        }


@dataclass
class TraceRun:
    """一次被观测的执行（评测 run 或单次对话）。"""

    run_id: str
    name: str = ""
    created_at: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    spans: list[Span] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    status: str = "running"  # running | ok | error

    # ---- 统计 -------------------------------------------------------------
    @property
    def root(self) -> Span | None:
        for s in self.spans:
            if s.parent_span_id is None:
                return s
        return None

    @property
    def duration_ms(self) -> float:
        root = self.root
        return root.duration_ms if root else 0.0

    @property
    def total_tokens(self) -> int:
        return sum(int(s.attributes.get("tokens", 0)) for s in self.spans)

    def span_tree(self) -> dict[str, Any] | None:
        """把扁平 span 列表装回树（用于 API/前端渲染）。"""
        nodes = {s.span_id: {**s.to_dict(), "children": []} for s in self.spans}
        root_node = None
        for s in self.spans:
            node = nodes[s.span_id]
            if s.parent_span_id and s.parent_span_id in nodes:
                nodes[s.parent_span_id]["children"].append(node)
            elif s.parent_span_id is None:
                root_node = node
        # 按 started_at 排序保持时间序
        def _sort(n: dict[str, Any]) -> None:
            n["children"].sort(key=lambda c: c["started_at"])
            for c in n["children"]:
                _sort(c)

        if root_node:
            _sort(root_node)
        return root_node

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "created_at": self.created_at,
            "attributes": self.attributes,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 3),
            "total_tokens": self.total_tokens,
            "spans": [s.to_dict() for s in self.spans],
            "events": [e.to_dict() for e in self.events],
        }


class Tracer:
    """Tracer 上下文管理器：with tracer.span("name") 嵌套记录。

    用法：
        tracer = Tracer("eval-run")
        with tracer.span("case", {"case_id": "c1"}) as root:
            with tracer.span("target.call") as s:
                s.event("llm.request", {"model": "mock"})
        tracer.finish(status="ok")
    """

    def __init__(self, name: str = "", attributes: dict[str, Any] | None = None):
        self.run = TraceRun(
            run_id=uuid.uuid4().hex[:16],
            name=name,
            created_at=time.time(),
            attributes=dict(attributes or {}),
        )
        self._stack: list[Span] = []

    @property
    def run_id(self) -> str:
        return self.run.run_id

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
        parent = self._stack[-1] if self._stack else None
        sp = Span(
            span_id=uuid.uuid4().hex[:16],
            name=name,
            parent_span_id=parent.span_id if parent else None,
            started_at=time.time(),
            attributes=dict(attributes or {}),
        )
        self.run.spans.append(sp)
        self._stack.append(sp)
        try:
            yield sp
        except Exception as exc:  # 记录异常后继续上抛
            sp.status = "error"
            sp.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            sp.ended_at = time.time()
            self._stack.pop()

    def event(self, name: str, attributes: dict[str, Any] | None = None) -> Event:
        """在当前最内层 span 上记录事件（无 span 则挂 run 级）。"""
        span = self._stack[-1] if self._stack else None
        ev = Event(
            event_id=uuid.uuid4().hex[:16],
            run_id=self.run.run_id,
            span_id=span.span_id if span else None,
            name=name,
            ts=time.time(),
            attributes=dict(attributes or {}),
        )
        self.run.events.append(ev)
        return ev

    def set_attr(self, key: str, value: Any) -> None:
        """给最内层 span（或 run 根）补属性。"""
        target = self._stack[-1] if self._stack else self.run.root
        if target is not None:
            target.attributes[key] = value

    def finish(self, status: str = "ok") -> TraceRun:
        if self.run.status == "running":
            self.run.status = status
        # root span 未闭合则补齐
        root = self.run.root
        if root and root.ended_at == 0.0:
            root.ended_at = time.time()
        return self.run


# ---------------------------------------------------------------- SQLite 持久化

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    name        TEXT,
    created_at  REAL,
    status      TEXT,
    attributes  TEXT
);
CREATE TABLE IF NOT EXISTS spans (
    span_id         TEXT PRIMARY KEY,
    run_id          TEXT,
    parent_span_id  TEXT,
    name            TEXT,
    started_at      REAL,
    ended_at        REAL,
    duration_ms     REAL,
    attributes      TEXT,
    status          TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_run ON spans(run_id);
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    run_id      TEXT,
    span_id     TEXT,
    name        TEXT,
    ts          REAL,
    attributes  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
"""


def _row_to_run(row: sqlite3.Row) -> TraceRun:
    return TraceRun(
        run_id=row["run_id"],
        name=row["name"] or "",
        created_at=row["created_at"] or 0.0,
        status=row["status"] or "ok",
        attributes=json.loads(row["attributes"] or "{}"),
    )


def _row_to_span(row: sqlite3.Row) -> Span:
    return Span(
        span_id=row["span_id"],
        name=row["name"] or "",
        parent_span_id=row["parent_span_id"],
        started_at=row["started_at"] or 0.0,
        ended_at=row["ended_at"] or 0.0,
        attributes=json.loads(row["attributes"] or "{}"),
        status=row["status"] or "ok",
        error=row["error"],
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        event_id=row["event_id"],
        run_id=row["run_id"],
        span_id=row["span_id"],
        name=row["name"] or "",
        ts=row["ts"] or 0.0,
        attributes=json.loads(row["attributes"] or "{}"),
    )


class TraceStore:
    """SQLite 持久化与查询。

    AGT-7 修复（评审 2.0）：docstring 曾声称「每次操作独立连接」，实现实为共享单连接
    且跨线程即抛 sqlite3.ProgrammingError（CanaryMonitor 内嵌生产进程即触发）。
    现如实声明并落实：共享连接 + threading.Lock 串行化全部读写。
    """

    def __init__(self, path: str | Any):
        # path 可以是 ":memory:" 或文件路径
        self.path = str(path)
        import threading

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 写入 --------------------------------------------------------------
    def save_run(self, run: TraceRun) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id,name,created_at,status,attributes) VALUES(?,?,?,?,?)",
                (run.run_id, run.name, run.created_at, run.status,
                 json.dumps(run.attributes, ensure_ascii=False)),
            )
            for sp in run.spans:
                self._conn.execute(
                    "INSERT OR REPLACE INTO spans(span_id,run_id,parent_span_id,name,started_at,"
                    "ended_at,duration_ms,attributes,status,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sp.span_id, run.run_id, sp.parent_span_id, sp.name, sp.started_at,
                     sp.ended_at, sp.duration_ms,
                     json.dumps(sp.attributes, ensure_ascii=False), sp.status, sp.error),
                )
            for ev in run.events:
                self._conn.execute(
                    "INSERT OR REPLACE INTO events(event_id,run_id,span_id,name,ts,attributes) "
                    "VALUES(?,?,?,?,?,?)",
                    (ev.event_id, ev.run_id, ev.span_id, ev.name, ev.ts,
                     json.dumps(ev.attributes, ensure_ascii=False)),
                )
            self._conn.commit()

    # ---- 查询 --------------------------------------------------------------
    def list_runs(self) -> list[TraceRun]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
            return [_row_to_run(r) for r in rows]

    def load_run(self, run_id: str) -> TraceRun | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return None
            run = _row_to_run(row)
            spans = self._conn.execute(
                "SELECT * FROM spans WHERE run_id=? ORDER BY started_at", (run_id,)
            ).fetchall()
            run.spans = [_row_to_span(r) for r in spans]
            events = self._conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY ts", (run_id,)
            ).fetchall()
            run.events = [_row_to_event(r) for r in events]
            return run

    def load_spans(self, run_id: str) -> list[Span]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM spans WHERE run_id=? ORDER BY started_at", (run_id,)
            ).fetchall()
            return [_row_to_span(r) for r in rows]

    def load_events(self, run_id: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY ts", (run_id,)
            ).fetchall()
            return [_row_to_event(r) for r in rows]
