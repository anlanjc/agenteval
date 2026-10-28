"""金丝雀监控测试：触发节律、滑动窗口基线、劣化告警、恒定通过不告警。"""
from agenteval.canary import CanaryConfig, CanaryMonitor
from agenteval.dataset import EvalDataset
from agenteval.judge import JudgeVerdict
from agenteval.runner import CallableTarget, Runner


def make_monitor(tmp_path, cases, judge_results):
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
    mon = CanaryMonitor(runner, ds, CanaryConfig(every_k_calls=2, sample_size=3),
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
