"""api.py 测试（TestClient）：触发评测、run 列表/详情、报告端点、span 树、404。"""

import pytest
from fastapi.testclient import TestClient

from agenteval.api import create_app
from agenteval.api import resolve_dataset


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTEVAL_LLM_API_KEY", raising=False)
    app = create_app(data_dir=tmp_path / "data")
    return TestClient(app), tmp_path / "data"


def test_full_run_lifecycle(client):
    c, data_dir = client
    # 触发评测（内置数据集 + mock target/judge）
    resp = c.post("/runs", json={
        "name": "api-smoke", "dataset": "builtin:askbi_smoke",
        "target": {"type": "mock"}, "judge": "mock",
    })
    assert resp.status_code == 200
    body = resp.json()
    run_id = body["run_id"]
    assert body["metrics"]["case_count"] == len(resolve_dataset("builtin:askbi_smoke").cases)
    assert body["metrics"]["success_rate"] == 1.0

    # run 列表
    runs = c.get("/runs").json()
    assert any(r["run_id"] == run_id for r in runs)

    # run 详情（含 cases 与 verdict）
    detail = c.get(f"/runs/{run_id}").json()
    assert detail["name"] == "api-smoke"
    assert len(detail["cases"]) == len(resolve_dataset("builtin:askbi_smoke").cases)
    assert detail["cases"][0]["verdict"]["overall"] == 5.0

    # markdown 报告
    md = c.get(f"/runs/{run_id}/report")
    assert md.status_code == 200
    assert "# 评测报告：api-smoke" in md.text
    # html 报告
    html = c.get(f"/runs/{run_id}/report", params={"format": "html"})
    assert "<!DOCTYPE html>" in html.text
    # 报告文件已落盘
    assert (data_dir / "reports" / f"{run_id}.md").exists()

    # trace 查询：span 树
    spans = c.get(f"/spans/{run_id}").json()
    tree = spans["span_tree"]
    assert tree["name"] == "api-smoke"
    assert len(tree["children"]) == len(resolve_dataset("builtin:askbi_smoke").cases)
    case0 = tree["children"][0]
    assert {ch["name"] for ch in case0["children"]} == {"target.call", "judge.score"}
    assert spans["events"]


def test_api_degraded_mock_gate_scenario(client):
    """API 层也能复现门禁演示：degrade_ratio 注入错误 → judge 指标下降。"""
    c, _ = client
    good = c.post("/runs", json={"name": "g", "dataset": "builtin:policy_compass_smoke",
                                 "target": {"type": "mock"}}).json()
    bad = c.post("/runs", json={"name": "b", "dataset": "builtin:policy_compass_smoke",
                                "target": {"type": "mock", "degrade_ratio": 0.4}}).json()
    assert bad["metrics"]["judge_pass_rate"] < good["metrics"]["judge_pass_rate"]
    assert bad["metrics"]["judge_mean"] < good["metrics"]["judge_mean"]


def test_api_judge_openai_without_key_is_400(client):
    c, _ = client
    resp = c.post("/runs", json={"judge": "openai_compatible"})
    assert resp.status_code == 400
    assert "AGENTEVAL_LLM_API_KEY" in resp.json()["detail"]


def test_api_validation_and_404(client):
    c, data_dir = client
    assert c.get("/runs/does-not-exist").status_code == 404
    assert c.get("/spans/does-not-exist").status_code == 404
    assert c.post("/runs", json={"dataset": "builtin:nope"}).status_code == 404
    # AGT-2：API 数据集路径白名单——根外路径 422（曾 404 回显 spec 构成存在性探测）
    assert c.post("/runs", json={"dataset": "Z:/definitely/missing.jsonl"}).status_code == 422
    assert c.post("/runs", json={"dataset": "builtin:../nope"}).status_code == 422
    # 数据根内的缺失文件仍是 404（detail 不回显 spec）
    inside = (data_dir / "missing.jsonl").as_posix()
    assert c.post("/runs", json={"dataset": inside}).status_code == 404
    # http target 缺 base_url → 422
    assert c.post("/runs", json={"target": {"type": "http"}}).status_code == 422
    # AGT-3：SSRF 防护——内网/环回目标 422（显式开关可放行）
    assert c.post("/runs", json={"dataset": "builtin:askbi_smoke",
                                 "target": {"type": "http", "base_url": "http://127.0.0.1:9"}}).status_code == 422
    assert c.get("/health").json()["status"] == "ok"


def test_api_serves_static_page(client):
    c, _ = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert "agenteval 观测台" in resp.text
    assert "/runs" in resp.text
