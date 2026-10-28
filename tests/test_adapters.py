"""adapters 测试：全部用 httpx.MockTransport 模拟同工作区两项目的 HTTP 契约，不真实调用。"""

import httpx
import pytest

from agenteval.adapters import get_adapter
from agenteval.adapters.askbi import AskBIAdapter, _answer_to_text, _parse_sse
from agenteval.adapters.policy_compass import PolicyCompassAdapter
from agenteval.dataset import EvalCase


def _case(q="杭州公积金租房提取每月最多能提多少"):
    return EvalCase(id="pc-001", input=q, expected="2000元", metadata={"keywords": ["2000"]})


# ---------------------------------------------------------------- policy-compass

def test_policy_compass_adapter_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        import json
        body = json.loads(request.content)
        captured["body"] = body
        return httpx.Response(200, json={
            "trace_id": "abc123", "question": body["question"],
            "answer": "杭州每月最高2000元", "refusal": False,
            "sources": [{"no": 1, "title": "hz_rent"}],
            "tool_result": None, "events": [], "latency_ms": 123.4,
        })

    adapter = PolicyCompassAdapter("http://mock.local", transport=httpx.MockTransport(handler))
    result = adapter(_case())
    assert result.error is None
    assert result.output == "杭州每月最高2000元"
    assert result.meta["refusal"] is False
    assert result.meta["source_count"] == 1
    assert result.meta["upstream_trace_id"] == "abc123"
    assert result.meta["upstream_latency_ms"] == 123.4
    assert captured["url"].endswith("/api/chat")
    assert captured["body"] == {"question": _case().input, "stream": False}


def test_policy_compass_adapter_refusal_passes_through():
    def handler(request):
        return httpx.Response(200, json={"answer": "超出知识库范围", "refusal": True,
                                         "sources": [], "trace_id": "t", "latency_ms": 5})
    adapter = PolicyCompassAdapter("http://mock.local", transport=httpx.MockTransport(handler))
    result = adapter(_case("杭州社保断缴影响落户吗"))
    assert result.meta["refusal"] is True
    assert result.output == "超出知识库范围"     # 拒答是合法行为，不是 error


def test_policy_compass_adapter_http_error():
    adapter = PolicyCompassAdapter("http://mock.local",
                                   transport=httpx.MockTransport(
                                       lambda r: httpx.Response(503)))
    result = adapter(_case())
    assert result.error and "503" in result.error


# ---------------------------------------------------------------- askbi

SSE_BODY = (
    'data: {"type": "start", "task_id": "tk-1"}\n\n'
    'data: {"type": "node_done", "node": "router"}\n\n'
    'data: {"type": "final", "task_id": "tk-1", '
    '"answer": {"kind": "query", "message": "上月销售额为358万元", "value": 3580000}, '
    '"elapsed_ms": 321.0}\n\n'
)


def test_askbi_adapter_sse_contract():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text=SSE_BODY,
                              headers={"content-type": "text/event-stream"})

    adapter = AskBIAdapter("http://mock.local", transport=httpx.MockTransport(handler))
    result = adapter(_case("上个月的销售额是多少"))
    assert result.error is None
    assert "358万元" in result.output
    assert result.meta["answer_kind"] == "query"
    assert result.meta["task_id"] == "tk-1"
    assert captured["body"]["role"] == "boss"


def test_askbi_adapter_json_fallback():
    def handler(request):
        return httpx.Response(200, json={
            "task_id": "tk-2", "answer": {"kind": "refusal", "message": "仅支持经营数据查询"},
            "elapsed_ms": 9.0})
    adapter = AskBIAdapter("http://mock.local", transport=httpx.MockTransport(handler))
    result = adapter(_case("帮我写周报"))
    assert result.output == "仅支持经营数据查询"
    assert result.meta["answer_kind"] == "refusal"


def test_askbi_sse_error_event_surfaces():
    body = 'data: {"type": "error", "message": "LLM quota exceeded"}\n\n'
    adapter = AskBIAdapter("http://mock.local",
                           transport=httpx.MockTransport(
                               lambda r: httpx.Response(200, text=body,
                                                        headers={"content-type": "text/event-stream"})))
    result = adapter(_case())
    assert result.error and "quota" in result.error


def test_askbi_sse_without_final_is_error():
    body = 'data: {"type": "start"}\n\n'
    adapter = AskBIAdapter("http://mock.local",
                           transport=httpx.MockTransport(
                               lambda r: httpx.Response(200, text=body,
                                                        headers={"content-type": "text/event-stream"})))
    result = adapter(_case())
    assert result.error and "final" in result.error


def test_askbi_helpers():
    assert _parse_sse(SSE_BODY)["type"] == "final"
    assert _answer_to_text({"kind": "query", "message": "m", "value": 5}) == "m\n结果：5"
    assert _answer_to_text({"kind": "x"}) == '{"kind": "x"}'
    assert _answer_to_text({"rows": [[1], [2]], "columns": ["a"]}) == "共 2 行数据"


# ---------------------------------------------------------------- 注册表

def test_get_adapter_registry():
    assert isinstance(get_adapter("policy_compass", "http://x"), PolicyCompassAdapter)
    assert isinstance(get_adapter("askbi", "http://x"), AskBIAdapter)
    with pytest.raises(ValueError, match="未知适配器"):
        get_adapter("nope", "http://x")


def test_adapters_work_end_to_end_with_runner():
    """适配器可直接作为 runner 的 target。"""
    from pathlib import Path

    from agenteval.dataset import EvalDataset
    from agenteval.runner import Runner
    adapter = PolicyCompassAdapter("http://mock.local", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"answer": "每月最高2000元", "refusal": False,
                                            "sources": [], "trace_id": "t", "latency_ms": 1})))
    ds_path = Path(__file__).parent.parent / "agenteval" / "datasets" / "policy_compass_smoke.jsonl"
    ds = EvalDataset.load_jsonl(ds_path)
    report = Runner(target=adapter).run(ds.sample(3))
    assert report.target == "policy_compass"
    assert report.metrics.success_rate == 1.0
