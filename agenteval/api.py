"""FastAPI 观测服务：

- POST /runs                 触发一次评测（mock target 开箱即跑，也支持 http/adapter target）
- GET  /runs                 run 列表
- GET  /runs/{run_id}        run 详情（metrics + cases）
- GET  /runs/{run_id}/report 报告（?format=html|json|md，默认 md）
- GET  /spans/{run_id}       trace 查询（span 树 + 事件流）
- GET  /                     单文件 HTML 观测页（static/index.html）

数据落盘：{data_dir}/store.db（trace）、{data_dir}/runs.db（run 注册表）、
{data_dir}/reports/{run_id}.{json,md,html}。默认 .agenteval/，可用环境变量
AGENTEVAL_DATA_DIR 覆盖；测试用 create_app(data_dir=tmp) 注入独立目录。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .adapters import get_adapter
from .dataset import EvalDataset
from .judge import JudgeError, MockJudge, OpenAICompatibleJudge
from .mockagent import MockAgentTarget
from .report import render_html, render_markdown
from .runner import HTTPTarget, RunReport, Runner
from .trace import TraceStore

_BUILTIN_DATASET_DIR = Path(__file__).parent / "datasets"


class AppContext:
    """每个 app 实例的数据目录上下文（隔离测试与多实例部署）。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def trace_store(self) -> TraceStore:
        return TraceStore(self.root / "store.db")

    def registry(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.root / "runs.db")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """CREATE TABLE IF NOT EXISTS eval_runs (
            run_id TEXT PRIMARY KEY, name TEXT, dataset TEXT, dataset_version TEXT,
            target TEXT, judge TEXT, created_at REAL, metrics_json TEXT, report_json TEXT
        )""")
        conn.commit()
        return conn

    def save_report(self, report: RunReport) -> None:
        d = self.root / "reports"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{report.run_id}.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (d / f"{report.run_id}.md").write_text(render_markdown(report), encoding="utf-8")
        (d / f"{report.run_id}.html").write_text(render_html(report), encoding="utf-8")
        conn = self.registry()
        conn.execute(
            "INSERT OR REPLACE INTO eval_runs(run_id,name,dataset,dataset_version,target,judge,"
            "created_at,metrics_json,report_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (report.run_id, report.name, report.dataset, report.dataset_version,
             report.target, report.judge, report.created_at,
             json.dumps(report.metrics.to_dict(), ensure_ascii=False),
             json.dumps(report.to_dict(), ensure_ascii=False)),
        )
        conn.commit()
        conn.close()


def default_context() -> AppContext:
    return AppContext(os.environ.get("AGENTEVAL_DATA_DIR", ".agenteval"))


# ---------------------------------------------------------------- 请求模型


class TargetSpec(BaseModel):
    type: Literal["mock", "http", "adapter"] = "mock"
    base_url: str | None = None
    mode: Literal["raw", "openai"] = "raw"
    response_path: str = "answer"
    model: str = "default"
    adapter: str | None = None       # policy_compass | askbi
    degrade_ratio: float = 0.0       # 仅 mock：注入错误输出（门禁演示用）


class RunRequest(BaseModel):
    name: str = "api-run"
    dataset: str = "builtin:policy_compass_smoke"
    target: TargetSpec = Field(default_factory=TargetSpec)
    judge: Literal["mock", "openai_compatible"] = "mock"
    judge_threshold: float = 4.0
    limit: int | None = Field(default=None, ge=1)


_DATASET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# AGT-3：私有/环回目标放行开关（CI 与本地评测显式开启；API 默认拒绝内网目标）
_ALLOW_PRIVATE_TARGETS = os.environ.get("AGENTEVAL_ALLOW_PRIVATE_TARGETS") == "1"


