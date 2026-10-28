"""CI 回归门禁：对比 baseline 与 current 报告，任一指标劣化超过阈值 → 非零退出码。

指标方向约定：
- success_rate / judge_pass_rate / judge_mean：越高越好（下降超阈值即拦截）
- latency_p50_ms / latency_p95_ms：越低越好（上升超阈值即拦截）
- error_count：越低越好（上升超阈值即拦截）
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 默认阈值：可按指标覆盖
DEFAULT_THRESHOLDS: dict[str, float] = {
    "success_rate": 0.02,      # 成功率下降 > 2pp 拦截
    "judge_pass_rate": 0.05,   # judge 通过率下降 > 5pp 拦截
    "judge_mean": 0.30,        # judge 均分下降 > 0.3 拦截
    "latency_p95_ms": 2000.0,  # P95 上升 > 2s 拦截
    "latency_p50_ms": 1000.0,
    "error_count": 0.0,        # 新增任何 error 拦截
}

HIGHER_IS_BETTER = {"success_rate", "judge_pass_rate", "judge_mean"}


@dataclass
class MetricDiff:
    metric: str
    baseline: float
    current: float
    delta: float                    # current - baseline
    threshold: float
    direction: str                  # "higher" | "lower"
    failed: bool
    reason: str = ""

    def to_row(self) -> list[str]:
        sign = "+" if self.delta >= 0 else ""
        return [
            self.metric,
            f"{self.baseline:.4g}",
            f"{self.current:.4g}",
            f"{sign}{self.delta:.4g}",
            f"{self.threshold:g}",
            "FAIL" if self.failed else "ok",
        ]


@dataclass
class GateResult:
    passed: bool
    diffs: list[MetricDiff] = field(default_factory=list)
    baseline_name: str = ""
    current_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "baseline": self.baseline_name,
            "current": self.current_name,
            "diffs": [
                {"metric": d.metric, "baseline": d.baseline, "current": d.current,
                 "delta": d.delta, "threshold": d.threshold, "failed": d.failed}
                for d in self.diffs
            ],
        }

    def render_table(self) -> str:
        header = f"{'metric':<18}{'baseline':>12}{'current':>12}{'delta':>12}{'阈值':>10}  结果"
        sep = "-" * len(header)
        lines = [header, sep]
        for d in self.diffs:
            row = d.to_row()
            lines.append(f"{row[0]:<18}{row[1]:>12}{row[2]:>12}{row[3]:>12}{row[4]:>10}  {row[5]}")
        lines.append(sep)
        verdict = "PASS ✅ 当前报告未触发任何回归阈值" if self.passed \
            else "FAIL ❌ 存在超阈值回归，禁止合入"
        lines.append(f"门禁结论: {verdict}")
        return "\n".join(lines)


def compare_metrics(baseline: dict[str, Any], current: dict[str, Any],
                    thresholds: dict[str, float] | None = None) -> GateResult:
    """对同一组指标做方向感知的阈值比较。阈值可按指标覆盖，未配置的指标跳过。"""
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update({k: float(v) for k, v in thresholds.items()})
        # 显式传 -1 或 inf 可关闭某指标检查
    diffs: list[MetricDiff] = []
    for metric, limit in th.items():
        if metric not in baseline or metric not in current:
            continue
        b = float(baseline[metric])
        c = float(current[metric])
        delta = c - b
        higher_better = metric in HIGHER_IS_BETTER
        if higher_better:
            failed = (b - c) > limit
            direction = "higher"
            reason = f"下降 {b - c:.4g} > 阈值 {limit:g}" if failed else ""
        else:
            failed = (c - b) > limit
            direction = "lower"
            reason = f"上升 {c - b:.4g} > 阈值 {limit:g}" if failed else ""
        diffs.append(MetricDiff(metric=metric, baseline=b, current=c, delta=delta,
                                threshold=limit, direction=direction,
                                failed=failed, reason=reason))
    return GateResult(
        passed=not any(d.failed for d in diffs),
        diffs=diffs,
        baseline_name=str(baseline.get("run_id", "baseline")),
        current_name=str(current.get("run_id", "current")),
    )


def load_report(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if "metrics" not in data:
        raise ValueError(f"{p} 不是 RunReport JSON（缺 metrics 字段）")
    return data


def compare_reports(baseline_path: str | Path, current_path: str | Path,
                    thresholds: dict[str, float] | None = None) -> GateResult:
    """读两份 RunReport JSON 做门禁比较。"""
    base = load_report(baseline_path)
    cur = load_report(current_path)
    result = compare_metrics(base["metrics"], cur["metrics"], thresholds)
    result.baseline_name = str(base.get("run_id", baseline_path))
    result.current_name = str(cur.get("run_id", current_path))
    return result


def gate(baseline_path: str | Path, current_path: str | Path,
         thresholds: dict[str, float] | None = None,
         stream: Any = sys.stdout) -> int:
    """CLI 入口：打印差异表，返回退出码（0=通过，1=拦截）。"""
    try:
        result = compare_reports(baseline_path, current_path, thresholds)
    except Exception as exc:
        print(f"[gate] 报告读取失败: {exc}", file=sys.stderr)
        return 2
    print(result.render_table())
    return 0 if result.passed else 1
