# CI 门禁修后复检：policy-compass（2026-11-09，S9 Wave2.5 前置）

## 结论：PASS ✅

对 policy-compass 今日 HEAD（c9272dd：含热更新闭环 + 检索出口注入隔离 + faithfulness 校验器三项新改动）本地复刻 CI eval-gate 全流程（ingest → mock 服务 → 30 条 smoke → 对比基线）：

| metric | baseline | current | delta | 结果 |
|---|---|---|---|---|
| success_rate | 1.0 | 1.0 | 0 | ok |
| judge_pass_rate | 1.0 | 1.0 | 0 | ok |
| judge_mean | 4.76 | 4.72 | -0.04 | ok（阈 0.3） |
| error_count | 0 | 0 | 0 | ok |
| latency P50/P95 | 778.5/838.1 | 807.5/835.1 | ±30ms | ok |

**意义**：检索出口新增注入隔离层未改变正常查询行为（judge_mean -0.04 在噪声内）——"门禁拦得住劣化、不误伤重构"的活体证据。

## 过程中抓到的运维真坑（门禁敏感性活体证据）

首跑 30/30 全 error（405 Method Not Allowed）——**8000 端口被一个已死演示会话的僵尸服务占用**：PC 服务器 Errno 10048 绑定失败，而"health 检查通过"是僵尸答的，门禁把 30 发全打在了错误目标上。处置：换专用端口（APP_PORT=8765）复跑即 PASS。教训入册：**门禁前置健康检查只验 200 不验"目标身份"，端口必须专用或先验服务标识**；僵尸服务随会话退出后 8000 已自然清空。
