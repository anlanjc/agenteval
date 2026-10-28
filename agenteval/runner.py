"""评测执行器：target（callable / HTTP endpoint）+ judge → trace + metrics。

- Target 抽象：任意 callable(case) -> TargetResult，或 HTTPTarget（openai 兼容 /v1/chat/completions
  或普通 POST /chat），或 adapters 里的项目专用适配器。
- 每个 case 一个 span 树：eval_run -> case[c{id}] -> target.call + judge.score，事件流记录请求/响应。
- metrics：成功率（无 error 且 judge 通过率另算）、judge 均分、时延 P50/P95。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx

from .dataset import EvalCase, EvalDataset
from .judge import JudgeProvider, JudgeError, MockJudge
from .trace import Tracer, TraceStore, estimate_tokens


# ---------------------------------------------------------------- Target

@dataclass
class TargetResult:
    """target 的归一化输出。"""

    output: str
    meta: dict[str, Any] = field(default_factory=dict)   # 适配器附带信息（sources/refusal 等）
    error: str | None = None
    latency_ms: float = 0.0


class Target(Protocol):
    name: str

    def __call__(self, case: EvalCase) -> TargetResult: ...


class CallableTarget:
    """把任意 callable 包装成 target。"""

    def __init__(self, fn: Callable[[EvalCase], Any], name: str = "callable"):
        self.fn = fn
        self.name = name

    def __call__(self, case: EvalCase) -> TargetResult:
        started = time.perf_counter()
        result = self.fn(case)
        latency = (time.perf_counter() - started) * 1000.0
        if isinstance(result, TargetResult):
            result.latency_ms = result.latency_ms or latency
            return result
        if isinstance(result, str):
            return TargetResult(output=result, latency_ms=latency)
        return TargetResult(output=json.dumps(result, ensure_ascii=False), latency_ms=latency)


def _pick(d: dict[str, Any], path: str) -> str:
    """按 a.b.0.c 取嵌套字段（数字段为数组下标）；取不到抛 KeyError。"""
    cur: Any = d
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                raise KeyError(f"数组下标非法: {part!r} in path {path!r}")
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(f"响应中找不到字段 {path!r}，实际键: {list(cur)[:8] if isinstance(cur, dict) else type(cur)}")
        cur = cur[part]
    return str(cur)


class HTTPTarget:
    """HTTP target，两种模式：

    - mode="openai": POST {base}/v1/chat/completions，body {model, messages}
    - mode="raw":    POST {base}/chat，body {question, ...extra_json}
    response_path 指定从响应 JSON 提取答案的字段路径（如 "answer" / "choices.0.message.content"）。
    transport 参数仅供测试注入 httpx.MockTransport / ASGITransport。
    """

    def __init__(self, base_url: str, mode: str = "raw", model: str = "default",
                 response_path: str = "answer", extra_json: dict[str, Any] | None = None,
                 timeout: float = 60.0, name: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.model = model
        self.response_path = response_path
        self.extra_json = dict(extra_json or {})
        self.timeout = timeout
        self.name = name or f"http:{mode}"
        self._transport = transport

    def _url(self) -> str:
        if self.mode == "openai":
            return f"{self.base_url}/v1/chat/completions"
        return f"{self.base_url}/chat"

    def _payload(self, case: EvalCase) -> dict[str, Any]:
        if self.mode == "openai":
            return {
                "model": self.model,
                "messages": [{"role": "user", "content": case.input}],
            }
        return {"question": case.input, **self.extra_json}

    def _extract(self, data: dict[str, Any]) -> str:
        if self.mode == "openai" and self.response_path == "answer":
            return _pick(data, "choices.0.message.content")
        return _pick(data, self.response_path)

    def __call__(self, case: EvalCase) -> TargetResult:
        started = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            with httpx.Client(timeout=self.timeout, **kwargs) as client:
                resp = client.post(self._url(), json=self._payload(case))
                resp.raise_for_status()
                data = resp.json()
            output = self._extract(data)
            latency = (time.perf_counter() - started) * 1000.0
            return TargetResult(output=output, meta={"raw_keys": list(data)}, latency_ms=latency)
        except KeyError as exc:
            return TargetResult(output="", error=f"响应字段缺失: {exc}",
                                latency_ms=(time.perf_counter() - started) * 1000.0)
        except Exception as exc:
            return TargetResult(output="", error=f"{type(exc).__name__}: {exc}",
                                latency_ms=(time.perf_counter() - started) * 1000.0)


# ---------------------------------------------------------------- Metrics / CaseResult

def percentile(values: list[float], p: float) -> float:
    """线性插值百分位（p in [0, 100]）。空列表返回 0。"""
    if not values:
        return 0.0
    vs = sorted(values)
    if len(vs) == 1:
        return vs[0]
    k = (len(vs) - 1) * (p / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(vs) - 1)
    frac = k - lo
    return vs[lo] * (1 - frac) + vs[hi] * frac


@dataclass
class CaseResult:
    case_id: str
    input: str
    output: str
    expected: str
    error: str | None = None
    latency_ms: float = 0.0
    verdict: dict[str, Any] | None = None   # JudgeVerdict.to_dict()
    passed: bool = False                    # judge 通过（无 judge 时 = 无 error）
    golden: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "input": self.input, "output": self.output,
            "expected": self.expected, "error": self.error,
            "latency_ms": round(self.latency_ms, 2),
            "verdict": self.verdict, "passed": self.passed,
            "golden": self.golden, "meta": self.meta,
        }


@dataclass
class RunMetrics:
    case_count: int
    error_count: int
    success_rate: float           # 无 error 占比
    judge_pass_rate: float        # judge passed 占比（无 judge 时 = success_rate）
    judge_mean: float             # judge overall 均分（1-5）
    latency_p50_ms: float
    latency_p95_ms: float
    total_tokens: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "case_count": self.case_count,
            "error_count": self.error_count,
            "success_rate": round(self.success_rate, 4),
            "judge_pass_rate": round(self.judge_pass_rate, 4),
            "judge_mean": round(self.judge_mean, 4),
            "latency_p50_ms": round(self.latency_p50_ms, 2),
            "latency_p95_ms": round(self.latency_p95_ms, 2),
            "total_tokens": self.total_tokens,
        }


@dataclass
class RunReport:
    run_id: str
    name: str
    dataset: str
    dataset_version: str
    target: str
    judge: str
    created_at: float
    metrics: RunMetrics
    cases: list[CaseResult]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "name": self.name,
            "dataset": self.dataset, "dataset_version": self.dataset_version,
            "target": self.target, "judge": self.judge, "created_at": self.created_at,
            "metrics": self.metrics.to_dict(),
            "cases": [c.to_dict() for c in self.cases],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunReport":
        m = d["metrics"]
        metrics = RunMetrics(
            case_count=m["case_count"], error_count=m["error_count"],
            success_rate=m["success_rate"], judge_pass_rate=m["judge_pass_rate"],
            judge_mean=m["judge_mean"], latency_p50_ms=m["latency_p50_ms"],
            latency_p95_ms=m["latency_p95_ms"], total_tokens=m["total_tokens"],
        )
        cases = [
            CaseResult(
                case_id=c["case_id"], input=c.get("input", ""), output=c.get("output", ""),
                expected=c.get("expected", ""), error=c.get("error"),
                latency_ms=c.get("latency_ms", 0.0), verdict=c.get("verdict"),
                passed=c.get("passed", False), golden=c.get("golden"),
                meta=c.get("meta") or {},
            )
            for c in d.get("cases", [])
        ]
        return cls(
            run_id=d["run_id"], name=d.get("name", ""), dataset=d.get("dataset", ""),
            dataset_version=d.get("dataset_version", "0"), target=d.get("target", ""),
            judge=d.get("judge", "none"), created_at=d.get("created_at", 0.0),
            metrics=metrics, cases=cases,
        )

    def save_json(self, path: str) -> None:
        from pathlib import Path

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- Runner

GATE_METRIC_DEFAULTS: dict[str, str] = {}  # 见 gate.py


class Runner:
    """逐 case 执行 target + judge，全程 tracer 采集，结果落 TraceStore。"""

    def __init__(self, target: Target, judge: JudgeProvider | None = None,
                 store: TraceStore | None = None, judge_threshold: float = 4.0):
        self.target = target
        self.judge = judge
        self.judge_threshold = judge_threshold
        self.store = store

    def _run_case(self, tracer: Tracer, case: EvalCase) -> CaseResult:
        with tracer.span(f"case[{case.id}]", {"case_id": case.id, "tags": case.tags}) as case_span:
            case_span.attributes["input"] = case.input
            with tracer.span("target.call", {"target": self.target.name}) as tspan:
                tracer.event("target.request", {"input": case.input[:500]})
                result = self.target(case)
                tracer.event("target.response", {
                    "output": result.output[:800],
                    "error": result.error,
                    "latency_ms": round(result.latency_ms, 2),
                })
                tspan.attributes["tokens"] = estimate_tokens(result.output)
                tspan.attributes["error"] = result.error
            verdict_dict: dict[str, Any] | None = None
            passed = result.error is None
            if self.judge is not None and result.error is None:
                with tracer.span("judge.score", {"judge": self.judge.name}) as jspan:
                    try:
                        verdict = self.judge.score(case, result.output, self.judge_threshold)
                        verdict_dict = verdict.to_dict()
                        passed = verdict.passed
                        tracer.event("judge.verdict", {
                            "overall": verdict_dict["overall"], "passed": verdict.passed,
                        })
                        jspan.attributes["overall"] = verdict_dict["overall"]
                    except JudgeError as exc:
                        verdict_dict = {"error": str(exc), "judge_name": self.judge.name}
                        result.error = f"judge_error: {exc}"
                        passed = False
                        jspan.status = "error"
                        jspan.error = str(exc)
            tracer.event("case.done", {"case_id": case.id, "passed": passed,
                                       "error": result.error})
            return CaseResult(
                case_id=case.id, input=case.input, output=result.output,
                expected=case.expected, error=result.error,
                latency_ms=result.latency_ms, verdict=verdict_dict, passed=passed,
                golden=case.golden, meta=dict(result.meta),
            )

    def run(self, dataset: EvalDataset, name: str = "eval") -> RunReport:
        tracer = Tracer(name, {"dataset": dataset.name, "dataset_version": dataset.version,
                               "target": self.target.name,
                               "judge": self.judge.name if self.judge else "none"})
        cases: list[CaseResult] = []
        status = "ok"
        with tracer.span(name, {"case_count": len(dataset.cases)}):
            for case in dataset.cases:
                try:
                    cases.append(self._run_case(tracer, case))
                except Exception as exc:  # target 抛异常也要兜住成 case 级 error
                    status = "error"
                    cases.append(CaseResult(
                        case_id=case.id, input=case.input, output="", expected=case.expected,
                        error=f"{type(exc).__name__}: {exc}", golden=case.golden,
                    ))
        run = tracer.finish(status=status)
        if self.store is not None:
            self.store.save_run(run)
        metrics = compute_metrics(cases, run)
        return RunReport(
            run_id=tracer.run_id,
            name=name,
            dataset=dataset.name,
            dataset_version=dataset.version,
            target=self.target.name,
            judge=self.judge.name if self.judge else "none",
            created_at=run.created_at,
            metrics=metrics,
            cases=cases,
        )


def compute_metrics(cases: list[CaseResult], run: Any = None) -> RunMetrics:
    n = len(cases)
    errors = [c for c in cases if c.error]
    latencies = [c.latency_ms for c in cases if not c.error]
    verdicts = [c.verdict for c in cases if c.verdict and not c.verdict.get("error")]
    judge_overalls = [v["overall"] for v in verdicts]
    success_rate = (n - len(errors)) / n if n else 0.0
    if verdicts:
        judge_pass_rate = sum(1 for c in cases if c.passed) / n if n else 0.0
        judge_mean = sum(judge_overalls) / len(judge_overalls)
    else:
        judge_pass_rate = success_rate
        judge_mean = 0.0
    total_tokens = 0
    if run is not None:
        total_tokens = getattr(run, "total_tokens", 0)
    return RunMetrics(
        case_count=n,
        error_count=len(errors),
        success_rate=success_rate,
        judge_pass_rate=judge_pass_rate,
        judge_mean=judge_mean,
        latency_p50_ms=percentile(latencies, 50),
        latency_p95_ms=percentile(latencies, 95),
        total_tokens=total_tokens,
    )


def default_judge(kind: str = "mock") -> JudgeProvider:
    if kind == "mock":
        return MockJudge()
    raise ValueError(f"未知 judge 类型: {kind}（openai_compatible 需构造 OpenAICompatibleJudge）")
