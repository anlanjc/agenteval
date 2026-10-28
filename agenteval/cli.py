"""CLI：python -m agenteval run|gate|report|calibrate。

示例：
  python -m agenteval run --dataset builtin:policy_compass_smoke --target mock --judge mock
  python -m agenteval gate --baseline <baseline.json> --current <current.json>
  python -m agenteval report --run-id <id> --format html --out reports
  python -m agenteval calibrate --scores <scores.json>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dataset import EvalDataset
from .judge import MockJudge, OpenAICompatibleJudge
from .mockagent import MockAgentTarget


def _resolve_dataset(spec: str) -> EvalDataset:
    from .api import resolve_dataset
    return resolve_dataset(spec)


def _build_target(args):
    from .adapters import get_adapter
    from .runner import HTTPTarget
    if args.target == "mock":
        return MockAgentTarget(degrade_ratio=args.degrade_ratio)
    if args.target == "openai":
        return HTTPTarget(base_url=args.base_url, mode="openai",
                          model=args.model or "default",
                          response_path="choices.0.message.content")
    if args.adapter:
        return get_adapter(args.adapter, args.base_url)
    return HTTPTarget(base_url=args.base_url, mode="raw",
                      response_path=args.response_path)


def _build_judge(args):
    if args.judge == "mock":
        return MockJudge()
    return OpenAICompatibleJudge()  # 缺 key 会在 score 时抛 MissingCredentialsError


def cmd_run(args) -> int:
    from .report import write_reports
    from .runner import Runner

    dataset = _resolve_dataset(args.dataset)
    if args.limit:
        dataset = dataset.sample(args.limit)
    target = _build_target(args)
    if isinstance(target, MockAgentTarget):
        target.bind_dataset(dataset.ids())
    runner = Runner(target=target, judge=_build_judge(args), store=TraceStore(args.db),
                    judge_threshold=args.threshold)
    report = runner.run(dataset, name=args.name)
    paths = write_reports(report, args.out)
    m = report.metrics.to_dict()
    print(f"run_id: {report.run_id}")
    print(f"dataset: {report.dataset} v{report.dataset_version}  target: {report.target}"
          f"  judge: {report.judge}")
    print(f"cases: {m['case_count']}  errors: {m['error_count']}  "
          f"success_rate: {m['success_rate']:.2%}  judge_pass_rate: {m['judge_pass_rate']:.2%}  "
          f"judge_mean: {m['judge_mean']:.3f}")
    print(f"latency P50/P95: {m['latency_p50_ms']:.1f}/{m['latency_p95_ms']:.1f} ms  "
          f"tokens≈{m['total_tokens']}")
    print("reports:")
    for k, v in paths.items():
        print(f"  {k}: {v}")
    return 0


def cmd_gate(args) -> int:
    from .gate import gate
    thresholds = {}
    for kv in args.threshold or []:
        k, v = kv.split("=", 1)
        thresholds[k] = float(v)
    return gate(args.baseline, args.current, thresholds)


def cmd_report(args) -> int:
    from .report import render_html, render_markdown
    from .runner import RunReport

    report: RunReport | None = None
    # ① 先查注册表 runs.db（run 命令/API 产生）
    reg_db = Path(args.db).parent / "runs.db"
    if reg_db.exists():
        import sqlite3
        conn = sqlite3.connect(reg_db)
        row = conn.execute("SELECT report_json FROM eval_runs WHERE run_id=?",
                           (args.run_id,)).fetchone()
        conn.close()
        if row:
            report = RunReport.from_dict(json.loads(row[0]))
    # ② 退化到 reports 目录中的 json
    if report is None:
        p = Path(args.out) / f"{args.run_id}.json"
        if not p.exists():
            print(f"找不到 run: {args.run_id}（查过 {reg_db} 与 {p}）", file=sys.stderr)
            return 2
        report = RunReport.from_dict(json.loads(p.read_text(encoding="utf-8")))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fmt = args.format
    if fmt == "html":
        path = out / f"{report.run_id}.html"
        path.write_text(render_html(report), encoding="utf-8")
    elif fmt == "json":
        path = out / f"{report.run_id}.json"
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")
    else:
        path = out / f"{report.run_id}.md"
        path.write_text(render_markdown(report), encoding="utf-8")
    print(f"written: {path}")
    return 0


def cmd_calibrate(args) -> int:
    from .judge import CalibrationSample, evaluate_threshold, sweep_thresholds

    data = json.loads(Path(args.scores).read_text(encoding="utf-8"))
    samples = [CalibrationSample(case_id=s["case_id"], golden=s["golden"],
                                 predicted="pass", overall=float(s["overall"]))
               for s in data["samples"]]
    t0 = data.get("threshold", args.threshold)
    r0 = evaluate_threshold(samples, t0)
    best_t, best_r, _ = sweep_thresholds(samples)
    print(r0.markdown())
    print()
    print(f"阈值扫描最优: threshold={best_t}  kappa={best_r.kappa:.4f}  "
          f"accuracy={best_r.accuracy:.4f}")
    if args.out:
        out = {"initial": r0.to_dict(), "best_threshold": best_t, "best": best_r.to_dict()}
        Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"written: {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agenteval", description="自研 Agent 评测与可观测平台")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="跑一次评测")
    pr.add_argument("--dataset", default="builtin:policy_compass_smoke",
                    help="builtin:policy_compass_smoke | builtin:askbi_smoke | JSONL 路径")
    pr.add_argument("--target", default="mock", choices=["mock", "http", "openai"])
    pr.add_argument("--base-url", default="http://127.0.0.1:8000")
    pr.add_argument("--model", default=None)
    pr.add_argument("--response-path", default="answer")
    pr.add_argument("--adapter", default=None, help="policy_compass | askbi（与 --target http 搭配）")
    pr.add_argument("--degrade-ratio", type=float, default=0.0, help="mock target 注入错误比例")
    pr.add_argument("--judge", default="mock", choices=["mock", "openai_compatible"])
    pr.add_argument("--threshold", type=float, default=4.0, help="judge pass 阈值")
    pr.add_argument("--limit", type=int, default=None)
    pr.add_argument("--name", default="cli-run")
    pr.add_argument("--db", default=".agenteval/store.db")
    pr.add_argument("--out", default=".agenteval/reports")
    pr.set_defaults(func=cmd_run)

    pg = sub.add_parser("gate", help="回归门禁：对比 baseline/current")
    pg.add_argument("--baseline", required=True)
    pg.add_argument("--current", required=True)
    pg.add_argument("--threshold", action="append", dest="threshold",
                    help="覆盖阈值，如 judge_mean=0.5（可多次）")
    pg.set_defaults(func=cmd_gate)

    prep = sub.add_parser("report", help="从 run 生成报告文件")
    prep.add_argument("--run-id", required=True)
    prep.add_argument("--db", default=".agenteval/store.db")
    prep.add_argument("--format", default="md", choices=["md", "html", "json"])
    prep.add_argument("--out", default=".agenteval/reports")
    prep.set_defaults(func=cmd_report)

    pc = sub.add_parser("calibrate", help="judge 校准（golden vs judge 决策）")
    pc.add_argument("--scores", required=True,
                    help="JSON：{'samples':[{'case_id','golden','overall'}], 'threshold':4.0}")
    pc.add_argument("--threshold", type=float, default=4.0)
    pc.add_argument("--out", default=None, help="校准报告 JSON 输出路径")
    pc.set_defaults(func=cmd_calibrate)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
