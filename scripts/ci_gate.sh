#!/usr/bin/env bash
# agenteval CI 回归门禁一键脚本：跑 smoke 适配器评测 → gate 对比基线 → 劣化则 exit 1 挡合并。
#
# 用法（在主项目 CI 的门禁 step 中调用）:
#   bash agenteval/scripts/ci_gate.sh <adapter> <base_url> <baseline.json> [metric=value ...]
# 例:
#   bash agenteval/scripts/ci_gate.sh askbi http://127.0.0.1:8000 \
#     agenteval/baselines/askbi_smoke_baseline.json
#
# 约定：
# - 评测集为内置 builtin:<adapter>_smoke（30 条，v2.0.0，零 key 可跑）；
# - judge 固定 MockJudge（确定性规则打分，与基线同口径）；
# - 正确性指标（success_rate / judge_pass_rate / judge_mean / error_count）是门禁信号；
# - mock 口径下时延不是信号（CI 共享机噪声大），默认放大时延阈值避免误拦，
#   追加位置参数 metric=value 可进一步覆盖/收紧阈值；
# - 环境变量 GATE_OUT_DIR 可把报告固定到指定目录（CI 上传 artifact 用），否则用临时目录。
set -euo pipefail

ADAPTER="${1:?用法: ci_gate.sh <adapter> <base_url> <baseline.json> [metric=value ...]}"
BASE_URL="${2:?缺少 base_url}"
BASELINE="${3:?缺少 baseline.json 路径}"
shift 3

THRESH_ARGS=(--threshold latency_p50_ms=1000000 --threshold latency_p95_ms=1000000)
for kv in "$@"; do THRESH_ARGS+=(--threshold "$kv"); done

if [ -n "${GATE_OUT_DIR:-}" ]; then
  OUT_DIR="$GATE_OUT_DIR"
  mkdir -p "$OUT_DIR"
else
  OUT_DIR="$(mktemp -d)"
  trap 'rm -rf "$OUT_DIR"' EXIT
fi

echo "[ci-gate] 1/2 评测: builtin:${ADAPTER}_smoke -> ${BASE_URL}"
python -m agenteval run --dataset "builtin:${ADAPTER}_smoke" \
  --target http --adapter "$ADAPTER" --base-url "$BASE_URL" \
  --judge mock --name "ci-gate-${ADAPTER}" --out "$OUT_DIR"

CURRENT="$(ls -t "$OUT_DIR"/*.json | head -1)"
echo "[ci-gate] 2/2 门禁: baseline=${BASELINE} vs current=${CURRENT}"
python -m agenteval gate --baseline "$BASELINE" --current "$CURRENT" "${THRESH_ARGS[@]}"