def _validate_public_base_url(url: str) -> None:
    """AGT-3 修复（评审 2.0）：base_url 曾无任何校验——未鉴权 API 可驱动服务器外联
    内网（错误字符串差异构成 SSRF 探测预言机）。仅放行 http/https 且解析结果
    不落在环回/私网/链路本地/保留段；AGENTEVAL_ALLOW_PRIVATE_TARGETS=1 显式放行
    （CI/本地）。CLI 不经此校验。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=422, detail="base_url 必须为合法 http/https URL")
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise HTTPException(status_code=422, detail="base_url 主机无法解析")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            raise HTTPException(status_code=422,
                                detail="base_url 指向内网/保留地址，已拒绝（SSRF 防护）")


def resolve_dataset(spec: str, extra_roots: tuple[Path, ...] = ()) -> EvalDataset:
    """支持 builtin:policy_compass_smoke / builtin:askbi_smoke / JSONL 文件路径。

    AGT-2 修复（评审 2.0）：API 路径曾接受任意文件系统路径——任意 JSONL 内容可被
    外带到攻击者 target，且 404 detail 回显 spec 构成存在性探测。现限定：内置目录
    （名字白名单）+ extra_roots（app 数据根）resolve 后校验；CLI 传 allow-any 语义
    时直接传 expanded 路径并走 extra_roots 放行。
    """
    if spec.startswith("builtin:"):
        name = spec.split(":", 1)[1]
        if not _DATASET_NAME_RE.fullmatch(name):
            raise HTTPException(status_code=422, detail="内置数据集名仅允许字母数字与 _-")
        path = (_BUILTIN_DATASET_DIR / f"{name}.jsonl").resolve()
        if not path.is_relative_to(_BUILTIN_DATASET_DIR.resolve()):
            raise HTTPException(status_code=422, detail="非法数据集路径")
        if not path.exists():
            raise HTTPException(status_code=404, detail="内置数据集不存在")
        return EvalDataset.load_jsonl(path, name=name)
    p = Path(spec)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p = p.resolve()
    except OSError:
        raise HTTPException(status_code=422, detail="非法数据集路径")
    allowed_roots = [_BUILTIN_DATASET_DIR.resolve(), *(r.resolve() for r in extra_roots)]
    if not any(p.is_relative_to(r) for r in allowed_roots):
        raise HTTPException(status_code=422,
                            detail="数据集路径越界：API 仅允许内置目录与数据根下的文件")
    if not p.exists():
        raise HTTPException(status_code=404, detail="数据集文件不存在")
    return EvalDataset.load_jsonl(p)


def build_target(spec: TargetSpec):
    if spec.type == "mock":
        # degrade id 集在 create_run 里按实际数据集绑定，保证确定性
        return MockAgentTarget(degrade_ratio=spec.degrade_ratio)
    if spec.type == "http":
        if not spec.base_url:
            raise HTTPException(status_code=422, detail="http target 需要 base_url")
        return HTTPTarget(base_url=spec.base_url, mode=spec.mode,
                          model=spec.model, response_path=spec.response_path)
    if not spec.base_url:
        raise HTTPException(status_code=422, detail="adapter target 需要 base_url")
    return get_adapter(spec.adapter or "policy_compass", spec.base_url)


def build_judge(kind: str):
    if kind == "mock":
        return MockJudge()
    judge = OpenAICompatibleJudge()
    try:
        judge.require_credentials()
    except JudgeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return judge


# ---------------------------------------------------------------- app 工厂


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    ctx = AppContext(data_dir) if data_dir is not None else default_context()
    app = FastAPI(title="agenteval", version="0.1.0",
                  description="自研 Agent 评测与可观测平台")
    static_file = Path(__file__).parent / "static" / "index.html"

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(static_file.read_text(encoding="utf-8"))

    @app.post("/runs")
    def create_run(req: RunRequest) -> dict[str, Any]:
        # AGT-1 修复（评审 2.0）：曾为 async def 内同步跑完整场评测——事件循环被独占、
        # 全端点（含 /health）冻结、存活探针超时杀进程致结果丢失。改同步 def，
        # Starlette 自动调入线程池，评测期间其余端点照常服务。
        dataset = resolve_dataset(req.dataset, extra_roots=(ctx.root,))
        if req.limit:
            dataset = dataset.sample(req.limit)
        # AGT-3：非 mock 目标的 base_url 过 SSRF 防护（内网/保留地址拒绝）
        if (req.target.type != "mock" and req.target.base_url
                and not _ALLOW_PRIVATE_TARGETS):
            _validate_public_base_url(req.target.base_url)
        target = build_target(req.target)
        if isinstance(target, MockAgentTarget):
            target.bind_dataset(dataset.ids())
        judge = build_judge(req.judge)
        runner = Runner(target=target, judge=judge, store=ctx.trace_store(),
                        judge_threshold=req.judge_threshold)
        report = runner.run(dataset, name=req.name)
        ctx.save_report(report)
        return {"run_id": report.run_id, "metrics": report.metrics.to_dict()}

    @app.get("/runs")
    async def list_runs() -> list[dict[str, Any]]:
        conn = ctx.registry()
        rows = conn.execute(
            "SELECT run_id,name,dataset,dataset_version,target,judge,created_at,metrics_json "
            "FROM eval_runs ORDER BY created_at DESC").fetchall()
        conn.close()
        return [
            {**{k: r[k] for k in ("run_id", "name", "dataset", "dataset_version",
                                  "target", "judge", "created_at")},
             "metrics": json.loads(r["metrics_json"])}
            for r in rows
        ]

    def _load(run_id: str) -> RunReport:
        conn = ctx.registry()
        row = conn.execute("SELECT report_json FROM eval_runs WHERE run_id=?",
                           (run_id,)).fetchone()
        conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail=f"run 不存在: {run_id}")
        return RunReport.from_dict(json.loads(row["report_json"]))

    @app.get("/runs/{run_id}")
    async def run_detail(run_id: str) -> dict[str, Any]:
        return _load(run_id).to_dict()

    @app.get("/runs/{run_id}/report")
    async def run_report(run_id: str, format: str = "md") -> Any:
        report = _load(run_id)
        if format == "html":
            return HTMLResponse(render_html(report))
        if format == "json":
            return JSONResponse(report.to_dict())
        return PlainTextResponse(render_markdown(report), media_type="text/markdown")

    @app.get("/spans/{run_id}")
    async def spans(run_id: str) -> dict[str, Any]:
        store = ctx.trace_store()
        run = store.load_run(run_id)
        store.close()
        if run is None:
            raise HTTPException(status_code=404, detail=f"trace 不存在: {run_id}")
        return {
            "run_id": run.run_id,
            "name": run.name,
            "status": run.status,
            "duration_ms": round(run.duration_ms, 2),
            "total_tokens": run.total_tokens,
            "span_tree": run.span_tree(),
            "events": [e.to_dict() for e in run.events],
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "time": str(time.time())}

    return app


app = create_app()
