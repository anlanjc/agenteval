"""gate.py 测试：指标方向感知、阈值拦截、报告文件对比、CLI 退出码。"""

import json

import pytest

from agenteval.gate import DEFAULT_THRESHOLDS, compare_metrics, compare_reports, gate


def _metrics(**kw):
    base = {"case_count": 30, "error_count": 0, "success_rate": 1.0,
            "judge_pass_rate": 1.0, "judge_mean": 4.9,
            "latency_p50_ms": 12.0, "latency_p95_ms": 25.0, "total_tokens": 900}
    base.update(kw)
    return base


def test_gate_blocks_on_judge_regression():
    baseline = _metrics()
    current = _metrics(judge_mean=3.2, judge_pass_rate=0.6)
    result = compare_metrics(baseline, current)
    assert result.passed is False
    failed = {d.metric for d in result.diffs if d.failed}
    assert "judge_mean" in failed and "judge_pass_rate" in failed
    assert "success_rate" not in failed


def test_gate_allows_healthy_change():
    result = compare_metrics(_metrics(), _metrics(judge_mean=4.95, latency_p95_ms=40.0))
    assert result.passed is True


def test_gate_latency_direction_aware():
    # P95 上升超过阈值 → 拦截（低好方向）
    result = compare_metrics(_metrics(), _metrics(latency_p95_ms=3000.0))
    assert result.passed is False
    diff = [d for d in result.diffs if d.metric == "latency_p95_ms"][0]
    assert diff.failed and diff.direction == "lower"
    # P95 下降 → 放行
    ok = compare_metrics(_metrics(), _metrics(latency_p95_ms=20.0))
    assert ok.passed is True


def test_gate_error_count_any_increase_blocks():
    result = compare_metrics(_metrics(), _metrics(error_count=1, success_rate=0.97))
    assert result.passed is False
    failed = {d.metric for d in result.diffs if d.failed}
    assert "error_count" in failed and "success_rate" in failed


def test_gate_threshold_override():
    # 覆盖阈值后原本拦截的下降变为放行
    result = compare_metrics(_metrics(), _metrics(judge_mean=4.5),
                             thresholds={"judge_mean": 0.5})
    d = [x for x in result.diffs if x.metric == "judge_mean"][0]
    assert d.threshold == 0.5
    assert result.passed is True


def test_gate_render_table_contains_fail_mark():
    result = compare_metrics(_metrics(), _metrics(judge_mean=3.0))
    table = result.render_table()
    assert "FAIL" in table and "judge_mean" in table
    payload = result.to_dict()
    assert payload["passed"] is False


def test_gate_compare_report_files(tmp_path):
    def report_json(run_id, metrics):
        return {"run_id": run_id, "metrics": metrics}
    base_p = tmp_path / "base.json"
    cur_p = tmp_path / "cur.json"
    base_p.write_text(json.dumps(report_json("base-1", _metrics())), encoding="utf-8")
    cur_p.write_text(json.dumps(report_json("cur-1", _metrics(judge_mean=3.0))),
                     encoding="utf-8")
    result = compare_reports(base_p, cur_p)
    assert result.passed is False
    assert result.baseline_name == "base-1" and result.current_name == "cur-1"
    # 缺 metrics 字段 → 报错
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    with pytest.raises(ValueError):
        compare_reports(base_p, bad)


def test_gate_cli_exit_codes(tmp_path, capsys):
    base_p = tmp_path / "base.json"
    ok_p = tmp_path / "ok.json"
    bad_p = tmp_path / "bad.json"
    base_p.write_text(json.dumps({"run_id": "b", "metrics": _metrics()}), encoding="utf-8")
    ok_p.write_text(json.dumps({"run_id": "o", "metrics": _metrics()}), encoding="utf-8")
    bad_p.write_text(json.dumps({"run_id": "x", "metrics": _metrics(judge_mean=2.0)}),
                     encoding="utf-8")
    assert gate(base_p, ok_p) == 0
    assert gate(base_p, bad_p) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    # 文件不存在 → 退出码 2
    assert gate(base_p, tmp_path / "missing.json") == 2


def test_gate_ignores_unknown_metrics():
    result = compare_metrics({"custom_metric": 1.0}, {"custom_metric": 99.0})
    assert result.passed is True       # 未配置阈值的指标不检查
    assert result.diffs == []


# ---------------------------------------------------------- AE-5（评审 4.0）

def _report_file(tmp_path, run_id, dataset="askbi_smoke", version="2.0.0",
                 case_count=30, **mkw):
    """带口径头部的报告：dataset/dataset_version/metrics.case_count（RunReport.to_dict 形态）。"""
    data = {"run_id": run_id, "name": run_id, "dataset": dataset,
            "dataset_version": version, "metrics": _metrics(case_count=case_count, **mkw)}
    p = tmp_path / f"{run_id}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_gate_rejects_dataset_version_mismatch(tmp_path, capsys):
    # 改数据集不重出基线：版本错位 → 硬失败 exit 2，stderr 明确报错（fail-closed）
    base = _report_file(tmp_path, "base")
    cur = _report_file(tmp_path, "cur", version="2.1.0")
    assert gate(base, cur) == 2
    err = capsys.readouterr().err
    assert "dataset_version" in err and "2.0.0" in err and "2.1.0" in err


def test_gate_rejects_dataset_name_mismatch(tmp_path, capsys):
    base = _report_file(tmp_path, "base")
    cur = _report_file(tmp_path, "cur", dataset="policy_compass_smoke")
    assert gate(base, cur) == 2
    assert "dataset" in capsys.readouterr().err


def test_gate_rejects_case_count_mismatch(tmp_path, capsys):
    # 不同 case 集的指标互比 → 拦截信号失真，硬失败
    base = _report_file(tmp_path, "base", case_count=30)
    cur = _report_file(tmp_path, "cur", case_count=10)
    assert gate(base, cur) == 2
    assert "case_count" in capsys.readouterr().err


def test_gate_same_triple_compares_normally(tmp_path, capsys):
    # 口径一致 → 正常指标比较（劣化仍按阈值出 1）
    base = _report_file(tmp_path, "base")
    ok = _report_file(tmp_path, "ok")
    bad = _report_file(tmp_path, "bad", judge_mean=3.0)
    assert gate(base, ok) == 0
    assert gate(base, bad) == 1
    capsys.readouterr()  # 清空
    assert "口径不一致" not in capsys.readouterr().err


def test_gate_minimal_reports_without_triple_still_work(tmp_path):
    # 两边都没有口径字段的历史最小报告 → 不误伤（字段缺失跳过，不比较不存在的东西）
    base_p = tmp_path / "b.json"
    cur_p = tmp_path / "c.json"
    base_p.write_text(json.dumps({"run_id": "b", "metrics": _metrics()}), encoding="utf-8")
    cur_p.write_text(json.dumps({"run_id": "c", "metrics": _metrics()}), encoding="utf-8")
    assert gate(base_p, cur_p) == 0
