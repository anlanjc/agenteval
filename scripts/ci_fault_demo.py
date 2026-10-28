"""CI 门禁故障注入演示：注入 8 条用例劣化 → gate FAIL → 修复 → gate PASS。

不碰主项目源码：在评测平台侧用 FaultTarget 包装适配器，对指定用例注入
错误（模拟服务 5xx）或劣化输出（模拟答非所问），等价于"主项目合并了一个劣化 PR"。
评测/门禁走与 ci_gate.sh 完全相同的 Runner + MockJudge + gate 路径。

用法（需先起对应主项目 mock 服务）:
  python scripts/ci_fault_demo.py --adapter askbi --base-url http://127.0.0.1:8801 \
      --baseline baselines/askbi_smoke_baseline.json
退出码：0=演示自洽（FAIL 阶段 gate=1 且 PASS 阶段 gate=0）；2=演示不符合预期。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agenteval import gate as gate_mod
from agenteval.adapters import get_adapter
from agenteval.api import resolve_dataset
from agenteval.judge import MockJudge
from agenteval.runner import Runner, TargetResult

WRONG_OUTPUT = "很抱歉，您请求的指标数据暂无法提供，请稍后再试。（fault-inject 模拟主项目回归的劣化输出）"

# 每个项目注入 8 条：3 条服务错误 + 5 条输出劣化
DEFAULT_FAULTS: dict[str, dict[str, str]] = {
    "askbi": {f"ab-{i:03d}": "error" for i in range(4, 7)}
    | {f"ab-{i:03d}": "wrong" for i in range(7, 12)},
    "policy_compass": {f"pc-{i:03d}": "error" for i in range(4, 7)}
    | {f"pc-{i:03d}": "wrong" for i in range(7, 12)},
}

LATENCY_THRESHOLDS = {"latency_p50_ms": 1000000.0, "latency_p95_ms": 1000000.0}


class FaultTarget:
    """把适配器包一层：对指定用例注入故障，其余用例原样透传。"""

    name = "fault-injected"

    def __init__(self, inner, faults: dict[str, str]):
        self.inner = inner
        self.faults = faults

    def __call__(self, case) -> TargetResult:
        kind = self.faults.get(case.id)
        if kind is None:
            return self.inner(case)
        if kind == "error":
            return TargetResult(output="", error="RuntimeError: [fault-inject] 模拟主项目服务 500")
        return TargetResult(output=WRONG_OUTPUT)


def run_once(target, dataset_spec: str, tmp: Path, name: str) -> Path:
    runner = Runner(target=target, judge=MockJudge())
    report = runner.run(resolve_dataset(dataset_spec), name=name)
    path = tmp / f"{name}.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    m = report.metrics.to_dict()
    print(f"  run: cases={m['case_count']} errors={m['error_count']} "
          f"success={m['success_rate']:.2%} judge_pass={m['judge_pass_rate']:.2%} "
          f"judge_mean={m['judge_mean']:.3f}")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, choices=sorted(DEFAULT_FAULTS))
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--baseline", required=True)
    args = ap.parse_args()

    dataset_spec = f"builtin:{args.adapter}_smoke"
    faults = DEFAULT_FAULTS[args.adapter]
    adapter = get_adapter(args.adapter, args.base_url)
    ok = True

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        print(f"=== 阶段一：注入 {len(faults)} 条用例劣化（3 error + 5 wrong-output）===")
        fail_path = run_once(FaultTarget(adapter, faults), dataset_spec, tmp, "faulted")
        print("  gate 对比基线：")
        rc_fail = gate_mod.gate(args.baseline, fail_path, LATENCY_THRESHOLDS)
        print(f"  gate 退出码 = {rc_fail}（预期 1 = 拦截合入）")
        ok &= rc_fail == 1

        print("=== 阶段二：移除故障注入（= 主项目修复）后复跑 ===")
        pass_path = run_once(adapter, dataset_spec, tmp, "repaired")
        print("  gate 对比基线：")
        rc_pass = gate_mod.gate(args.baseline, pass_path, LATENCY_THRESHOLDS)
        print(f"  gate 退出码 = {rc_pass}（预期 0 = 放行）")
        ok &= rc_pass == 0

    print("=== 演示结论 ===")
    print("  通过：门禁对 8 条劣化全部拦截(FAIL)，修复后放行(PASS)"
          if ok else "  不符合预期：gate 退出码与预期不符")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
