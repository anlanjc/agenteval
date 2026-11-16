"""金丝雀监控测试：触发节律、滑动窗口基线、劣化告警、恒定通过不告警。"""
import pytest

from agenteval.canary import CanaryConfig, CanaryMonitor
from agenteval.dataset import EvalDataset
from agenteval.judge import JudgeVerdict
from agenteval.runner import CallableTarget, Runner


def make_monitor(tmp_path, cases, judge_results, **cfg_overrides):
    """构造金丝雀：judge_results 是每次运行对每个 case 的 pass 序列（弹队）。"""
    import json as _json
    ds_file = tmp_path / "canary.jsonl"
    ds_file.write_text(
        "\n".join(_json.dumps({"id": f"c{i}", "input": f"问题{i}", "expected": "x",
                               "golden": "pass"})
                  for i in range(cases)) + "\n", encoding="utf-8")
    ds = EvalDataset.load_jsonl(ds_file)

    class SeqJudge:
        """按预设序列逐案打分的确定性 judge（对齐 JudgeProvider 协议）。"""
        name = "seq"

        def __init__(self, seq):
            self.seq = list(seq)

        def score(self, case, output, threshold=4.0):
            verdict = self.seq.pop(0) if self.seq else "fail"
            ok = verdict == "pass"
            return JudgeVerdict(case_id=case.id,
                                scores={"correctness": 5.0 if ok else 1.0},
                                overall=5.0 if ok else 1.0, passed=ok,
                                threshold=threshold)

    runner = Runner(target=CallableTarget(lambda case: case.input, name="mock-target"),
                    judge=SeqJudge(judge_results))
    alerts = []
    mon = CanaryMonitor(runner, ds,
                        CanaryConfig(every_k_calls=2, sample_size=3, **cfg_overrides),
                        on_alert=alerts.append)
    return mon, alerts


def _drive(mon, times):
    out = []
    for _ in range(times):
        r = mon.canary_maybe()
        if r:
            out.append(r)
    return out


def test_no_trigger_before_k(tmp_path):
    mon, _ = make_monitor(tmp_path, 3, ["pass", "pass", "pass"])
    assert _drive(mon, 1) == []            # 1 < K=2 不触发


def test_triggers_every_k_and_records_window(tmp_path):
    mon, _ = make_monitor(tmp_path, 3, ["pass"] * 12)
    runs = _drive(mon, 6)                  # 6 次调用 → 3 次金丝雀
    assert len(runs) == 3
    assert mon.stats()["runs"] == 3
    assert all(r["pass_rate"] == 1.0 for r in runs)


def test_degradation_alerts(tmp_path):
    # 前 2 窗全过 → 基线 1.0；第 3 窗掉到 1/3 → 告警
    mon, alerts = make_monitor(tmp_path, 3,
                               ["pass", "pass", "pass", "pass", "pass", "pass",
                                "pass", "fail", "fail"])
    runs = _drive(mon, 8)                  # 4 次金丝雀（最后不足 2 次？8/2=4）
    assert len(alerts) >= 1
    assert alerts[0]["kind"] == "canary_degradation"
    assert alerts[0]["pass_rate"] < alerts[0]["baseline"]


def test_stable_pass_no_alert(tmp_path):
    mon, alerts = make_monitor(tmp_path, 3, ["pass"] * 12)
    _drive(mon, 8)
    assert alerts == []


def test_explicit_baseline(tmp_path):
    mon, alerts = make_monitor(tmp_path, 3,
                               ["pass", "fail", "fail", "pass", "fail", "fail"])
    mon.config.baseline_pass_rate = 0.95   # 显式基线：第一窗 1/3 即应告警
    runs = _drive(mon, 4)
    assert len(alerts) >= 1


def test_sustained_degradation_keeps_alerting(tmp_path):
    """AE-3 回归（评审 4.0）：基线曾实现为『窗口去掉当前值的尾随均值』——随窗口
    吸收已劣化数据，持续劣化约 window_size 次后 0.5 < baseline-0.15 不再成立，
    告警永久停止（模块在『持续劣化』这一核心场景下失明）。修复后首窗满即冻结
    基线锚点，持续劣化第 10+ 次仍告警。"""
    # 评审场景复刻：前 5 次运行全过（1.0），随后 20 次持续劣化（每次 3 case 全 fail
    # → 0.0），window_size=6、tolerance=0.15 均默认
    mon, alerts = make_monitor(tmp_path, 3, ["pass"] * 15 + ["fail"] * 60)
    runs = _drive(mon, 50)                 # 25 次金丝雀运行
    assert len(runs) == 25
    # 首窗（第 6 次）满冻结基线 = (5×1.0 + 0.0)/6，此后每次运行都告警
    assert len(alerts) == 20
    frozen = 5.0 / 6
    assert all(a["baseline"] == pytest.approx(frozen) for a in alerts)
    # 第 10 次及以后的运行仍告警（旧实现第 10 次起永久静默）
    alert_ids = {a["run_id"] for a in alerts}
    assert all(r["run_id"] in alert_ids for r in runs[9:])


# ---------------------------------------------------------- AE-9（评审 4.0）

def test_alerts_capped_under_sustained_degradation(tmp_path):
    """AE-9 回归：alerts 曾是无上限 list——常驻监控持续劣化期间每窗 append 一条
    含 window 快照的 alert 永不清理，驻留进程内存无界。现保留最近 N 条 + 累计
    计数单独记账（stats 口径不丢总量）；on_alert 回调通道不受影响（AE-3 场景照旧）。"""
    mon, alerts = make_monitor(tmp_path, 3, ["pass"] * 15 + ["fail"] * 60,
                               max_alerts=3)
    runs = _drive(mon, 50)                 # 25 次运行 → 20 次告警
    assert len(alerts) == 20               # 回调侧仍收到全部告警（与 AE-3 回归一致）
    assert len(mon.state.alerts) == 3      # 状态侧封顶：只保留最近 3 条
    assert mon.state.alert_count == 20     # 累计计数不丢
    retained_ids = [a["run_id"] for a in mon.state.alerts]
    assert retained_ids == [a["run_id"] for a in alerts[-3:]]   # 留的是最新 3 条
    stats = mon.stats()
    assert stats["alerts"] == 20 and stats["alerts_retained"] == 3
    # AE-3 协同：冻结锚点不受轮转影响
    assert all(a["baseline"] == pytest.approx(5.0 / 6) for a in mon.state.alerts)
