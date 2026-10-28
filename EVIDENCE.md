# agenteval EVIDENCE.md（去 demo 化证据册 · SM-1 会话 2026-10-29）

## 1. 做了什么 / 没做什么

**做了**
- judge 运行间波动正式实验（`scripts/judge_variance.py`，三阶段，230 次调用）：
  - 阶段1：同一 30 条 golden 数据集、同一 judge（v2 rubric）连跑 5 遍——每遍
    accuracy/kappa/混淆，kappa 分布，运行两两判官一致性，逐 case 分数波动，
    5 遍多数决 + bootstrap 95% CI；
  - 阶段2：pointwise 位置偏差——prompt 里 [参考要点] 与 [被评输出] 段序翻转对比；
  - 阶段3：pairwise 位置偏差——10 对（同题一好一坏候选）A/B 顺序 + 翻转。
- `.agenteval/judge_variance_report.md`：把 v1 rubric 两次裸跑 "0.4444 vs 0.5455"
  的旧疑问做成正式结论（详见该报告结论节）。
- `real_judge_calibration.md` 追记交叉引用；git 仓库初始化 + evidence/ 归档。
- **judge.py 零改动**（95 测试全程绿）——本窗口只做实验，不动被试。

**没做（并说明为什么）**
- 多模型 judge 对照（第二模型验证结论可迁移）：预算窗口内 230 次调用已用掉本任务
  大部分配额，且换模型要重做 rubric 校准才有可比性——留给后续。
- 200+ 条独立人工标注 TODO：不变（校准拟合 vs 无偏估计的口径问题，与波动实验正交）。
- 阈值扫描复跑：复用二轮校准结论（调阈值免费），本次波动实验固定 t=4.0 口径。

## 2. 三个关键决策

1. **kappa 对标签直接算，不走 evaluate_threshold**：判官间一致性（运行两两 kappa）
   是标签对标签，evaluate_threshold 是分数过阈值——初版误用导致全 0（见 §3），
   修复后两两 kappa 均值 0.9333。
2. **波动实验复用校准的同一份 30 条数据集**：与二轮校准完全可比（同一 judge、同一
   golden、同一 t=4.0），旧疑问才能被"正式结论"而不是"另一组数字"回答。
3. **多数决（majority-of-5）作为零成本稳定器**：温度=0 不等于确定性（服务端），
   5 遍多数决 kappa 0.8889——同一份数据跑多遍取多数是生产可用的方差缓解手段。

## 3. 三个真实 bug（本窗口发现）

1. **两两 kappa 误用 evaluate_threshold（已修复）**：CalibrationSample(overall=0) 过
   阈值判定 → 全部判 fail → 两两 kappa 恒 0.0000、多数决 kappa 恒 0.0000。
   修复：改用 `cohen_kappa` 直接算标签一致性 + `--recompute` 离线重算（不重调 LLM）。
2. **bootstrap 初版逻辑错误（已修复）**：只重采样 majority 标签然后"对自身算 kappa"
   ——数学上无意义。修复：按 (golden, verdict) 成对重采样，每次算真 kappa，
   2000 次取 2.5/97.5 分位。
3. **curl 传输层 gzip 未解压（传输层，memomind 侧先发现）**：转发 accept-encoding
   但 curl 不带 `--compressed` 时返回 gzip 原始字节 → JSON 解码 0x8b 报错。
   修复：curl 加 `--compressed`（三个项目的传输层适配器统一）。

## 4. 评测表（mock 与 real 严格分开）

| 口径 | 内容 | 数字 |
|---|---|---|
| mock 校准（历史，`selfeval.py`） | 规则 judge v1→v2 | kappa 0.51→0.8889（历史数字） |
| real 校准（历史，`real_judge_calibration.md`） | 真 judge v1/v2 裸跑 + 阈值扫描 | v1@4.0 两次 0.5455/0.4444；v2@4.0 0.8889（历史数字） |
| **real 波动（本次，`judge_variance.py`）** | v2 rubric 5 遍 | kappa ∈ [0.7917, 0.8889]，均值 0.8500，std 0.0532；两两一致性均值 0.9333；多数决 0.8889（CI [0.6154, 1.0]）；翻转 case 1/30（c-23） |
| **real 位置偏差（本次）** | 段序翻转 30 条 + pairwise A/B 10 对 | 段序：\|Δ\| 均值 0.17（满量程 3.4%），pass 翻转 2/30（都是 verbose 边缘）；pairwise：翻转一致率 9/10 |
| 花费（本次 real） | 230 次调用（judge 不透出 usage，按 ~1400in/150out 每次） | est **¥1.70**（上限 ¥2.5 未触） |

pytest：全量 **95 passed**（judge.py 零改动）。

## 5. 复现命令

```bash
cd agenteval
.venv/Scripts/python.exe -m pytest -q                          # 95 passed
.venv/Scripts/python.exe -m agenteval.selfeval                 # mock 自评测（离线）
.venv/Scripts/python.exe scripts/judge_variance.py             # 波动实验三阶段（需 key）
.venv/Scripts/python.exe scripts/judge_variance.py --recompute # 离线重算聚合指标
```

## 6. 已知边界

