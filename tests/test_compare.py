"""多版本对比评测测试：指标 delta、逐用例翻转、输出漂移、渲染与落盘。"""
import json

import pytest

from agenteval.compare import compare_report_files, compare_runs
from agenteval.runner import CaseResult, RunMetrics, RunReport


def make_report(target: str, results: dict[str, bool], outputs: dict[str, str] | None = None,
                latencies: dict[str, float] | None = None) -> RunReport:
    outputs = outputs or {}
    latencies = latencies or {}
    cases = [
        CaseResult(case_id=cid, input=f"输入{cid}", output=outputs.get(cid, f"输出{cid}@{target}"),
                   expected="期望", passed=ok, latency_ms=latencies.get(cid, 100.0))
        for cid, ok in results.items()
    ]
    n = len(results)
    ok_n = sum(results.values())
    metrics = RunMetrics(
        case_count=n, error_count=0, success_rate=1.0,
        judge_pass_rate=ok_n / n, judge_mean=5.0 * ok_n / n,
        latency_p50_ms=100.0, latency_p95_ms=200.0, total_tokens=1000,
    )
    return RunReport(run_id=f"run-{target}", name="eval", dataset="demo.jsonl",
                     dataset_version="v1", target=target, judge="mock",
                     created_at=0.0, metrics=metrics, cases=cases)


def test_identical_runs_no_diff():
    outputs = {"a": "输出a", "b": "输出b"}
    base = make_report("v1", {"a": True, "b": False}, outputs=outputs)
    cand = make_report("v2", {"a": True, "b": False}, outputs=outputs)
    rep = compare_runs(base, cand)
    assert rep.summary == {"baseline": "v1", "candidate": "v2",
                           "regressions": 0, "fixes": 0, "output_changed": 0}


def test_fix_and_regression_detected():
    base = make_report("v1", {"a": True, "b": True, "c": False})
    cand = make_report("v2", {"a": True, "b": False, "c": True})
    rep = compare_runs(base, cand)
    assert [c.case_id for c in rep.fixes] == ["c"]
    assert [c.case_id for c in rep.regressions] == ["b"]


def test_dataset_mismatch_rejected():
    base = make_report("v1", {"a": True})
    cand = make_report("v2", {"a": True})
    cand.dataset = "other.jsonl"
    with pytest.raises(ValueError, match="数据集"):
        compare_runs(base, cand)


def test_case_set_mismatch_rejected():
    base = make_report("v1", {"a": True, "b": True})
    cand = make_report("v2", {"a": True, "c": True})
    with pytest.raises(ValueError, match="用例集合"):
        compare_runs(base, cand)


def test_metrics_delta_direction_aware():
    base = make_report("v1", {"a": True, "b": True, "c": True, "d": False})
    cand = make_report("v2", {"a": True, "b": True, "c": False, "d": True})
    rep = compare_runs(base, cand)
    by_name = {d.metric: d for d in rep.metrics_diff}
    # judge_pass_rate：3/4 → 3/4（翻转抵消），delta=0
    assert by_name["judge_pass_rate"].delta == pytest.approx(0.0)
    # 成功率下降 0 → 仍为 0（success_rate 无 error 占比，mock 下不变）
    assert by_name["latency_p95_ms"].direction == "lower"


def test_output_drift_detected_when_verdict_unchanged():
    base = make_report("v1", {"a": True}, outputs={"a": "答案A"})
    cand = make_report("v2", {"a": True}, outputs={"a": "答案A（更详细）"})
    rep = compare_runs(base, cand)
    assert rep.output_changed == ["a"]


def test_latency_deltas_per_case():
    base = make_report("v1", {"a": True}, latencies={"a": 100.0})
    cand = make_report("v2", {"a": True}, latencies={"a": 40.0})
    rep = compare_runs(base, cand)
    assert rep.latency_deltas["a"] == pytest.approx(-60.0)


def test_markdown_render_and_save(tmp_path):
    base = make_report("v1", {"a": True, "b": False})
    cand = make_report("v2", {"a": False, "b": True})
    rep = compare_runs(base, cand)
    md = rep.render_markdown()
    assert "对比评测报告" in md and "修复明细" in md and "回归明细" in md
    out = tmp_path / "compare"
    rep.save(out)
    payload = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["summary"]["regressions"] == 1
    assert (out.with_suffix(".md")).exists()


def test_compare_report_files_roundtrip(tmp_path):
    base = make_report("v1", {"a": True, "b": False})
    cand = make_report("v2", {"a": True, "b": True})
    for name, rep in (("base", base), ("cand", cand)):
        (tmp_path / f"{name}.json").write_text(
            json.dumps(rep.to_dict(), ensure_ascii=False), encoding="utf-8")
    rep = compare_report_files(tmp_path / "base.json", tmp_path / "cand.json")
    assert [c.case_id for c in rep.fixes] == ["b"]
