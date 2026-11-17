# CI 跨项目回归门禁集成指南（AE-2，2026-10-29）

> 一句话：agenteval 不再是孤岛——askbi 与 policy-compass 的 GitHub Actions 各自多了一个
> `eval-gate` job：**起本项目 mock 模式服务 → agenteval 打真 HTTP 跑 30 条 smoke → gate 对比
> 基线 → 任一正确性指标劣化即 exit 1 挡合并**。全链路零 API key。

## 1. 门禁在工作区里长什么样

```
主项目 CI (eval-gate job)
 ├─ checkout 主项目 + checkout agenteval（同 owner，main 分支）
 ├─ pip install 主项目依赖 + pip install -e ./agenteval
 ├─ 数据就绪：askbi `python scripts/gen_data.py`（seed 42，确定性）
 │             policy-compass `python scripts/ingest.py`（BM25-only，EMBED/RERANK 显式关）
 ├─ 起 mock 服务（固定环境变量，见 §3）+ 轮询健康检查
 └─ bash agenteval/scripts/ci_gate.sh <adapter> http://127.0.0.1:8000 \
        agenteval/baselines/<adapter>_smoke_baseline.json
        ├─ step1: python -m agenteval run --dataset builtin:<adapter>_smoke
        │         --target http --adapter <adapter> --judge mock   → current.json
        └─ step2: python -m agenteval gate --baseline ... --current ... → exit 0/1
```

`gate` 是方向感知的：success_rate / judge_pass_rate / judge_mean 下降超阈值、error_count
新增、即 FAIL（exit 1）。mock 口径下时延不是信号，`ci_gate.sh` 默认放大时延阈值避免
CI 共享机噪声误拦；可追加位置参数 `metric=value` 收紧。

## 2. 前提

- agenteval 仓库推送到 GitHub，且与两个主项目**同 owner**（CI 里
  `repository: ${{ github.repository_owner }}/agenteval`）；分支 main。
- 基线（`baselines/*.json`）与数据集版本（`_version: 2.0.0`）配套——改 smoke 数据集
  必须重新生成基线（见 §4），否则 gate 数字口径错位。（AE-5：此配套不再纯靠人工纪律——
  gate 现会比较前校验两份报告的 dataset/dataset_version/case_count 三元组，
  不一致 stderr 报错并 exit 2 硬失败。）
- askbi 基线生成环境 = CI 环境（LLM_PROVIDER=mock、PROFILE=conservative、
  gen_data seed 42）；policy-compass 同理（POLICY_COMPASS_FORCE_MOCK=1、
  EMBED_ENABLED=false、RERANK_ENABLED=false、ingest 确定性切句）。换任何一项都要重出基线。

## 3. 主项目侧 CI 改动一览

| 项目 | 文件 | 改动 | 原有步骤 |
|---|---|---|---|
| askbi | `.github/workflows/ci.yml` | 只**追加** `eval-gate` job；原 `test` job 一字未动 | gen_data → pytest → v1/v2 评测 → 报告上传，全部保留 |
| policy-compass | `.github/workflows/ci.yml` | **新建**（此前无 CI）；`test` job 按本地口径（ingest → pytest → v1 评测 smoke）+ `eval-gate` job | test job 环境变量与门禁基线同源（见 §2） |

红线执行记录（改前/改后各跑一遍全量 pytest，本地）：askbi 94 passed → 94 passed；
policy-compass 23 passed → 23 passed（ambient 环境），另跑 CI 同款固定 mock 环境也 23 passed。
详见 `evidence/ci_gate_demo.md` §5。

## 4. 本地复现 / 维护

```bash
# ① 起主项目 mock 服务
cd askbi && DB_PATH=/tmp/askbi.duckdb LLM_PROVIDER=mock PROFILE=conservative \
  python scripts/gen_data.py && \
  DB_PATH=/tmp/askbi.duckdb LLM_PROVIDER=mock PROFILE=conservative \
  python -m uvicorn app.api.main:app --port 8801

cd policy-compass && POLICY_COMPASS_FORCE_MOCK=1 EMBED_ENABLED=false RERANK_ENABLED=false \
  DB_PATH=/tmp/pc_index.db python scripts/ingest.py && \
  POLICY_COMPASS_FORCE_MOCK=1 EMBED_ENABLED=false RERANK_ENABLED=false \
  DB_PATH=/tmp/pc_index.db APP_PORT=8802 python scripts/serve.py

# ② 一键门禁（与 CI 同一脚本）
bash agenteval/scripts/ci_gate.sh askbi http://127.0.0.1:8801 \
  agenteval/baselines/askbi_smoke_baseline.json
bash agenteval/scripts/ci_gate.sh policy_compass http://127.0.0.1:8802 \
  agenteval/baselines/policy_compass_smoke_baseline.json

# ③ 门禁本身会拦坏变更吗？（故障注入演示：8 条劣化 → FAIL → 修复 → PASS）
python agenteval/scripts/ci_fault_demo.py --adapter askbi \
  --base-url http://127.0.0.1:8801 --baseline agenteval/baselines/askbi_smoke_baseline.json

# ④ 重出基线（改了 smoke 数据集或主项目 mock 行为升级后）
python agenteval/scripts/build_smoke_v2.py --probe-askbi <重探> --probe-pc <重探>
# 探测方式见 build_smoke_v2.py 头注释：全量请求主项目 v2 评测集，存输出，再选例构建
```

