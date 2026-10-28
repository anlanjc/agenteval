# TAKEOVER — agenteval 接手指南

> 目标读者：接手本项目的下一个开发者（或三个月后忘记细节的自己）。
> 原则：每个模块都按"白板重写清单"组织——如果不看代码，你能否在白板上把核心逻辑重新写出来？
> 写不出来，说明还没真正理解。

---

## 0. 五分钟读懂全局

```
评测 = for case in dataset: target(case) --trace--> judge.score → verdict
校准 = judge 决策 vs golden 人工标注 → accuracy / Cohen's kappa / 混淆矩阵 / 阈值扫描
门禁 = compare(baseline_report, current_report) → 任一指标劣化超阈值 → exit 1
```

- 数据流：`dataset.jsonl → Runner → RunReport(json) → gate / report / api`
- 观测流：`Runner → Tracer(span 树+事件) → TraceStore(SQLite runs/spans/events) → GET /spans/{run_id}`
- 一切离线可跑：`MockAgentTarget` + `MockJudge` + `MockSimulator`，pytest 71 个用例不碰网络。

## 1. 模块白板重写清单

### 1.1 trace.py（观测地基）
必须能白板写出的东西：
- 三个 dataclass 的字段：`Span(span_id, name, parent_span_id, started_at, ended_at, attributes, status, error)`、`Event(event_id, run_id, span_id, name, ts, attributes)`、`TraceRun(run_id, spans, events, ...)`。
- **Tracer 的实现核心**：`_stack` 保存打开的 span；`span()` 是 `@contextmanager`，
  进入时建 Span 压栈，异常时置 status=error 后 **re-raise**，finally 里闭合时间并弹栈。
  事件挂在栈顶（最内层 span）。
- SQLite 三表 runs/spans/events；`span_tree()` 用两遍循环（建 dict 节点 → 按 parent 挂 children），
  **只返回第一个无 parent 的节点为根**——所以 Runner 必须显式开一个根 span，否则多个 case
  span 都会成为"无 parent"，第二个开始的会从树里丢掉（这是我们实际踩过的坑）。
- token 估算：CJK ≈ 1 字/token，其他 ≈ 4 字符/token，只做量级观测。

### 1.2 judge.py（评分 + 校准，面试主战场）
- rubric prompt：system 要求**只输出严格 JSON** `{"correctness":1-5,"faithfulness":1-5,"format":1-5,"reasoning"}`；
  解析要容忍 markdown 围栏，分数越界/非法要抛 `JudgeError` 并重试一次。
- 维度加权 overall：correctness 0.5 / faithfulness 0.3 / format 0.2。**决策与解释分离**：
  分维度打分，加权汇总，pass = overall ≥ threshold。
- `OpenAICompatibleJudge`：base_url/key/model 全走环境变量
  （`AGENTEVAL_LLM_BASE_URL/AGENTEVAL_LLM_API_KEY/AGENTEVAL_LLM_MODEL`）；
  **缺 key 必须 fail-fast**（`score()` 第一行就 `require_credentials()`），否则会被
  重试逻辑吞掉变成含糊的"两次尝试均失败"。
- 校准四件套（重写时按此顺序）：
  1. `calibrate(goldens, predicted)` → accuracy + 混淆矩阵（行=golden 列=predicted）；
  2. `cohen_kappa(y1, y2) = (p_o - p_e)/(1 - p_e)`，边界约定：完全一致→1.0
     （含双方同投一类的退化）、p_e=1 且不一致→0.0、长度不等/空→ValueError；
  3. `evaluate_threshold(samples, t)` → 单个阈值下的 CalibrationReport；
  4. `sweep_thresholds` 在 1.0~5.0（步长 0.1）上网格扫描，**并列取最高阈值**（同等 kappa
     下偏好更严格），返回 (best_t, best_report, sweep 明细)。
- **MockJudge 的"偏严"旋钮**（校准叙事的物理载体）：
  `MockJudgeConfig.verbose_faithfulness`（v1=1.0：超长答案 faithfulness 封顶 1 分）
  与 `correctness_bands`（命中率分档）。v2 校准 = verbose_faithfulness 1→3 + 分档收紧。

### 1.3 gate.py（回归门禁）
- 核心：**方向感知**。`HIGHER_IS_BETTER = {success_rate, judge_pass_rate, judge_mean}`，
  其余（latency_p50/p95、error_count）为低好方向；劣化量 > 阈值才 FAIL（允许噪声）。
- 默认阈值 `DEFAULT_THRESHOLDS`：success_rate 0.02 / judge_pass_rate 0.05 / judge_mean 0.30 /
  latency_p95 2000ms / error_count 0；CLI `--threshold judge_mean=0.5` 按指标覆盖。
