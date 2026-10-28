"""askbi 适配器（POST /api/ask，SSE 流式，契约参考其 app/api/main.py）。

请求：{"question": str, "role": "boss"|"south"|"north", "depth": ""|"light"|"full"}
响应：SSE（data: {...}），事件类型 start / node_done / event / error / final；
      final 事件携带 answer: {kind: query|refusal|clarify|drill|error, ...}。
也兼容直接返回 JSON 的部署（取 answer 字段）。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..dataset import EvalCase
from ..runner import TargetResult


def _answer_to_text(answer: dict[str, Any]) -> str:
    """把 askbi 的 answer dict 压成文本（保留 judge 可用关键词）。"""
    parts: list[str] = []
    for key in ("message", "summary", "conclusion"):
        if answer.get(key):
            parts.append(str(answer[key]))
    if "value" in answer and answer["value"] is not None:
        parts.append(f"结果：{answer['value']}")
    if "rows" in answer and isinstance(answer["rows"], list):
        parts.append(f"共 {len(answer['rows'])} 行数据")
    if not parts:
        return json.dumps(answer, ensure_ascii=False)
    return "\n".join(parts)


def _parse_sse(text: str) -> dict[str, Any] | None:
    """从 SSE 文本中取最后一个 type=final 事件的 payload。"""
    final: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "final":
            final = evt
        elif evt.get("type") == "error":
            final = {"error": evt.get("message", "upstream error")}
    return final


class AskBIAdapter:
    name = "askbi"

    def __init__(self, base_url: str, role: str = "boss", depth: str = "",
                 timeout: float = 120.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.role = role
        self.depth = depth
        self.timeout = timeout
        self._transport = transport

    def __call__(self, case: EvalCase) -> TargetResult:
        started = time.perf_counter()
        url = f"{self.base_url}/api/ask"
        # 用例级 role 优先（评测集自带角色分层），否则用构造时的默认角色
        role = str(case.metadata.get("role") or self.role)
        body = {"question": case.input, "role": role, "depth": self.depth}
        try:
            kwargs: dict[str, Any] = {}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            with httpx.Client(timeout=self.timeout, **kwargs) as client:
                resp = client.post(url, json=body)
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    final = _parse_sse(resp.text)
                    if final is None:
                        raise ValueError("SSE 流中未找到 final 事件")
                    if "error" in final:
                        raise RuntimeError(str(final["error"]))
                    answer = final.get("answer") or {}
                    elapsed = final.get("elapsed_ms")
                    task_id = final.get("task_id")
                else:
                    data = resp.json()
                    answer = data.get("answer") or {}
                    elapsed = data.get("elapsed_ms")
                    task_id = data.get("task_id")
            output = _answer_to_text(answer) if isinstance(answer, dict) else str(answer)
            meta = {
                "answer_kind": answer.get("kind") if isinstance(answer, dict) else None,
                "task_id": task_id,
                "upstream_latency_ms": elapsed,
            }
            return TargetResult(
                output=output, meta=meta,
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except Exception as exc:
            return TargetResult(
                output="", error=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
