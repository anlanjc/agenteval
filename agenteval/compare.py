"""多版本对比评测：同一数据集上两个 agent 版本的逐条 diff 与指标对比（A/B 实验的核心件）。

回答面试必答题"你说优化了，优化了多少、怎么测的"：
- 指标层：复用 gate 的方向感知逻辑给出 delta（成功率/通过率/均分/P50/P95/token）
- 用例层：逐条对齐 pass→fail（回归）、fail→pass（修复）、输出差异、延迟差
- 报告：Markdown 一键导出，直接贴进 CI 评论或面试讲稿
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gate import compare_metrics
from .runner import RunReport


@dataclass
class CaseFlip:
    """单个用例在两版之间的判定翻转。"""
    case_id: str
    direction: str          # "regression" (pass→fail) | "fix" (fail→pass)
    baseline_output: str
    candidate_output: str
    expected: str
    note: str = ""

    def to_row(self) -> list[str]:
        return [self.case_id, self.direction, self.note or "-"]


@dataclass
class ComparisonReport:
    baseline_name: str
    candidate_name: str
    dataset: str
    metrics_diff: list[Any] = field(default_factory=list)   # gate.MetricDiff
    regressions: list[CaseFlip] = field(default_factory=list)   # pass→fail
    fixes: list[CaseFlip] = field(default_factory=list)         # fail→pass
    output_changed: list[str] = field(default_factory=list)     # 判定未翻但输出不同（语义漂移信号）
    latency_deltas: dict[str, float] = field(default_factory=dict)  # case_id → 候选-基线(ms)

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline_name,
            "candidate": self.candidate_name,
            "regressions": len(self.regressions),
            "fixes": len(self.fixes),
            "output_changed": len(self.output_changed),
        }

    def render_markdown(self) -> str:
        lines = [
            f"# 对比评测报告：{self.baseline_name} → {self.candidate_name}",
            "",
            f"- 数据集：{self.dataset}",
            f"- 修复 {len(self.fixes)} 条，回归 {len(self.regressions)} 条，"
            f"输出漂移 {len(self.output_changed)} 条",
            "",
            "## 指标对比（方向感知）",
            "",
            "| 指标 | 基线 | 候选 | Δ | 阈值 | 判定 |",
            "|---|---|---|---|---|---|",
        ]
        for d in self.metrics_diff:
            cells = d.to_row()
            lines.append("| " + " | ".join(str(c) for c in cells) + " |")
        for title, items in (("修复明细（fail→pass）", self.fixes),
                             ("回归明细（pass→fail）", self.regressions)):
            lines += ["", f"## {title}", ""]
            if not items:
                lines.append("_无_")
            for c in items:
                lines.append(f"- **{c.case_id}**：{c.note}")
        lines += ["", "## 输出漂移（判定未翻但输出不同——语义回归的早期信号）", ""]
        if not self.output_changed:
            lines.append("_无_")
        for cid in self.output_changed:
            lines.append(f"- {cid}")
        lines += ["", "## 延迟变化 Top（候选-基线，ms）", ""]
        for cid, delta in sorted(self.latency_deltas.items(), key=lambda kv: -abs(kv[1]))[:10]:
            lines.append(f"- {cid}: {delta:+.0f}ms")
        return "\n".join(lines) + "\n"

    def save(self, path: str | Path, markdown: bool = True) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        import json

        payload = {
            "summary": self.summary,
            "metrics_diff": [
                {"metric": d.metric, "baseline": d.baseline, "current": d.current,
                 "delta": d.delta, "threshold": d.threshold, "failed": d.failed}
                for d in self.metrics_diff
            ],
            "regressions": [vars(c) for c in self.regressions],
            "fixes": [vars(c) for c in self.fixes],
            "output_changed": list(self.output_changed),        # 评审 C3：漂移明细进结构化通道
            "latency_deltas": dict(self.latency_deltas),
        }
        p.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if markdown:
            p.with_suffix(".md").write_text(self.render_markdown(), encoding="utf-8")


def compare_runs(baseline: RunReport, candidate: RunReport,
                 thresholds: dict[str, float] | None = None) -> ComparisonReport:
    """对比两份 RunReport（同一数据集），产出指标 delta + 逐用例翻转 + 输出漂移。"""
    if baseline.dataset != candidate.dataset:
        raise ValueError(f"数据集不一致：{baseline.dataset} vs {candidate.dataset}")
    if (baseline.dataset_version and candidate.dataset_version
            and baseline.dataset_version != candidate.dataset_version):
        raise ValueError(
            f"数据集版本不一致（评审 C2）：{baseline.dataset_version} vs "
            f"{candidate.dataset_version}——expected 被修订过的两版报告不可互比")
    gate_result = compare_metrics(baseline.metrics.to_dict(),
                                  candidate.metrics.to_dict(), thresholds)
    metrics_diff = gate_result.diffs

    base_cases = {c.case_id: c for c in baseline.cases}
    cand_cases = {c.case_id: c for c in candidate.cases}
    if set(base_cases) != set(cand_cases):
        raise ValueError("用例集合不一致——请确认两版跑的是同一数据集版本")

    regressions, fixes, output_changed, latency = [], [], [], {}

    def _norm(text: str) -> str:
        # 评审 C1：漂移判定做空白归一，弱化真 LLM target 的措辞噪声
        # （完整噪声边界见 compare_adr.md §3；确定性 target 下信号最强）
        return "".join((text or "").split())

    for cid in sorted(base_cases):
        b, c = base_cases[cid], cand_cases[cid]
        latency[cid] = c.latency_ms - b.latency_ms
        # 评审 C4：expected 统一取 baseline——前置校验保证两版恒等
        expected = b.expected
        if b.passed and not c.passed:
            regressions.append(CaseFlip(
                case_id=cid, direction="regression",
                baseline_output=b.output, candidate_output=c.output,
                expected=expected,
                note=(c.error or _verdict_note(c.verdict) or "判定翻转为失败")))
        elif not b.passed and c.passed:
            fixes.append(CaseFlip(
                case_id=cid, direction="fix",
                baseline_output=b.output, candidate_output=c.output,
                expected=expected,
                note=_verdict_note(c.verdict) or "判定翻转为通过"))
        elif _norm(b.output) != _norm(c.output):
            output_changed.append(cid)

    return ComparisonReport(
        baseline_name=baseline.target or baseline.name,
        candidate_name=candidate.target or candidate.name,
        dataset=baseline.dataset,
        metrics_diff=metrics_diff,
        regressions=regressions,
        fixes=fixes,
        output_changed=output_changed,
        latency_deltas=latency,
    )


def compare_report_files(baseline_path: str | Path, candidate_path: str | Path,
                         thresholds: dict[str, float] | None = None) -> ComparisonReport:
    """从两份 RunReport JSON 文件做对比（CLI 入口用）。"""
    import json

    base = RunReport.from_dict(json.loads(Path(baseline_path).read_text(encoding="utf-8")))
    cand = RunReport.from_dict(json.loads(Path(candidate_path).read_text(encoding="utf-8")))
    return compare_runs(base, cand, thresholds)


def _verdict_note(verdict: dict[str, Any] | None) -> str:
    """从 judge 判定里提取一句人话（无 judge 返回空）。"""
    if not verdict:
        return ""
    parts = []
    if verdict.get("overall") is not None:
        parts.append(f"overall={verdict['overall']}")
    for dim in ("correctness", "faithfulness", "format"):
        v = (verdict.get("scores") or {}).get(dim)
        if v is not None:
            parts.append(f"{dim}={v}")
    reason = verdict.get("reason") or ""
    return "；".join(parts) + (f"｜{reason[:80]}" if reason else "")
