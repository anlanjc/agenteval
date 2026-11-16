"""policy-compass 适配器（POST /api/chat，契约参考其 app/api/schemas.py）。

请求：{"question": str, "thread_id"?: str, "history"?: [...], "stream": false}
响应：{"trace_id", "question", "answer", "refusal", "sources": [...], "tool_result",
       "events": [...], "latency_ms"}
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..dataset import EvalCase
from ..runner import TargetResult


class PolicyCompassAdapter:
    name = "policy_compass"

    def __init__(self, base_url: str, timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        # AE-8 修复（评审 4.0）：曾逐 case `with httpx.Client` 新建再销毁——TCP/TLS
        # 握手成本全数计入 latency_ms 汇入 P50/P95（AGT-11 只修了 HTTPTarget）。
        # 对齐 runner.py HTTPTarget 先例：实例级惰性 Client 复用 keep-alive 连接池；
        # close() 由 Runner 评测结束时调用（runner.run finally 对有 close 属性的
        # target 通用，适配器即适用）。
        if self._client is None:
            kwargs: dict[str, Any] = {}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.Client(timeout=self.timeout, **kwargs)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __call__(self, case: EvalCase) -> TargetResult:
        started = time.perf_counter()
        url = f"{self.base_url}/api/chat"
        body = {"question": case.input, "stream": False}
        try:
            resp = self._get_client().post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            answer = str(data.get("answer", ""))
            meta = {
                "refusal": bool(data.get("refusal")),
                "source_count": len(data.get("sources") or []),
                "upstream_trace_id": data.get("trace_id"),
                "upstream_latency_ms": data.get("latency_ms"),
            }
            return TargetResult(
                output=answer, meta=meta,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as exc:
            return TargetResult(
                output="", error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
