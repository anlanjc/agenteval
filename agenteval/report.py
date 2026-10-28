"""报告生成：Markdown + 单文件 HTML（内嵌样式，指标表 / case 明细 / 失败 case 高亮）。"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .runner import RunReport


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v else "0"
    return html.escape(str(v))


# ---------------------------------------------------------------- Markdown

def render_markdown(report: RunReport) -> str:
    m = report.metrics.to_dict()
    lines = [
        f"# 评测报告：{report.name}",
        "",
        f"- run_id: `{report.run_id}`",
        f"- dataset: {report.dataset} (v{report.dataset_version})",
        f"- target: {report.target}  |  judge: {report.judge}",
        "",
        "## 指标",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| case 数 | {m['case_count']} |",
        f"| 错误数 | {m['error_count']} |",
        f"| 成功率 | {m['success_rate']:.2%} |",
        f"| judge 通过率 | {m['judge_pass_rate']:.2%} |",
        f"| judge 均分 (1-5) | {m['judge_mean']:.3f} |",
        f"| 时延 P50 | {m['latency_p50_ms']:.1f} ms |",
        f"| 时延 P95 | {m['latency_p95_ms']:.1f} ms |",
        f"| 估算 tokens | {m['total_tokens']} |",
        "",
        "## Case 明细",
        "",
        "| case | 通过 | judge 均分 | 时延(ms) | 错误/理由 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in report.cases:
        overall = ""
        reasoning = ""
        if c.verdict:
            overall = str(c.verdict.get("overall", ""))
            reasoning = c.verdict.get("reasoning") or ""
        reason = c.error or reasoning
        mark = "✅" if c.passed else "❌"
        lines.append(f"| {c.case_id} | {mark} | {overall} | {c.latency_ms:.0f} | {reason[:80]} |")

    failed = [c for c in report.cases if not c.passed]
    if failed:
        lines += ["", "## 失败 Case（高亮）", ""]
        for c in failed:
            lines.append(f"### ❌ {c.case_id}")
            lines.append("")
            lines.append(f"- 输入：{c.input[:200]}")
            lines.append(f"- 期望：{c.expected[:200]}")
            lines.append(f"- 实际：{(c.error or c.output)[:300]}")
            if c.verdict:
                lines.append(f"- judge：{json.dumps(c.verdict.get('scores', {}), ensure_ascii=False)}"
                             f" overall={c.verdict.get('overall')}")
            lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- HTML（单文件）

_HTML_TMPL = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>agenteval 报告 · {name}</title>
<style>
  :root {{ --bg:#0f1420; --card:#171e2e; --line:#26304a; --fg:#dbe4f5; --muted:#8b97b0;
          --ok:#3ecf8e; --bad:#ff6b6b; --accent:#5b8cff; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--fg);
         font: 14px/1.6 "Segoe UI", "Microsoft YaHei", sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:28px 0 10px; color:var(--accent); }}
  .sub {{ color:var(--muted); margin-bottom:20px; }}
  .cards {{ display:flex; gap:14px; flex-wrap:wrap; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px 18px; min-width:140px; }}
  .card .k {{ color:var(--muted); font-size:12px; }}
  .card .v {{ font-size:22px; font-weight:600; margin-top:2px; }}
  .ok {{ color:var(--ok); }} .bad {{ color:var(--bad); }}
  table {{ border-collapse:collapse; width:100%; background:var(--card);
          border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:8px 12px; border-bottom:1px solid var(--line); text-align:left;
           vertical-align:top; }}
  th {{ background:#1b2438; color:var(--muted); font-weight:500; }}
  tr:last-child td {{ border-bottom:none; }}
  tr.fail-row {{ background:rgba(255,107,107,.08); }}
  tr.fail-row td:first-child {{ border-left:3px solid var(--bad); }}
  pre {{ background:#0b101c; border:1px solid var(--line); border-radius:8px;
        padding:10px 14px; overflow:auto; white-space:pre-wrap; word-break:break-all; }}
  code {{ background:#0b101c; padding:1px 5px; border-radius:4px; }}
</style>
</head>
<body>
<h1>评测报告：{name}</h1>
<div class="sub">run <code>{run_id}</code> · dataset {dataset} v{version} · target {target} · judge {judge}</div>

<h2>指标总览</h2>
<div class="cards">
  <div class="card"><div class="k">成功率</div><div class="v">{success_rate}</div></div>
  <div class="card"><div class="k">judge 通过率</div><div class="v">{judge_pass_rate}</div></div>
  <div class="card"><div class="k">judge 均分</div><div class="v">{judge_mean}</div></div>
  <div class="card"><div class="k">时延 P50 / P95</div><div class="v">{latency}</div></div>
  <div class="card"><div class="k">错误 / 用例</div><div class="v">{errors}</div></div>
  <div class="card"><div class="k">估算 tokens</div><div class="v">{tokens}</div></div>
</div>

<h2>Case 明细</h2>
<table>
<thead><tr><th>case</th><th>结果</th><th>judge 均分</th><th>时延 ms</th><th>错误 / 理由</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>

<h2>失败 Case（{fail_count}）</h2>
{fail_section}
</body>
</html>
"""