- 退出码约定：0=放行，1=拦截，2=报告读取失败（三种情况 CI 语义不同）。
- 面试要点：为什么 judge_pass_rate 阈值(0.05)比 success_rate(0.02)宽？
  judge 打分有随机性（真实 LLM judge 更明显），小波动不应拦截。

### 1.4 simulator.py（τ-bench 式模拟）
- 协议：`next_user_turn(history, agent_reply) -> str | None`，None=结束。
  这一个方法就是 τ-bench 的精髓：**模拟器知道隐藏政策（HiddenPolicy），被问到才透露**。
- `MockSimulator`：脚本化 + 可选 ending_marker（agent 回复包含即结束，模拟"问题解决"）+
  可选 policy（agent 回复命中 secret_facts 的 key 时，下一轮补充透露）。
- `run_simulation` 结束原因三态：`ending_marker` / `script_end` / `max_turns`，
  success = 最后一条 agent 回复包含 marker。
- `LLMSimulator`：provider 接口已留好，缺 key 抛 `MissingCredentialsError`。

### 1.5 其余模块（相对简单，能说清数据形状即可）
- dataset.py：JSONL 首行 `{"_version": ...}` 是版本号；`sample(n, seed)` 确定性抽样
  是回归可比的前提。
- runner.py：`_run_case` 的 span 结构（case → target.call + judge.score）；target 抛异常
  必须兜成 case 级 error（`success_rate` 反映的是"系统没崩"，judge 通过率才是"答得对"）。
- report.py：单文件 HTML = 内嵌 CSS + 失败行 `fail-row` 高亮；用户内容必须 `html.escape`。
- api.py：`AppContext` 隔离数据目录（测试注入 tmp_path）；报告三件套
  reports/{run_id}.{json,md,html} + runs.db 注册表。

## 2. 必做实验（按顺序，每个都有明确产出）

1. **judge 校准复现**：`python -m agenteval.selfeval`，读 `.agenteval/selfeval/calibration_report.md`。
   产出：亲手解释 kappa 0.51→0.89 的每一步（混淆矩阵哪格变了、为什么阈值扫描只到 0.76）。
2. **门禁拦截复现**：`python -m agenteval run`（baseline）→ 带 `--degrade-ratio 0.5` 再跑 →
   `python -m agenteval gate --baseline ... --current ...`，确认 exit code 1；
   再覆盖阈值 `--threshold judge_mean=2.0` 观察放行。产出：理解阈值的业务含义。
3. **接真实 LLM judge**：配 `AGENTEVAL_LLM_API_KEY` 后，把 selfeval 的 30 条 mock 输出
   交给 `OpenAICompatibleJudge` 重新打分，与 golden 算 kappa，对比 MockJudge 的 0.89。
   产出：真实数据点——"LLM judge 是否真比规则 judge 强"。
4. **接真项目端到端**：起 policy-compass 服务，`python -m agenteval run
   --dataset agenteval/datasets/policy_compass_smoke.jsonl --target http
   --base-url http://127.0.0.1:8001`，观察 /spans 里的上游 trace_id。
   产出：跨项目 trace 串联的真实证据。
5. **多轮模拟评测**：用 `MockSimulator + HiddenPolicy` 写一个 5 轮客服场景，
   验证 agent 在第 3 轮才问到关键隐藏信息（订单号/退款政策）。
   产出：理解 τ-bench 为什么比单轮 QA 更能暴露 Agent 缺陷。

## 3. 原创扩展方向（挑一个做深，别贪多）

1. **judge 一致性的置信区间**：kappa 是点估计，30 条样本的置信区间很宽。
   用 bootstrap 重采样算 kappa 的 95% CI，校准报告加一列"区间宽度"，
   顺便回答"多少条 golden 才够"（区间宽度 < 0.1 时的最小 n）。
2. **metric 级分片门禁（per-tag gate）**：当前 gate 只看整体指标；扩展成按
   dataset tags 分片对比（如 refusal 类 case 通过率单独设阈值），
   防止"整体达标但拒答能力悄悄退化"。
3. **trace 驱动的自动归因**：门禁 FAIL 时自动对比 baseline/current 的 span 树
   （哪个 case 变差、target.call 耗时分布漂移、error 事件聚类），
   在差异表下追加"Top 回归原因"。这把 gate 从"报警器"升级为"定位器"。

## 4. 工程注意事项

- Windows + Git Bash；装依赖必须 `-i https://pypi.org/simple/`（默认清华源 403）。
- 跑测试前 `tests/conftest.py` 会把 `AGENTEVAL_DATA_DIR` 指到临时目录，别删它。
- 不要 git init、不要动工作区其他项目（policy-compass / askbi / miniagent 只读）。
- `.agenteval/` 是运行产物目录，可随时删除重建（selfeval 会重新生成）。
- 校准数字的可复现性依赖两个固定 seed：`build_cases` 的固定用例表与
  `MockAgentTarget(seed=7/11)`——改数据前先跑 selfeval 确认叙事数字仍成立。
