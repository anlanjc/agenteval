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


# ---------------------------------------------------------- AE-6（评审 4.0）

@pytest.fixture()
def client_no_raise(tmp_path, monkeypatch):
    # 异常路径回归用：默认 TestClient 会把服务端异常重抛给测试（raise_server_exceptions=True），
    # 这里要断言的是「异常 → 500 响应且连接已关」
    monkeypatch.delenv("AGENTEVAL_LLM_API_KEY", raising=False)
    app = create_app(data_dir=tmp_path / "data")
    return TestClient(app, raise_server_exceptions=False), tmp_path / "data"


def _spy_trace_store_close(monkeypatch):
    """给 TraceStore.close 挂计数器（TraceStore 是普通 Python 类，可 setattr）。"""
    from agenteval.trace import TraceStore
    closes = {"n": 0}
    orig = TraceStore.close

    def spy(self):
        closes["n"] += 1
        orig(self)

    monkeypatch.setattr(TraceStore, "close", spy)
    return closes


def _spy_registry_close(monkeypatch, fail_execute=False):
    """给 registry() 返回的连接挂 close 计数器（sqlite3.Connection 不允许 setattr，
    用 SimpleNamespace 代理端点实际用到的 execute/close 两成员）。"""
    import sqlite3
    import types

    from agenteval.api import AppContext
    closes = {"n": 0}
    orig = AppContext.registry

    def spy(self):
        conn = orig(self)
        if fail_execute:
            def execute(*a, **k):
                raise sqlite3.OperationalError("database is locked")
        else:
            execute = conn.execute

        def close():
            closes["n"] += 1
            conn.close()

        return types.SimpleNamespace(execute=execute, close=close)

    monkeypatch.setattr(AppContext, "registry", spy)
    return closes


_RUN_BODY = {"name": "ae6", "dataset": "builtin:askbi_smoke",
             "target": {"type": "mock"}, "judge": "mock"}


def test_create_run_closes_trace_store(client, monkeypatch):
    c, _ = client
    closes = _spy_trace_store_close(monkeypatch)
    resp = c.post("/runs", json=_RUN_BODY)
    assert resp.status_code == 200
    assert closes["n"] == 1                       # 每请求新建的 TraceStore 必须显式关闭


def test_create_run_closes_trace_store_on_runner_failure(client_no_raise, monkeypatch):
    # runner.run 异常路径连接也不滞留（GC 托付在 Windows 即 store.db 句柄滞留）
    c, _ = client_no_raise
    from agenteval.runner import Runner
    closes = _spy_trace_store_close(monkeypatch)
    monkeypatch.setattr(Runner, "run",
                        lambda self, ds, name="eval": (_ for _ in ()).throw(RuntimeError("boom")))
    resp = c.post("/runs", json=_RUN_BODY)
    assert resp.status_code == 500
    assert closes["n"] == 1


def test_spans_closes_store_on_load_error(client_no_raise, monkeypatch):
    c, _ = client_no_raise
    from agenteval.trace import TraceStore
    closes = _spy_trace_store_close(monkeypatch)
    monkeypatch.setattr(TraceStore, "load_run",
                        lambda self, run_id: (_ for _ in ()).throw(RuntimeError("boom")))
    resp = c.get("/spans/no-such-run")
    assert resp.status_code == 500
    assert closes["n"] == 1                       # 裸 close 不在 try/finally 曾滞留


def test_list_runs_closes_registry_on_execute_error(client_no_raise, monkeypatch):
    c, _ = client_no_raise
    closes = _spy_registry_close(monkeypatch, fail_execute=True)
    resp = c.get("/runs")
    assert resp.status_code == 500
    assert closes["n"] == 1                       # sqlite busy 等异常路径连接不滞留


def test_run_detail_closes_registry_on_parse_error(client_no_raise, monkeypatch):
    c, _ = client_no_raise
    from agenteval.report import RunReport
    resp = c.post("/runs", json=_RUN_BODY)
    run_id = resp.json()["run_id"]
    closes = _spy_registry_close(monkeypatch)
    monkeypatch.setattr(RunReport, "from_dict",
                        classmethod(lambda cls, d: (_ for _ in ()).throw(RuntimeError("boom"))))
    resp = c.get(f"/runs/{run_id}")
    assert resp.status_code == 500
    assert closes["n"] == 1
