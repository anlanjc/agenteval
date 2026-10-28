"""trace.py 测试：token 估算、span 树、异常记录、SQLite 持久化往返。"""

import pytest

from agenteval.trace import Event, Span, TraceRun, TraceStore, Tracer, estimate_tokens


def test_estimate_tokens_ascii_and_cjk():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1        # 4 字符 ≈ 1 token
    assert estimate_tokens("中文字") == 3       # CJK 约 1 字/token
    assert estimate_tokens("中文abc") == 3      # 2 CJK + ceil(3/4)
    assert estimate_tokens("中文") == 2


def test_tracer_nested_spans_and_tree():
    tracer = Tracer("demo", {"k": "v"})
    with tracer.span("root", {"a": 1}) as root:
        with tracer.span("child") as child:
            tracer.event("tick", {"n": 1})
        with tracer.span("child2"):
            pass
    run = tracer.finish()
    assert run.name == "demo"
    assert run.attributes["k"] == "v"
    assert len(run.spans) == 3
    tree = run.span_tree()
    assert tree["name"] == "root"
    assert [c["name"] for c in tree["children"]] == ["child", "child2"]
    assert root.duration_ms >= 0
    assert child.parent_span_id == root.span_id
    # 事件挂在最内层 span
    assert run.events[0].span_id == child.span_id
    assert run.events[0].attributes["n"] == 1


def test_tracer_records_error_and_reraises():
    tracer = Tracer("err-demo")
    with pytest.raises(ValueError, match="boom"):
        with tracer.span("failing"):
            raise ValueError("boom")
    run = tracer.finish(status="error")
    sp = run.spans[0]
    assert sp.status == "error"
    assert "ValueError" in sp.error and "boom" in sp.error
    assert run.status == "error"


def test_store_roundtrip_spans_and_events(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    tracer = Tracer("roundtrip", {"dataset": "d1"})
    with tracer.span("root", {"i": 0}) as root:
        with tracer.span("leaf", {"tokens": 42}) as leaf:
            tracer.event("llm.request", {"model": "mock"})
    run = tracer.finish()
    store.save_run(run)

    loaded = store.load_run(run.run_id)
    assert loaded is not None
    assert loaded.name == "roundtrip"
    assert loaded.attributes["dataset"] == "d1"
    assert {s.name for s in loaded.spans} == {"root", "leaf"}
    leaf_loaded = [s for s in loaded.spans if s.name == "leaf"][0]
    assert leaf_loaded.attributes["tokens"] == 42
    assert leaf_loaded.parent_span_id == root.span_id
    assert len(loaded.events) == 1
    assert loaded.events[0].name == "llm.request"
    # 树结构在重载后仍成立
    tree = loaded.span_tree()
    assert tree["children"][0]["name"] == "leaf"
    assert loaded.total_tokens == 42
    store.close()


def test_store_list_and_missing(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    for i in range(3):
        t = Tracer(f"r{i}")
        with t.span("s"):
            pass
        store.save_run(t.finish())
    runs = store.list_runs()
    assert len(runs) == 3
    assert store.load_run("nonexistent") is None
    assert store.load_spans("nonexistent") == []
    assert store.load_events("nonexistent") == []
    store.close()


def test_span_duration_and_run_helpers():
    sp = Span(span_id="s1", name="x", started_at=1.0, ended_at=1.5)
    assert sp.duration_ms == pytest.approx(500.0)
    run = TraceRun(run_id="r1", spans=[sp], events=[Event("e1", "r1", "s1", "ev", 1.1)])
    assert run.duration_ms == pytest.approx(500.0)
    assert run.root is sp
    d = run.to_dict()
    assert d["spans"][0]["duration_ms"] == 500.0
    assert d["events"][0]["name"] == "ev"
