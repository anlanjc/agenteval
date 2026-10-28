"""adapters：把同工作区的 Agent 项目（policy-compass / askbi）接入评测平台。

只通过 HTTP 契约对接（参考两项目 app/api 源码的接口形状，不修改它们）；
测试用 httpx.MockTransport 模拟其响应，绝不真实调用。
"""

from .askbi import AskBIAdapter
from .policy_compass import PolicyCompassAdapter

ADAPTERS = {
    "policy_compass": PolicyCompassAdapter,
    "askbi": AskBIAdapter,
}


def get_adapter(name: str, base_url: str, **kwargs):
    cls = ADAPTERS.get(name)
    if cls is None:
        raise ValueError(f"未知适配器: {name}，可选 {sorted(ADAPTERS)}")
    return cls(base_url=base_url, **kwargs)
