"""gateway-smoke 的 echo mock 上游：OpenAI /v1/chat/completions 形状（非流式 JSON）。

[1B4-SM] AE-12 门禁演练专用：把用户消息原样回显进"结论：…"模板——MockJudge 的
关键词/格式/忠实度三维都能确定性打分（数据集 gw-xxx 的 keywords ⊂ 问题 ⊂ 回显文本，
问题全部无数字 → 忠实度不触发捏造扣分）。与 agentgate-go/scripts/mock_upstream.py
（压测口径，固定"ok"响应）不同：那版供双语言压测吃同一响应形态，本版供评测冒烟
判分用，两者互不替代。

口径声明：mock upstream / 本机单进程（网关+上游+redis 全在同一台机），时延不是信号。

用法：python scripts/gateway_mock_upstream.py --port 9302
"""

from __future__ import annotations

import argparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

_ECHO_SUFFIX = "——已由 agentgate-go 网关转发至 mock 上游返回（gateway-smoke-echo）。"


def _last_user_content(messages: list[dict]) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user" and msg.get("content"):
            return str(msg["content"])
    return ""


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    content = f"结论：{_last_user_content(body.get('messages') or [])}{_ECHO_SUFFIX}"
    n = max(len(content), 1)
    return JSONResponse({
        "id": "cmpl-gw-smoke",
        "model": body.get("model") or "gateway-smoke-mock",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        # usage 与真实 OpenAI 响应对齐（total_tokens 驱动网关账行/配额计量口径）
        "usage": {"prompt_tokens": n, "completion_tokens": n, "total_tokens": 2 * n},
    })


@app.get("/healthz")
async def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9302)
    args = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
