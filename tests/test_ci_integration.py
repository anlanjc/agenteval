"""CI 集成回归：门禁被两个主项目真实消费的路径（datasets v2 / CLI run / fault target / baselines）。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agenteval.dataset import EvalDataset
from agenteval.runner import TargetResult

HERE = Path(__file__).parent.parent
DATASETS = HERE / "agenteval" / "datasets"
BASELINES = HERE / "baselines"


# ---------------------------------------------------------------- 数据集 v2

@pytest.mark.parametrize("name", ["askbi_smoke", "policy_compass_smoke"])
def test_smoke_datasets_v2_shape(name):
    ds = EvalDataset.load_jsonl(DATASETS / f"{name}.jsonl")
    assert ds.version == "2.0.0"
    assert len(ds) == 30
    ids = ds.ids()
    assert len(set(ids)) == 30
    for case in ds.cases:
        # expected=基线输出快照（faithfulness 的数字核对依据），必须非空
        assert case.expected.strip()
        # keywords 是门禁语义锚点：必须全部出现在基线快照里（构建时断言，此处防回归）
        kws = case.metadata.get("keywords") or []
        assert kws, f"{case.id} 缺 keywords"
        missing = [k for k in kws if k not in case.expected]
        assert not missing, f"{case.id} keywords 不在 expected 中: {missing}"
        assert "smoke" in case.tags and "source_id" in case.metadata


def test_smoke_datasets_stratified():
    ab = EvalDataset.load_jsonl(DATASETS / "askbi_smoke.jsonl")
    kinds = [c.tags[1] for c in ab.cases]
    assert kinds.count("query") == 17 and kinds.count("drill") == 4
    assert kinds.count("clarify") == 5 and kinds.count("refusal") == 4
    roles = {c.metadata.get("role") for c in ab.cases}
    assert {"boss", "south", "north"} <= roles          # 行级权限代表性
    pc = EvalDataset.load_jsonl(DATASETS / "policy_compass_smoke.jsonl")
    types = [c.tags[1] for c in pc.cases]
    assert set(types) == {"fact", "compare", "calc", "refuse", "timeliness", "slang", "multi"}
    assert types.count("fact") == 11 and types.count("refuse") == 4


# ---------------------------------------------------------------- CLI run 回归
# cli.cmd_run 曾因 TraceStore 未导入而 NameError——CLI 是 CI 门禁的实际入口，钉死


def test_cli_run_produces_report(tmp_path, monkeypatch):
    out = tmp_path / "reports"
    db = tmp_path / "store.db"
    rc = pytest.importorskip("agenteval.cli").main([
        "run", "--dataset", "builtin:askbi_smoke", "--limit", "3",
        "--target", "mock", "--judge", "mock",
        "--db", str(db), "--out", str(out), "--name", "ci-regression",
    ])
    assert rc == 0
    files = list(out.glob("*.json"))
    assert files, "CLI run 未产出报告 JSON"
    report = json.loads(files[0].read_text(encoding="utf-8"))
    assert report["metrics"]["case_count"] == 3


# ---------------------------------------------------------------- baselines 自洽

@pytest.mark.parametrize("adapter", ["askbi", "policy_compass"])
def test_baselines_gate_against_themselves(tmp_path, adapter):
    from agenteval import gate as gate_mod

    baseline = BASELINES / f"{adapter}_smoke_baseline.json"
    assert baseline.exists()
    data = json.loads(baseline.read_text(encoding="utf-8"))
    assert data["metrics"]["judge_pass_rate"] == 1.0 and data["metrics"]["error_count"] == 0
    # 门禁自检：基线 vs 自身必须 PASS（exit 0）——时延阈值放大与 ci_gate.sh 同口径
    rc = gate_mod.gate(baseline, baseline,
                       {"latency_p50_ms": 1000000.0, "latency_p95_ms": 1000000.0},
                       stream=tmp_path / "out.txt")
    assert rc == 0


# ---------------------------------------------------------------- fault target

def _load_fault_demo():
    spec = importlib.util.spec_from_file_location(
        "ci_fault_demo", HERE / "scripts" / "ci_fault_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fault_target_passthrough_and_injection():
    from agenteval.dataset import EvalCase

    mod = _load_fault_demo()

    def inner(case):
        return TargetResult(output=f"answer:{case.id}")

    target = mod.FaultTarget(inner, {"ab-004": "error", "ab-007": "wrong"})
    ok = target(EvalCase(id="ab-001", input="q"))
    assert ok.output == "answer:ab-001" and ok.error is None      # 透传
    err = target(EvalCase(id="ab-004", input="q"))
    assert err.error and "fault-inject" in err.error              # 服务错误
    bad = target(EvalCase(id="ab-007", input="q"))
    assert bad.output == mod.WRONG_OUTPUT and bad.error is None   # 劣化输出


def test_fault_defaults_cover_8_cases():
    mod = _load_fault_demo()
    for adapter, faults in mod.DEFAULT_FAULTS.items():
        assert len(faults) == 8
        assert list(faults.values()).count("error") == 3
        assert list(faults.values()).count("wrong") == 5


def test_faulted_run_fails_gate(tmp_path):
    """8 条劣化后 Runner+MockJudge+gate 全链路必须给出 FAIL（与本地演示同路径）。"""
    from agenteval import gate as gate_mod
    from agenteval.judge import MockJudge
    from agenteval.runner import Runner

    mod = _load_fault_demo()
    ds = EvalDataset.load_jsonl(DATASETS / "askbi_smoke.jsonl")
    faults = mod.DEFAULT_FAULTS["askbi"]
    runner = Runner(target=mod.FaultTarget(_FakeAdapter(), faults), judge=MockJudge())
    report = runner.run(ds, name="faulted")
    path = tmp_path / "faulted.json"
    path.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")
    assert report.metrics.error_count == 3
    assert report.metrics.judge_pass_rate == pytest.approx(22 / 30, abs=1e-6)
    rc = gate_mod.gate(BASELINES / "askbi_smoke_baseline.json", path,
                       {"latency_p50_ms": 1000000.0, "latency_p95_ms": 1000000.0},
                       stream=tmp_path / "out.txt")
    assert rc == 1


class _FakeAdapter:
    """离线假适配器：用数据集 expected 快照当答案（不需要起服务）。"""

    name = "fake-askbi"

    def __init__(self):
        self.ds = EvalDataset.load_jsonl(DATASETS / "askbi_smoke.jsonl")
        self.by_id = {c.id: c for c in self.ds.cases}

    def __call__(self, case) -> TargetResult:
        return TargetResult(output=self.by_id[case.id].expected)
