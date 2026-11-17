# agentgate-go 门禁演练记录（[1B4-SM] AE-12，2026-11-18）

> 一句话：agentgate-go 网关作为 agenteval 的第三个评测目标接入——30 条冒烟（builtin:gateway_smoke）
> 走真 HTTP（网关→echo mock 上游），出基线 `baselines/gateway_smoke_baseline.json`，
> gate CLI 对账 PASS / 劣化注入 FAIL / 真网关级故障 FAIL / 恢复 PASS 全链路演练自洽。

## 1. 口径（诚实声明）

- **mock upstream / 本机单进程**：agentgate-go（`D:\1\1\1Tasks\zcode\agentgate-go\agentgate-go.exe`）
  + echo mock 上游（`scripts/gateway_mock_upstream.py`，回显"结论：…"模板）+ 便携 redis
  （`.tooling/agentgate-redis`，端口 6392）全部同机；REDIS 准入开启、LEDGER/M3 响应缓存
  默认关；时延不是门禁信号（本机口径噪声大，`ci_gate.sh` 惯例放大阈值）。
- judge=MockJudge（确定性规则打分）；数据集问题全部无数字 → 忠实度不触发捏造扣分。
- vk：`ag-smoke`（budget/tpm 不限额、rpm 10 万、并发槽 8）——适配器默认 api_key 与 CLI
  `get_adapter(args.adapter, args.base_url)` 无 kwargs 通道的现实对齐：seed 这个 key。
- 演练记录为终端输出摘录（纯文本时间线），无浏览器截图——同 demo_m2 诚实标注惯例。

## 2. 拉起与预置（端口 6392/9302/8092，演练后已全部清杀、端口核净）

```bash
.tooling/agentgate-redis/redis-server.exe --port 6392 --save "" --appendonly no
python scripts/gateway_mock_upstream.py --port 9302          # /healthz {"ok":true}
cd agentgate-go && REDIS_ADDR=127.0.0.1:6392 UPSTREAM_1_URL=http://127.0.0.1:9302 \
  UPSTREAM_1_KEY=sk-smoke ADMIN_TOKEN=smoke-admin PORT=8092 ./agentgate-go.exe
#   log: redis auth+admission enabled (127.0.0.1:6392) / listening on :8092 (channels=1)
# vk 预置（sha256("ag-smoke") → vk:<hash>）：
redis-cli -p 6392 SET "vk:<hash>" '{"id":"vk-gw-smoke","tenant_id":"t-gw-smoke","model_whitelist":null,
  "rpm_limit":100000,"tpm_limit":null,"budget_tokens":null,"concurrency_limit":8,"is_active":true}'
```

直连冒烟（httpx，UTF-8）：`POST /v1/chat/completions` → 200，回显正文 +
`usage.total_tokens=126`（账面驱动配额计量）。**排错实录**：用 Git Bash curl 直发中文
body 是 GBK 编码 → 网关转发原字节 → 上游 JSON 解码 500 → 通道进入 60s 冷却（5xx 口径
含网络错误）；httpx 发 UTF-8 即通——评测链路全程无此问题。

## 3. 基线与 gate PASS

`python -m agenteval run --dataset builtin:gateway_smoke --target http --adapter agentgate_go
--base-url http://127.0.0.1:8092 --judge mock`（30 条，gateway_smoke v2.0.0）：

```
cases: 30  errors: 0  success_rate: 100.00%  judge_pass_rate: 100.00%  judge_mean: 5.000
```

健康复跑 → `python -m agenteval gate --baseline baselines/gateway_smoke_baseline.json
--current <report>`：六指标全 ok（success/judge_pass/judge_mean delta=0），**gate exit=0**。

## 4. 劣化注入 FAIL（复用 ci_fault_demo.py 的 FaultTarget 手法）

`python scripts/ci_fault_demo.py --adapter agentgate_go --base-url http://127.0.0.1:8092
--baseline baselines/gateway_smoke_baseline.json --dataset builtin:gateway_smoke`
（注入 8 条：gw-004~006 error + gw-007~011 劣化输出）：

```
阶段一：run: cases=30 errors=3 success=90.00% judge_pass=73.33% judge_mean=4.556
  gate: success_rate -0.10 FAIL / judge_pass_rate -0.2667 FAIL / judge_mean -0.444 FAIL
        / error_count +3 FAIL → 门禁结论 FAIL ❌，gate 退出码 = 1（拦截合入）
阶段二：移除注入复跑 → 30/0/100%/100%/5.000 → gate exit=0（放行）
演示结论：门禁对 8 条劣化全部拦截(FAIL)，修复后放行(PASS)，demo exit=0（自洽）
```

## 5. 真网关级故障 FAIL（补充维度：杀 echo 上游进程）

上游进程停止后复跑 30 条 → `cases: 30 errors: 30 success_rate: 0.00%` →
gate：error_count +30 FAIL → **gate exit=1**。恢复上游后**未能立即转绿**——
agentgate-go 通道冷却 60s（`internal/channel/pool.go` Cool5xx，网络错误按 5xx 口径）
且**每次失败请求都会刷新冷却窗口**，连跑期间通道持续冷却；静默 65s 后单次复跑：
`30/0/100%/100%/5.000` → gate exit=0。**诚实注记**：这不是缺陷而是熔断设计——
对门禁演练的操作性启示是"网关级故障恢复后要等冷却窗口过完再对账"，已回填 ci_integration.md。

## 6. 覆盖面与边界

- 适配器契约测试（5 项，MockTransport 全 mock）见 AE-11 提交；本文件只记真链路演练。
- 未覆盖：M3 响应缓存命中路径（RESPONSE_CACHE_ENABLED 默认关，x-cache 头缺席时适配器
  meta=None——契约测试已钉）、429/预算路径（演练 vk 不限额）、流式请求（适配器恒
  stream=false）。留真实网关窗口。
