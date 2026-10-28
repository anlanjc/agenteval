"""report.py 测试：Markdown 与单文件 HTML 渲染、失败高亮、文件写出。"""

import json

from agenteval.judge import MockJudge
from agenteval.mockagent import MockAgentTarget
from agenteval.dataset import EvalCase, EvalDataset
from agenteval.report import render_html, render_markdown, write_reports
from agenteval.runner import CaseResult, RunMetrics, RunReport, Runner


def _report_with_failure(tmp_path):
    cases = [
        CaseResult(case_id="c1", input="q1", output="好答案 2000", expected="2000",
                   latency_ms=10.0, verdict={"overall": 5.0, "scores": {}, "reasoning": "对"},
                   passed=True),
        CaseResult(case_id="c2", input="q2", output="错答案 999", expected="2000",
                   latency_ms=20.0, error=None,
                   verdict={"overall": 2.1, "scores": {"correctness": 1.0}, "reasoning": "关键词未命中"},
                   passed=False),
    ]
    metrics = RunMetrics(case_count=2, error_count=0, success_rate=1.0,
                         judge_pass_rate=0.5, judge_mean=3.55,
                         latency_p50_ms=15.0, latency_p95_ms=20.0, total_tokens=42)
    return RunReport(run_id="run-x", name="demo", dataset="ds", dataset_version="1.0",
                     target="mock", judge="mock", created_at=1700000000.0,
                     metrics=metrics, cases=cases)


def test_markdown_contains_metrics_and_failure_section():
    md = render_markdown(_report_with_failure(None))
    assert "# 评测报告：demo" in md
    assert "| 成功率 | 100.00% |" in md
    assert "| judge 均分 (1-5) | 3.550 |" in md
    assert "## 失败 Case（高亮）" in md
    assert "### ❌ c2" in md
    assert "关键词未命中" in md


def test_html_single_file_and_fail_highlight():
    html = render_html(_report_with_failure(None))
    assert html.startswith("<!DOCTYPE html>")
    assert "<style>" in html                       # 内嵌样式
    assert "fail-row" in html                      # 失败行高亮
    assert "无失败 case" not in html                # 有失败 case
    # 转义：output 中的 HTML 字符不应注入
    r = _report_with_failure(None)
    r.cases[0].output = "<script>alert(1)</script>"
    assert "<script>alert(1)</script>" not in render_html(r)


def test_write_reports_three_files(tmp_path):
    r = _report_with_failure(tmp_path)
    out_dir = tmp_path / "out"
    paths = write_reports(r, out_dir)
    assert set(paths) == {"json", "md", "html"}
    for p in paths.values():
        assert out_dir.joinpath(out_dir.name).exists() or True  # 占位，真实断言如下
    assert (out_dir / "run-x.json").exists()
    assert (out_dir / "run-x.md").exists()
    assert (out_dir / "run-x.html").exists()
    d = json.loads((out_dir / "run-x.json").read_text(encoding="utf-8"))
    assert d["run_id"] == "run-x" and d["metrics"]["judge_pass_rate"] == 0.5
    assert "fail-row" in (out_dir / "run-x.html").read_text(encoding="utf-8")


def test_report_from_real_runner_run(tmp_path):
    ds = EvalDataset(name="t", version="1", cases=[
        EvalCase("c-good", "问题", expected="最高2000元", metadata={"keywords": ["2000"]}),
        EvalCase("c-bad", "问题", expected="最高2000元", metadata={"keywords": ["2000"]}),
    ])
    from agenteval.runner import CallableTarget

    def agent(case):
        return "结论：最高2000元。" if case.id == "c-good" else "结论：金额是1999元。"
    report = Runner(target=CallableTarget(agent), judge=MockJudge()).run(ds)
    md = render_markdown(report)
    html = render_html(report)
    assert "### ❌ c-bad" in md
    assert "fail-row" in html
    assert "c-good" in md
