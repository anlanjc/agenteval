"""生态导出测试：Langfuse JSONL 格式、父子链接、落盘往返。"""
import json

import pytest

from agenteval.export import export_all, export_run, trace_to_langfuse
from agenteval.trace import Event, Span, TraceRun, TraceStore


def make_run() -> TraceRun:
    run = TraceRun(run_id="r1", name="eval", created_at=1000.0,
                   attributes={"dataset": "demo.jsonl", "target": "mock"})
    root = Span(span_id="s-root", name="root", started_at=1000.0, ended_at=1010.0,
                attributes={"tokens": 42})
    child = Span(span_id="s-case", name="case.q1", parent_span_id="s-root",
                 started_at=1001.0, ended_at=1005.0)
    ev = type("E", (), {"event_id": "e1", "run_id": "r1", "span_id": "s-case",
                        "name": "judge", "ts": 1004.0,
                        "attributes": {"overall": 4}})()
    run.spans = [root, child]
    run.events = [ev]
    return run


def test_trace_to_langfuse_structure():
    events = trace_to_langfuse(make_run())
    assert events[0]["type"] == "trace-create" and events[0]["id"] == "r1"
    spans = [e for e in events if e["type"] == "span-create"]
    assert len(spans) == 3                     # 2 span + 1 事件降级
    assert spans[0]["traceId"] == "r1"
    assert spans[0]["metadata"]["tokens"] == 42
    ev_span = [e for e in spans if e["name"] == "event.judge"][0]
    assert ev_span["metadata"]["overall"] == 4
    assert ev_span["startTime"].startswith("1970")  # ts=1000 → 1970-01-01 UTC（可读性无妨，格式正确）


def test_export_run_writes_jsonl(tmp_path):
    store = TraceStore(str(tmp_path / "trace.db"))
    store.save_run(make_run())
    out = tmp_path / "r1.jsonl"
    stats = export_run(store, "r1", out)
    assert stats["spans"] == 2
    lines = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["type"] == "trace-create"
    assert any(l["name"] == "case.q1" for l in lines)


def test_export_run_unknown_raises(tmp_path):
    store = TraceStore(str(tmp_path / "trace.db"))
    with pytest.raises(KeyError):
        export_run(store, "nope", tmp_path / "x.jsonl")


def test_export_all_covers_every_run(tmp_path):
    store = TraceStore(str(tmp_path / "trace.db"))
    r1, r2 = make_run(), make_run()
    r2.run_id = "r2"
    store.save_run(r1)
    store.save_run(r2)
    out = tmp_path / "all.jsonl"
    stats = export_all(store, out)
    assert stats["runs"] == 2
    ids = [json.loads(l)["id"] for l in out.read_text(encoding="utf-8").splitlines()
           if json.loads(l)["type"] == "trace-create"]
    assert ids == ["r1", "r2"]


def test_export_all_includes_spans_and_events(tmp_path):
    """AGT-5 回归（评审 2.0）：批量导出曾用 list_runs 丢全部 span/event——
    现逐 run 完整装载，total 与实际行数对账。"""
    store = TraceStore(str(tmp_path / "trace.db"))
    run = make_run()
    run.spans = [Span(span_id="sp1", name="root", started_at=1.0, ended_at=2.0,
                      status="ok")]
    run.events = [Event(event_id="ev1", run_id=run.run_id, span_id="sp1",
                        name="started", ts=1.0)]
    store.save_run(run)
    out = tmp_path / "all.jsonl"
    stats = export_all(store, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert stats["events"] == len(lines) == 1 + 1 + 1  # trace-create + span + event(降级瞬时 span)
    names = [json.loads(l).get("name", "") for l in lines]
    assert "root" in names and "event.started" in names  # span 与事件都在，不再静默丢失