## 5. smoke 数据集 v2.0.0 的设计口径

- **代表性子集**：askbi 30 条 = dataset_v2(116) 的 query17/drill4/clarify5/refusal4
  （含 south/north 行级权限各 1）；policy-compass 30 条 = eval_set_v2(90) 的
  fact11/compare4/calc4/refuse4/timeliness3/slang3/multi1（只取 main split）。
- **expected = 基线正确输出快照**：mock 口径下答案确定，快照即金标——faithfulness 的
  "输出数字必须都在 expected 中"才能对长文本政策摘要正确工作（PC 输出是大段政策原文，
  手写 expected 无法覆盖所有数字）。
- **keywords = 语义锚点**（构建脚本逐条断言在基线输出中）：指标名/金额/行数/拒答话术等，
  回归时 keywords miss 或输出冒出新数字都会被 gate 捕捉。
- 只收 mock 下 on-topic 的用例：PC 已知 7 条 mock 弱检索用例（v2 mock 92.2% 的缺口）
  留在主项目自己的完整评测集里，**门禁只钉"稳定行为的回归"，不把已知缺陷固化为金标**。

## 6. 集成过程中发现并修复的平台 bug

| bug | 影响 | 修复 |
|---|---|---|
| `cli.py` `cmd_run` 用 `TraceStore` 未导入 | `python -m agenteval run` 必然 NameError——门禁入口 CLI 从未被端到端跑通过 | 补 lazy import + `tests/test_ci_integration.py` CLI 回归测试 |
| `pyproject.toml` 缺 `[build-system]`/包声明 | `pip install -e .` 直接失败（flat-layout 多顶层目录自动发现报错）——平台根本装不进 CI | 显式 packages + package-data，干净 venv 验证可装可用 |
| 旧 smoke 数据集从未对活服务验证 | 10 条用真适配器打真 mock 服务仅 10% 通过（keywords 拍脑袋、faithfulness 误杀） | v2.0.0 全量探测后构建，30/30 双项目实测通过 |

这三条是"评测平台被真实消费后才暴露"的问题——正是本次集成的价值之一。

## 7. agentgate-go 网关接入（[1B4-SM] AE-11/AE-12，2026-11-18）

- **适配器**：`agenteval/adapters/agentgate_go.py`——POST `/v1/chat/completions`
  （OpenAI 兼容），`Authorization: Bearer <vk>`，解析 `choices[0].message.content`；
  `usage.total_tokens` / `x-cache`（M3 响应缓存命中标记）/ `x-request-id` / finish_reason
  进 meta 留痕。实例级惰性 Client + `close()`（Runner.run finally 通道）——AE-8 先例
  逐字对齐。冒烟集 `builtin:gateway_smoke`（30 条，v2.0.0，与适配器名不同构，属有意命名：
  gateway_smoke 指"网关冒烟"口径，适配器键 `agentgate_go` 指实现文件）。
- **本地演练已自洽**（全记录 `evidence/gateway_gate_demo_20261118.md`）：便携 redis +
  echo mock 上游（`scripts/gateway_mock_upstream.py`，回显"结论：…"模板供 MockJudge
  确定性打分；与 agentgate-go 自带的压测口径 mock_upstream.py 互不替代）+ 网关三进程
  同机拉起 → seed vk（适配器默认 key `ag-smoke`，budget/tpm 不限额）→ 基线
  `baselines/gateway_smoke_baseline.json`（30/30，judge_mean 5.000，文件内含口径字段）→
  gate PASS exit 0 → FaultTarget 劣化注入 8 条 → 四指标 FAIL exit 1 → 修复 PASS exit 0 →
  真网关级故障（杀上游）30 错 FAIL exit 1 → 恢复 PASS exit 0。
- **CI 常驻口径（留档，未实施）**：CI 上跑这套门禁需要三个常驻服务位——①redis
  （service 容器或 `redis-server --port`，demo 口径 6392）②echo/压测 mock 上游
  （python 服务，端口自定）③agentgate-go（job 内 `go build ./...` 出二进制后直接跑，
  `REDIS_ADDR/UPSTREAM_1_URL/PORT` 注入）；vk 预置步骤（redis-cli SET vk:<sha256>）
  放网关启动后、评测前。本机排错实录：Windows 控制台 curl 发中文 body 是 GBK 编码，
  网关转发原字节会让上游解码 500 并触发通道冷却——评测链路走 httpx（UTF-8）无此问题；
  网关级故障恢复后通道冷却 60s 且被失败请求刷新，对账前需静默期。