def render_html(report: RunReport) -> str:
    m = report.metrics.to_dict()
    rows: list[str] = []
    fail_blocks: list[str] = []
    for c in report.cases:
        overall = ""
        reasoning = ""
        if c.verdict:
            overall = f"{c.verdict.get('overall', '')}"
            reasoning = c.verdict.get("reasoning") or ""
        reason = c.error or reasoning
        cls = "fail-row" if not c.passed else ""
        mark = '<span class="ok">PASS</span>' if c.passed else '<span class="bad">FAIL</span>'
        rows.append(
            f'<tr class="{cls}"><td>{html.escape(c.case_id)}</td><td>{mark}</td>'
            f"<td>{_fmt(overall)}</td><td>{c.latency_ms:.0f}</td>"
            f"<td>{html.escape(reason[:160])}</td></tr>"
        )
        if not c.passed:
            fail_blocks.append(
                f"<pre>【{html.escape(c.case_id)}】\n输入: {html.escape(c.input[:300])}\n"
                f"期望: {html.escape(c.expected[:300])}\n"
                f"实际: {html.escape((c.error or c.output)[:400])}</pre>"
            )
    fail_section = "\n".join(fail_blocks) if fail_blocks else \
        '<div class="sub">无失败 case 🎉</div>'
    return _HTML_TMPL.format(
        name=html.escape(report.name),
        run_id=report.run_id,
        dataset=html.escape(report.dataset),
        version=html.escape(report.dataset_version),
        target=html.escape(report.target),
        judge=html.escape(report.judge),
        success_rate=f"{m['success_rate']:.1%}",
        judge_pass_rate=f"{m['judge_pass_rate']:.1%}",
        judge_mean=f"{m['judge_mean']:.2f}",
        latency=f"{m['latency_p50_ms']:.0f} / {m['latency_p95_ms']:.0f} ms",
        errors=f"{m['error_count']} / {m['case_count']}",
        tokens=m["total_tokens"],
        rows="\n".join(rows),
        fail_count=len([c for c in report.cases if not c.passed]),
        fail_section=fail_section,
    )


def write_reports(report: RunReport, out_dir: str | Path) -> dict[str, str]:
    """写出 md/html/json 三件套，返回路径表。"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": str(d / f"{report.run_id}.json"),
        "md": str(d / f"{report.run_id}.md"),
        "html": str(d / f"{report.run_id}.html"),
    }
    Path(paths["json"]).write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    Path(paths["md"]).write_text(render_markdown(report), encoding="utf-8")
    Path(paths["html"]).write_text(render_html(report), encoding="utf-8")
    return paths
