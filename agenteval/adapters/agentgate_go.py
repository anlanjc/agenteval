"""agentgate-go 适配器（POST /v1/chat/completions，OpenAI 兼容契约）。

请求：{"model": str, "messages": [{"role": "user", "content": case.input}], "stream": false}
鉴权：Authorization: Bearer <api_key>（agentgate-go 虚拟 key，vk 由网关侧预置）。
响应：OpenAI 形状 JSON——choices[0].message.content 为回答文本，usage 带账面 token；
      agentgate-go 附加响应头 x-cache（M3 响应缓存命中标记），进 meta 留痕。

与 askbi / policy_compass 适配器同构（AE-8 先例）：实例级惰性 httpx.Client 复用
keep-alive 连接池——逐 case 新建 Client 会把 TCP/TLS 握手成本计入 latency_ms 污染
P50/P95；close() 经 Runner.run 的 finally 通道归还连接池。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..dataset import EvalCase
from ..runner import TargetResult


class AgentGateGoAdapter:
    name = "agentgate_go"

    def __init__(self, base_url: str, api_key: str = "ag-smoke",
                 model: str = "gateway-smoke-mock", timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._transport = transport
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        # AE-8 先例对齐：实例级惰性 Client（keep-alive 复用），不用逐 case 新建。
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
        url = f"{self.base_url}/v1/chat/completions"
        body = {
            "model": str(case.metadata.get("model") or self.model),
            "messages": [{"role": "user", "content": case.input}],
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            client = self._get_client()
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise ValueError("响应缺少 choices 字段")
            message = choices[0].get("message") or {}
            content = message.get("content")
            if content is None:
                raise ValueError("choices[0].message.content 缺失")
            usage = data.get("usage") or {}
            meta = {
                "gateway_model": data.get("model"),
                "finish_reason": choices[0].get("finish_reason"),
                "usage_total_tokens": usage.get("total_tokens"),
                "x_cache": resp.headers.get("x-cache"),
                "request_id": resp.headers.get("x-request-id"),
            }
            return TargetResult(
                output=str(content), meta=meta,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as exc:
            return TargetResult(
                output="", error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
