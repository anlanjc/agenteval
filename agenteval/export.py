"""Trace 生态导出：把落库的 run 转成 Langfuse 兼容的事件 JSONL（ADR 下一版清单的落地）。

Langfuse ingest 格式（简化版）：每行一个 JSON 对象，type=trace-create（根）或
span-create（含 parentSpanID/startTime/endTime/level/metadata）——字段命名对齐
Langfuse 的 ingestion API 约定，可直接被其 /api/public/ingestion 消费（或转 OTel）。

用途：自研 trace 的可观测性不锁死——生产上接 Langfuse 只需这一层导出，评测的
trace 语义（run→case→target.call→judge.score）在两家体系里同样可读。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .trace import TraceRun, TraceStore


def _iso(ts: float) -> str:
    """unix 秒 → ISO8601（Langfuse 要求 UTC 带时区）。"""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _level(status: str, error: str | None) -> str:
    return "ERROR" if (status == "error" or error) else "DEFAULT"


def trace_to_langfuse(run: TraceRun) -> list[dict[str, Any]]:
    """TraceRun → Langfuse ingestion 事件列表。"""
    events: list[dict[str, Any]] = [{
        "type": "trace-create",
        "id": run.run_id,
        "name": run.name,
        "timestamp": _iso(run.created_at),
        "metadata": {**run.attributes, "status": run.status},
    }]
    for span in run.spans:
        events.append({
            "type": "span-create",
            "id": span.span_id,
            "traceId": run.run_id,
            "parentSpanId": span.parent_span_id,
            "name": span.name,
            "startTime": _iso(span.started_at),
            "endTime": _iso(span.ended_at),
            "level": _level(span.status, span.error),
            "metadata": {**span.attributes,
                         **({"error": span.error} if span.error else {})},
        })
    for ev in run.events:
        events.append({
            "type": "span-create",   # 事件降级为瞬时 span（Langfuse 无独立 observation 事件的简化映射）
            "id": ev.event_id,
            "traceId": run.run_id,
            "spanId": ev.span_id,
            "name": f"event.{ev.name}",
            "startTime": _iso(ev.ts),
            "endTime": _iso(ev.ts),
            "level": "DEFAULT",
            "metadata": dict(ev.attributes or {}),
        })
    return events


def export_run(store: TraceStore, run_id: str, path: str | Path) -> dict[str, int]:
    """从 SQLite 取 run 并导出 Langfuse JSONL。返回计数。"""
    run = store.load_run(run_id)
    if run is None:
        raise KeyError(f"run 不存在：{run_id}")
    events = trace_to_langfuse(run)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
    return {"events": len(events), "spans": len(run.spans), "trace_events": len(run.events)}


def export_all(store: TraceStore, path: str | Path) -> dict[str, int]:
    """导出库里全部 run（追加式 JSONL，带 trace-create 分隔）。"""
    total = {"events": 0, "runs": 0}
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for run in store.list_runs():
            for ev in trace_to_langfuse(run):
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
            total["events"] += 1 + len(run.spans) + len(run.events)
            total["runs"] += 1
    return total
