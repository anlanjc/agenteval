# CI 跨项目回归门禁集成证据（AE-2 会话 · 2026-10-29）

> 任务：①agenteval 门禁真接进 askbi / policy-compass 的 GitHub Actions；②smoke 适配器
> 10→30 条（对齐两主项目各自 v2 评测集代表性子集）；③本地完整走一遍
> "注入 8 个错误 → 门禁 FAIL → 修复 → PASS"。
> 红线：主项目 CI 改动不影响原有 step（改前改后全量 pytest 各一遍）；不碰两项目源码；git 只提交 agenteval。

## 1. smoke 数据集 v2.0.0 构建（10→30）

方法：先对两主项目 mock 模式服务**全量探测**各自 v2 评测集，用真实输出做选例依据——
（探测原始输出：`ci_gate_probe_askbi.json` 116 条 / `ci_gate_probe_pc.json` 90 条；
构建脚本 `scripts/build_smoke_v2.py`，keywords 逐条断言在基线输出中）。

- askbi 30 条：query 17 / drill 4 / clarify 5 / refusal 4（对齐 `askbi/eval/dataset_v2.json`
  116 条的分层；含 south/north 行级权限用例各 1，适配器为此新增 per-case role 透传）。
- policy-compass 30 条：fact 11 / compare 4 / calc 4 / refuse 4 / timeliness 3 / slang 3 /
  multi 1（对齐 `policy-compass/data/eval_set_v2.jsonl` 90 条七层；只取 main split）。
- 设计口径：`expected`=基线输出快照（faithfulness 数字核对依据）、`keywords`=语义锚点、
  `metadata.source_id`=主项目原条目 id 可追溯；只收 mock 下 on-topic 的用例——
  PC 已知 7 条 mock 弱检索用例（v2 mock 92.2% 缺口）留给主项目完整评测，**不把已知缺陷固化成门禁金标**。

**发现**：旧 10 条 smoke 用真适配器打真服务只有 10% 通过（keywords 从未对活服务验证过，
test_adapters 只用手搓 SSE 夹具）——v2 全部重建后 30/30 实测通过。

## 2. 确定性验证（CI 可复现的前提）

| 验证 | 结果 |
|---|---|
| askbi：gen_data 重新生成临时库（seed 42，复刻 CI）vs 原 10/26 库 | 30 条输出**逐字节一致**，judge_pass 30/30，judge_mean 均 4.707 |
| policy-compass：同一服务两次全量评测 | 30 条输出**逐字节一致**，judge_mean 均 4.760 |
| 固定环境 | askbi：LLM_PROVIDER=mock PROFILE=conservative；PC：POLICY_COMPASS_FORCE_MOCK=1 EMBED_ENABLED=false RERANK_ENABLED=false（与两份 ci.yml 完全一致） |

## 3. ci_gate.sh 一键门禁（PASS 路径实测）

```
$ bash scripts/ci_gate.sh askbi http://127.0.0.1:8801 baselines/askbi_smoke_baseline.json
[ci-gate] 1/2 评测: builtin:askbi_smoke -> http://127.0.0.1:8801
run_id: 24f1b51b8dad4970  dataset: askbi_smoke v2.0.0  target: askbi  judge: mock
cases: 30  errors: 0  success_rate: 100.00%  judge_pass_rate: 100.00%  judge_mean: 4.707
[ci-gate] 2/2 门禁: baseline=... vs current=...
success_rate / judge_pass_rate / judge_mean / latency / error_count 全 ok
门禁结论: PASS ✅ 当前报告未触发任何回归阈值     exit=0
```

## 4. 故障注入演示：8 错 → FAIL → 修复 → PASS（双项目全过）

不碰主项目源码：`scripts/ci_fault_demo.py` 用 FaultTarget 包装适配器注入
3 条服务错误 + 5 条劣化输出，评测与门禁走与 CI 完全相同的 Runner+MockJudge+gate 路径。

**askbi**（完整记录 `ci_gate_demo_askbi.txt`）：

```
阶段一（注入 8 条）：errors=3 success=90.00% judge_pass=73.33% judge_mean=4.333
  success_rate      1 → 0.9    (-0.10)   FAIL
  judge_pass_rate   1 → 0.7333 (-0.2667) FAIL
  judge_mean     4.707 → 4.333  (-0.3734) FAIL
  error_count       0 → 3      (+3)      FAIL
  门禁结论: FAIL ❌ 存在超阈值回归，禁止合入    gate 退出码 = 1 ✅（预期 1）
阶段二（移除注入=修复）：30/30 全过，judge_mean=4.707 与基线持平
  门禁结论: PASS ✅                              gate 退出码 = 0 ✅（预期 0）
```

**policy-compass**（完整记录 `ci_gate_demo_pc.txt`）：同构结果——
8 条劣化 → 4 项指标 FAIL → exit 1；修复后 30/30、4.760 与基线持平 → exit 0。

## 5. 红线执行记录（主项目 CI 改动前后全量 pytest）

| 项目 | 改 CI 前 | 改 CI 后 | CI 同款固定 mock 环境 |
|---|---|---|---|
| askbi | 94 passed | **94 passed**（`pytest_askbi_after_ci_edit.txt`） | —（CI gen_data+serve 环境已用临时库等价验证，见 §2） |
| policy-compass | 23 passed | **23 passed**（`pytest_pc_after_ci_edit.txt`） | 23 passed（POLICY_COMPASS_FORCE_MOCK=1 EMBED_ENABLED=false RERANK_ENABLED=false） |

CI 文件改动本身：askbi 只**追加** eval-gate job（原 test job 逐字未动）；
policy-compass 原本无 CI，新建（test job 按其 Makefile/README 本地口径）。两份 yml 均通过 PyYAML 解析验证。

## 6. 集成中发现并修复的平台 bug（详见 docs/ci_integration.md §6）

1. `cli.py` cmd_run `TraceStore` 未导入 → CLI 门禁入口 NameError（lazy import + 回归测试）。
2. `pyproject.toml` 无 `[build-system]`/包声明 → `pip install -e .` 失败（干净 venv 验证通过）。
3. 旧 smoke 10 条从未对活服务验证（10% 通过）→ v2 重建 30/30。

## 7. 复现命令

```bash
# 双服务（CI 同款环境，临时库）
cd askbi && DB_PATH=/tmp/askbi.duckdb LLM_PROVIDER=mock PROFILE=conservative \
  .venv/Scripts/python.exe scripts/gen_data.py && \
  DB_PATH=/tmp/askbi.duckdb LLM_PROVIDER=mock PROFILE=conservative \
  .venv/Scripts/python.exe -m uvicorn app.api.main:app --port 8801
cd policy-compass && POLICY_COMPASS_FORCE_MOCK=1 EMBED_ENABLED=false RERANK_ENABLED=false \
  DB_PATH=/tmp/pc_index.db python scripts/ingest.py && \
  POLICY_COMPASS_FORCE_MOCK=1 EMBED_ENABLED=false RERANK_ENABLED=false \
  DB_PATH=/tmp/pc_index.db APP_PORT=8802 python scripts/serve.py

# 门禁 + 演示
cd agenteval && .venv/Scripts/python.exe -m pytest -q        # 104 passed
bash scripts/ci_gate.sh askbi http://127.0.0.1:8801 baselines/askbi_smoke_baseline.json
bash scripts/ci_gate.sh policy_compass http://127.0.0.1:8802 baselines/policy_compass_smoke_baseline.json
.venv/Scripts/python.exe scripts/ci_fault_demo.py --adapter askbi --base-url http://127.0.0.1:8801 \
  --baseline baselines/askbi_smoke_baseline.json
```
