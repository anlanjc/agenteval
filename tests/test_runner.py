"""runner.py 测试：metrics 计算、HTTP target（MockTransport）、trace 落库、错误兜底。"""

import httpx
import pytest

from agenteval.dataset import EvalCase, EvalDataset
from agenteval.judge import MockJudge
from agenteval.mockagent import MockAgentTarget
from agenteval.runner import (CallableTarget, HTTPTarget, Runner, percentile)
from agenteval.trace import TraceStore


def _dataset(n=3):
    return EvalDataset(name="t", version="1", cases=[
        EvalCase(f"c{i}", f"问题{i}", expected=f"每月最高200{i}元",
                 metadata={"keywords": [f"200{i}"]}) for i in range(n)
    ])


def test_percentile_interpolation():
    assert percentile([], 50) == 0.0
    assert percentile([5.0], 95) == 5.0
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)
    assert percentile([10, 20], 0) == 10.0


def test_runner_mock_agent_with_judge(tmp_path):
    store = TraceStore(tmp_path / "s.db")
    target = MockAgentTarget().bind_dataset([f"c{i}" for i in range(3)])
    report = Runner(target=target, judge=MockJudge(), store=store).run(_dataset(), name="r1")
    m = report.metrics.to_dict()
    assert m["case_count"] == 3
    assert m["success_rate"] == 1.0
    assert m["judge_pass_rate"] == 1.0
    assert m["judge_mean"] == pytest.approx(5.0)
    assert report.cases[0].verdict["overall"] == 5.0
    assert report.dataset == "t" and report.judge == "mock"
    # trace 已落库：root + 3×(case + target.call + judge.score) = 10 spans
    run = store.load_run(report.run_id)
    assert len(run.spans) == 1 + 3 * 3
    names = {s.name for s in run.spans}
    assert {"case[c0]", "target.call", "judge.score"} <= names
    tree = run.span_tree()
    assert tree["name"] == "r1"
    assert len(tree["children"]) == 3
    store.close()


def test_runner_without_judge(tmp_path):
    report = Runner(target=CallableTarget(lambda c: f"答案 for {c.id}")).run(_dataset(2))
    assert report.metrics.judge_mean == 0.0
    assert report.metrics.judge_pass_rate == report.metrics.success_rate == 1.0
    assert report.cases[0].passed is True


def test_runner_callable_returning_target_result(tmp_path):
    from agenteval.runner import TargetResult
    t = CallableTarget(lambda c: TargetResult(output="ok", meta={"x": 1}))
    report = Runner(target=t).run(_dataset(1))
    assert report.cases[0].meta["x"] == 1


def _mock_http(handler):
    return httpx.MockTransport(handler)


def test_http_target_raw_mode(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat"
        import json
        body = json.loads(request.content)
        return httpx.Response(200, json={"answer": f"每月最高2000元（{body['question']}）",
                                         "trace_id": "up-1"})
    target = HTTPTarget("http://mock.local", mode="raw", response_path="answer",
                        transport=_mock_http(handler))
    report = Runner(target=target).run(_dataset(2))
    assert report.metrics.success_rate == 1.0
    assert report.cases[0].output.startswith("每月最高2000元")
    assert report.cases[0].meta["raw_keys"] == ["answer", "trace_id"]


def test_http_target_openai_mode():
    def handler(request):
        import json
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "user"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "openai 风格回答 2000"}}]})
    target = HTTPTarget("http://mock.local", mode="openai", transport=_mock_http(handler))
    report = Runner(target=target, judge=MockJudge()).run(_dataset(1))
    assert report.cases[0].output == "openai 风格回答 2000"
    # judge 对 mock 回答打分（关键词 2000 命中）
    assert report.metrics.judge_pass_rate == 1.0


def test_http_target_error_becomes_case_error():
    def handler(request):
        return httpx.Response(500, text="boom")
    target = HTTPTarget("http://mock.local", mode="raw", transport=_mock_http(handler))
    report = Runner(target=target).run(_dataset(2))
    assert report.metrics.error_count == 2
    assert report.metrics.success_rate == 0.0
    assert "500" in report.cases[0].error or "HTTP" in report.cases[0].error


def test_http_target_missing_field_is_error():
    def handler(request):
        return httpx.Response(200, json={"unexpected": 1})
    target = HTTPTarget("http://mock.local", mode="raw", response_path="answer",
                        transport=_mock_http(handler))
    report = Runner(target=target).run(_dataset(1))
    assert report.cases[0].error and "answer" in report.cases[0].error


def test_runner_target_exception_does_not_crash_run():
    def boom(case):
        raise RuntimeError("target crashed")
    report = Runner(target=CallableTarget(boom)).run(_dataset(2))
    assert report.metrics.error_count == 2
    assert "target crashed" in report.cases[0].error


def test_runner_report_roundtrip_dict(tmp_path):
    target = MockAgentTarget().bind_dataset(["c0"])
    report = Runner(target=target, judge=MockJudge()).run(_dataset(1), name="rt")
    d = report.to_dict()
    from agenteval.runner import RunReport
    r2 = RunReport.from_dict(d)
    assert r2.run_id == report.run_id
    assert r2.metrics.judge_mean == report.metrics.judge_mean
    assert r2.cases[0].verdict == report.cases[0].verdict