- 单 judge 单模型（deepseek-v4-flash-0731）；n=30 小标注集，单遍 kappa 的 bootstrap CI
  宽（[0.62, 1.0]）——结论以"分布与机制"呈现，不吹单点精度。
- 温度=0 不保证确定性（服务端），这正是实验对象而非缺陷。
- 成本按调用数 × 假设 token 量估算（judge 不透出 usage），非账单值。
- 波动实验的 5 遍打分是独立调用，未验证同进程会话级缓存对波动的影响。

---

# 7. CI 跨项目回归门禁集成（AE-2 会话 2026-10-29 追加）

## 做了什么

- **门禁真接两主项目 CI**：askbi `.github/workflows/ci.yml` 追加 `eval-gate` job（原 test
  job 逐字未动）；policy-compass 新建 ci.yml（此前无 CI，test job 按其本地口径）。
  job 内容：起主项目 mock 服务 → agenteval 打真 HTTP 跑 smoke → gate 对比基线 → 劣化 exit 1。
  全链路零 key；主项目侧零源码改动（yml 除外）。
- **smoke 数据集 v2.0.0（10→30×2）**：先全量探测主项目 v2 评测集（askbi 116 / PC 90 条，
  探测原始输出在 evidence/），再选例构建——askbi query17/drill4/clarify5/refusal4（含
  south/north 行级权限），PC fact11/compare4/calc4/refuse4/timeliness3/slang3/multi1
  （只取 main split）。30/30 双项目对活服务实测通过。
- **门禁有效性演示（任务③）**：`scripts/ci_fault_demo.py` 注入 8 条用例劣化（3 error +
  5 wrong-output，不碰主项目源码）→ 双项目 gate 全部 exit 1 拦截 → 移除注入（=修复）→
  gate exit 0 放行。全程记录 `evidence/ci_gate_demo.md` + 两个原始 transcript。
- 适配器增强：askbi per-case role 透传（评测集自带角色分层，构造参数兜底）。

## 关键决策

1. **expected = 基线输出快照**：mock 口径下答案确定，快照即金标——PC 的政策摘要有几十个
   数字，手写 expected 无法满足 MockJudge"输出数字必须都在 expected 中"的 faithfulness 规则；
   keywords 独立于快照人工挑（语义锚点），构建脚本逐条断言。
2. **CI 与基线同环境锁定**：askbi（mock provider / conservative 档 / gen_data seed 42）、
   PC（FORCE_MOCK=1 / EMBED、RERANK 显式关 / BM25-only ingest）——本地用临时库复刻 CI
   状态验证确定性（新旧库输出逐字节一致）后才出基线；任何一项环境变化都必须重出基线。
3. **门禁只钉稳定行为**：PC 已知 7 条 mock 弱检索用例不进 smoke——不把已知缺陷固化成金标。

## 三个真实 bug（集成消费才暴露，均已修复+回归测试）

1. `cli.py` cmd_run `TraceStore` 未导入 → `python -m agenteval run` 必然 NameError
   （门禁 CLI 入口此前从未被端到端跑通过；test 只直连 Runner 所以没抓到）。
2. `pyproject.toml` 缺 `[build-system]` 与包声明 → `pip install -e .` 失败（flat-layout
   多顶层目录自动发现报错）——平台此前根本装不进 CI。
3. 旧 smoke 10 条从未对活服务验证：真适配器打真 mock 服务仅 10% 通过
   （keywords 拍脑袋 + faithfulness 误杀）——v2 全量重建。

## 评测表（mock 与 real 严格分开）

| 口径 | 内容 | 数字 |
|---|---|---|
| mock（本次，双项目 smoke v2） | 30 条×2 打真 HTTP 服务 | askbi 100% / 4.707；PC 100% / 4.760；门禁基线入库 |
| mock（本次，故障注入演示） | 8/30 劣化 | judge_pass 100%→73.33%，gate exit 1；修复后与基线持平，exit 0 |
| pytest（本次） | agenteval 全量 | **104 passed**（95→104：+9 CI 集成回归，test_api 硬编码 10 条处改按数据集长度） |

红线：askbi 94→94 passed、PC 23→23 passed（改 CI 前后各一遍）+ PC 固定 CI 环境 23 passed，
全在 `evidence/pytest_*_after_ci_edit.txt`。

## 复现命令 / 边界

```bash
.venv/Scripts/python.exe -m pytest -q                     # 104 passed
bash scripts/ci_gate.sh askbi http://127.0.0.1:8801 baselines/askbi_smoke_baseline.json
.venv/Scripts/python.exe scripts/ci_fault_demo.py --adapter askbi \
  --base-url http://127.0.0.1:8801 --baseline baselines/askbi_smoke_baseline.json
```
- CI 真正生效前提：agenteval 推送到与主项目同 owner 的 GitHub（main 分支）——yml 已就位，
  push 动作留给用户。
- 门禁判的是 mock 口径下的行为回归（keywords + 数字漂移），语义等价的措辞变化会误报——
  那是 keyword judge 的已知边界，重出基线即可；真模型口径回归用主项目自己的 v2 评测。
- 主项目 CI 文件改动未提交（按分工留各自仓库主人确认）；agenteval 侧全部提交。
