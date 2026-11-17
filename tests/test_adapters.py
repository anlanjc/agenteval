"""adapters 测试：全部用 httpx.MockTransport 模拟同工作区两项目的 HTTP 契约，不真实调用。"""

import httpx
import pytest

from agenteval.adapters import get_adapter
from agenteval.adapters.askbi import AskBIAdapter, _answer_to_text, _parse_sse
from agenteval.adapters.agentgate_go import AgentGateGoAdapter
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


# ---------------------------------------------------------- AE-8（评审 4.0）

def _pc_handler(calls):
    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"answer": "每月最高2000元", "refusal": False,
                                         "sources": [], "trace_id": "t", "latency_ms": 1})
    return handler


def test_policy_compass_adapter_reuses_client_across_cases():
    calls = {"n": 0}
    adapter = PolicyCompassAdapter("http://mock.local",
                                   transport=httpx.MockTransport(_pc_handler(calls)))
    adapter(_case("q1"))
    adapter(_case("q2"))
    assert calls["n"] == 2
    assert adapter._client is not None           # 连接池实例级保留（AE-8 前逐 case 新建销毁）
    first = adapter._client
    adapter(_case("q3"))
    assert adapter._client is first              # 同一 Client 复用，不逐 case 重建
    adapter.close()
    assert adapter._client is None


def test_askbi_adapter_reuses_client_and_close():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"answer": {"kind": "query", "message": "结论"},
                                         "elapsed_ms": 5, "task_id": "t1"})

    adapter = AskBIAdapter("http://mock.local", transport=httpx.MockTransport(handler))
    adapter(_case("q1"))
    adapter(_case("q2"))
    assert calls["n"] == 2
    first = adapter._client
    assert first is not None
    adapter(_case("q3"))
    assert adapter._client is first
    adapter.close()
    assert adapter._client is None


def test_runner_closes_adapter_after_run():
    """Runner.run 的 finally 通道（AGT-11）对适配器同样生效：评测结束归还连接池。"""
    from pathlib import Path

    from agenteval.dataset import EvalDataset
    from agenteval.runner import Runner
    adapter = PolicyCompassAdapter("http://mock.local", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"answer": "每月最高2000元", "refusal": False,
                                            "sources": [], "trace_id": "t", "latency_ms": 1})))
    ds_path = Path(__file__).parent.parent / "agenteval" / "datasets" / "policy_compass_smoke.jsonl"
    ds = EvalDataset.load_jsonl(ds_path)
    Runner(target=adapter).run(ds.sample(2))
    assert adapter._client is None               # Runner finally 已调 adapter.close()


# ---------------------------------------------------------------- agentgate-go（[1B4-SM] AE-11）

def _gw_response(content="结论：已按网关口径返回。", total=30):
    return httpx.Response(200, json={
        "id": "cmpl-gw", "model": "gateway-smoke-mock",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": total},
    }, headers={"x-cache": "MISS", "x-request-id": "req-1"})


def test_agentgate_go_adapter_contract():
    """OpenAI 兼容契约：POST /v1/chat/completions + Bearer vk + messages 形状；解析 content 与账面。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        import json
        captured["body"] = json.loads(request.content)
        return _gw_response(content="结论：已收到问题并返回。")

    adapter = AgentGateGoAdapter("http://mock.local/", api_key="ag-test",
                                 transport=httpx.MockTransport(handler))
    result = adapter(_case("网关冒烟问题"))
    assert result.error is None
    assert result.output == "结论：已收到问题并返回。"
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["auth"] == "Bearer ag-test"
    assert captured["body"]["model"] == "gateway-smoke-mock"
    assert captured["body"]["stream"] is False
    assert captured["body"]["messages"] == [{"role": "user", "content": "网关冒烟问题"}]
    assert result.meta["usage_total_tokens"] == 30
    assert result.meta["x_cache"] == "MISS"
    assert result.meta["request_id"] == "req-1"
    assert result.meta["finish_reason"] == "stop"


def test_agentgate_go_case_level_model_overrides():
    """用例级 metadata.model 优先于构造默认（对齐 askbi 的 case 级 role 覆盖手法）。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        captured["body"] = json.loads(request.content)
        return _gw_response()

    adapter = AgentGateGoAdapter("http://mock.local", model="default-model",
                                 transport=httpx.MockTransport(handler))
    case = EvalCase(id="gw-001", input="q", expected="e",
                    metadata={"model": "case-model"})
    adapter(case)
    assert captured["body"]["model"] == "case-model"


def test_agentgate_go_adapter_error_paths():
    """HTTP 5xx / 缺 choices 两种失败都收敛为 TargetResult.error（不抛穿透）。"""
    adapter = AgentGateGoAdapter("http://mock.local",
                                 transport=httpx.MockTransport(lambda r: httpx.Response(502)))
    result = adapter(_case())
    assert result.error and "502" in result.error

    def empty(request):
        return httpx.Response(200, json={"id": "x", "choices": []})
    adapter2 = AgentGateGoAdapter("http://mock.local",
                                  transport=httpx.MockTransport(empty))
    result2 = adapter2(_case())
    assert result2.error and "choices" in result2.error


def test_agentgate_go_client_reused_and_closed():
    """AE-8 先例：跨 case 复用同一实例级 Client；close() 置 None。"""
    adapter = AgentGateGoAdapter("http://mock.local",
                                 transport=httpx.MockTransport(lambda r: _gw_response()))
    adapter(_case("q1"))
    first = adapter._client
    assert first is not None
    adapter(_case("q2"))
    assert adapter._client is first          # 同一实例（keep-alive 复用）
    adapter.close()
    assert adapter._client is None


def test_runner_closes_agentgate_go_adapter(tmp_path):
    """Runner.run 的 finally 关闭通道对 agentgate_go 适配器生效（AE-8 同款钉子）。"""
    import json as _json

    from agenteval.judge import MockJudge
    from agenteval.runner import Runner

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _gw_response()

    adapter = AgentGateGoAdapter("http://mock.local",
                                 transport=httpx.MockTransport(handler))
    runner = Runner(target=adapter, judge=MockJudge())
    from agenteval.dataset import EvalDataset
    runner.run(EvalDataset(name="gw-smoke", version="2.0.0", cases=[_case("q")]), name="gw-close-check")
    assert calls["n"] == 1
    assert adapter._client is None           # finally 通道已归还连接池
