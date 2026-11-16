"""金丝雀监控：线上常驻小评测，抓"模型静默变笨"（笔记 15 的实现）。

背景：LLM 供应商的静默劣化（不报错但质量降）是四种故障形态里最危险的——
报错能触发重试/切换，静默劣化只能靠持续小评测发现。

设计：
1. 金丝雀集：从正式评测集抽样 N 条（固定 seed 保证可比），带 golden 标注；
2. 每 K 次生产调用触发一次金丝雀运行（caller 只需在请求后调 canary_maybe()）；
3. 滑动窗口记录通过率，低于基线容差 → 告警回调；基线缺省为首窗均值
   （首窗满即冻结锚点，此后不随窗口吸收新值——AE-3），或显式设定；
4. 与 gate 同源的方向感知判定——**线上监控与离线门禁一套口径**。
"""
from __future__ import annotations

import random
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from .dataset import EvalDataset
from .runner import Runner


@dataclass
class CanaryConfig:
    sample_size: int = 5            # 每次金丝雀运行的用例数
    every_k_calls: int = 20         # 每 K 次生产调用触发一次
    window_size: int = 6            # 滑动窗口保留的最近运行数
    tolerance: float = 0.15         # 通过率相对基线下降超 15% → 告警
    baseline_pass_rate: float | None = None   # 缺省用首窗均值（首窗满后冻结——AE-3）
    seed: int = 42
    max_alerts: int = 100           # 状态内保留的最近告警条数（AE-9：常驻防无界增长）


@dataclass
class CanaryState:
    calls_since_run: int = 0
    window: deque = field(default_factory=deque)   # 每次运行的通过率
    alerts: list[dict[str, Any]] = field(default_factory=list)
    runs: int = 0
    baseline_anchor: float | None = None   # AE-3：首窗均值，满窗后冻结不再吸收新值
    alert_count: int = 0   # AE-9：累计告警数（alerts 封顶轮转后总量仍可查）


class CanaryMonitor:
    """把评测从"一次性活动"变成"常驻监控"——静默劣化的唯一可靠发现手段。"""

    def __init__(self, runner: Runner, dataset: EvalDataset,
                 config: CanaryConfig | None = None,
                 on_alert: Callable[[dict[str, Any]], None] | None = None):
        self.runner = runner
        cfg = config or CanaryConfig()
        self.config = cfg
        self.dataset = dataset.sample(cfg.sample_size, seed=cfg.seed)
        self.on_alert = on_alert
        self.state = CanaryState()
        self._lock = threading.Lock()   # AGT-8：计数/判定/状态变更原子化
        self._running = False           # AGT-8：singleflight 标记

    def canary_maybe(self) -> dict[str, Any] | None:
        """生产调用后调用：计数到 K 触发金丝雀运行，返回本次运行结果或 None。

        AGT-8 修复（评审 2.0）：计数 RMW 与 check-then-reset 曾全程无锁——并发下
        更新丢失（金丝雀晚触发/不触发）或双跑双告警（双倍 judge 花费）。计数/判定/
        重置在锁内原子完成，耗时的 runner.run 在锁外，singleflight 防重入。
        """
        with self._lock:
            self.state.calls_since_run += 1
            if self.state.calls_since_run < self.config.every_k_calls:
                return None
            if self._running:      # singleflight：上一场金丝雀仍在跑
                return None
            self.state.calls_since_run = 0
            self._running = True
        try:
            return self.run_canary()
        finally:
            with self._lock:
                self._running = False

    def run_canary(self) -> dict[str, Any]:
        report = self.runner.run(self.dataset, name=f"canary-{self.state.runs + 1}")
        with self._lock:  # AGT-8：状态变更原子化（慢调用已在锁外）
            self.state.runs += 1
            pass_rate = report.metrics.judge_pass_rate
            self.state.window.append(pass_rate)
            if len(self.state.window) > self.config.window_size:
                self.state.window.popleft()

            baseline = self.config.baseline_pass_rate
            if baseline is None:
                # AE-3 修复（评审 4.0）：旧实现为『窗口去掉当前值的尾随均值』——
                # 基线随窗口逐步吸收已劣化数据，持续劣化约 window_size 次后
                # 0.5 < baseline-0.15 不再成立，告警永久停止（模块在『持续劣化』
                # 这一核心场景下失明）。现首窗满时冻结基线锚点，此后不再吸收新值；
                # 锚点未冻结（前 window_size-1 窗）沿用去当前值的均值作临时基线，
                # 保持早期告警能力。显式 baseline_pass_rate 恒优先于锚点。
                if self.state.baseline_anchor is not None:
                    baseline = self.state.baseline_anchor
                elif len(self.state.window) >= self.config.window_size:
                    self.state.baseline_anchor = baseline = (
                        sum(self.state.window) / len(self.state.window))
                elif len(self.state.window) > 1:
                    baseline = sum(list(self.state.window)[:-1]) / (len(self.state.window) - 1)
            alert = None
            if baseline is not None and pass_rate < baseline - self.config.tolerance:
                alert = {
                    "kind": "canary_degradation",
                    "pass_rate": pass_rate, "baseline": baseline,
                    "window": list(self.state.window),
                    "run_id": report.run_id,
                }
                self.state.alerts.append(alert)
                # AE-9 修复（评审 4.0）：alerts 曾是无上限 list——canary 定位线上
                # 常驻监控，持续劣化期间每窗 append 一条含 window 快照的 alert 永不
                # 清理，驻留进程内存随时间无界增长。现封顶轮转只留最近 max_alerts 条
                # （window 有界 popleft 的同款思路），累计总量单独记 alert_count，
                # on_alert 回调通道不受影响（下游自行决定是否落盘全量）。
                self.state.alert_count += 1
                if len(self.state.alerts) > self.config.max_alerts:
                    del self.state.alerts[0]
                if self.on_alert:
                    self.on_alert(alert)
        return {"run_id": report.run_id, "pass_rate": pass_rate,
                "baseline": baseline, "alert": alert}

    def stats(self) -> dict[str, Any]:
        return {"runs": self.state.runs, "calls_since_run": self.state.calls_since_run,
                "window": list(self.state.window), "alerts": self.state.alert_count,
                "alerts_retained": len(self.state.alerts),
                "canary_size": len(self.dataset)}
