"""τ-bench 式用户模拟器：persona + 隐藏 policy，多轮对话。

- Simulator 协议：给出对话历史与 agent 最近回复，产出下一条用户消息；返回 None 表示结束。
- MockSimulator：脚本化应答（确定性，测试与离线评测用）。
- LLMSimulator：真实 LLM 驱动的模拟器（留 provider 接口，缺 key 抛清晰异常）。
- run_simulation：驱动 agent target 多轮对话，全程 tracer 采集 transcript。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from .dataset import EvalCase
from .judge import MissingCredentialsError
from .runner import Target, TargetResult
from .trace import Tracer, TraceStore, estimate_tokens

ENV_BASE_URL = "AGENTEVAL_LLM_BASE_URL"
ENV_API_KEY = "AGENTEVAL_LLM_API_KEY"
ENV_MODEL = "AGENTEVAL_LLM_MODEL"

Message = dict[str, str]  # {"role": "user"|"assistant", "content": str}


@dataclass
class Persona:
    """模拟用户的画像。"""

    name: str = "用户"
    style: str = "礼貌但着急，说话简短"
    goal: str = "解决一个问题"


@dataclass
class HiddenPolicy:
    """隐藏政策：只有模拟器知道，agent 需要在对话中自己探明（τ-bench 精髓）。"""

    rules: list[str] = field(default_factory=list)
    secret_facts: dict[str, str] = field(default_factory=dict)  # 模拟器被问到才透露

    def answer_about(self, question: str) -> str | None:
        """模拟器按隐藏政策回答事实性问题（MockSimulator 用）。"""
        q = question.lower()
        for key, fact in self.secret_facts.items():
            if key.lower() in q:
                return fact
        return None


class Simulator(Protocol):
    persona: Persona
    policy: HiddenPolicy | None

    def next_user_turn(self, history: list[Message], agent_reply: str | None) -> str | None: ...


class MockSimulator:
    """脚本化模拟器：按预设脚本轮次输出；agent 回复中命中 ending_marker 提前结束。

    可选规则：当 agent 回复包含 policy 相关关键词时，透露隐藏事实（模拟"用户知道政策细节"）。
    """

    def __init__(self, script: list[str], persona: Persona | None = None,
                 policy: HiddenPolicy | None = None, ending_marker: str | None = None):
        if not script:
            raise ValueError("MockSimulator 脚本不能为空")
        self.script = list(script)
        self.persona = persona or Persona()
        self.policy = policy
        self.ending_marker = ending_marker
        self._turn = 0

    def reset(self) -> None:
        self._turn = 0

    def next_user_turn(self, history: list[Message], agent_reply: str | None) -> str | None:
        if agent_reply and self.ending_marker and self.ending_marker in agent_reply:
            return None  # agent 宣告完成，对话结束
        if self._turn >= len(self.script):
            return None
        turn = self.script[self._turn]
        self._turn += 1
        # 隐藏政策：agent 问到政策关键词时透露（模拟真实用户"被问到才说"）
        if self.policy and agent_reply:
            secret = self.policy.answer_about(agent_reply)
            if secret:
                turn = f"{turn}（补充：{secret}）"
        return turn


class LLMSimulator:
    """真实 LLM 驱动的用户模拟器（provider 接口留好，缺 key 明确报错）。

    prompt 结构：persona + goal + 隐藏政策 + 对话历史 → 下一条用户消息。
    """

    persona: Persona
    policy: HiddenPolicy | None

    def __init__(self, persona: Persona | None = None, policy: HiddenPolicy | None = None,
                 base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, max_turns: int = 8):
        self.persona = persona or Persona()
        self.policy = policy
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL)
                         or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.api_key = api_key or os.environ.get(ENV_API_KEY)
        self.model = model or os.environ.get(ENV_MODEL) or "qwen-plus"
        self.max_turns = max_turns

    def _require_key(self) -> None:
        if not self.api_key:
            raise MissingCredentialsError(
                "LLMSimulator 需要 API key：设置环境变量 "
                f"{ENV_API_KEY}（可选 {ENV_BASE_URL}、{ENV_MODEL}），"
                "或改用 MockSimulator（脚本化、确定性）。"
            )

    def next_user_turn(self, history: list[Message], agent_reply: str | None) -> str | None:
        import httpx

        self._require_key()
        if agent_reply is not None and len([m for m in history if m["role"] == "user"]) >= self.max_turns:
            return None
        policy_txt = ""
        if self.policy:
            policy_txt = "\n".join(f"- {r}" for r in self.policy.rules)
        system = (
            f"你在扮演一个用户：{self.persona.name}。风格：{self.persona.style}。目标：{self.persona.goal}。\n"
            f"{('你知道的隐藏信息（被问到才说，不要主动全盘托出）：\n' + policy_txt) if policy_txt else ''}\n"
            "只输出用户的下一句话，不要输出其他内容。若问题已解决，只输出：[DONE]"
        )
        messages = [{"role": "system", "content": system}] + history[-10:]
        resp = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"model": self.model, "messages": messages, "temperature": 0.7},
            timeout=60.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if "[DONE]" in content:
            return None
        return content


# ---------------------------------------------------------------- 多轮驱动


@dataclass
class SimulationResult:
    transcript: list[Message]
    turns: int
    ended_reason: str            # "script_end" | "ending_marker" | "max_turns"
    success: bool                # agent 最后回复包含 ending_marker（或判定函数通过）
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript": self.transcript, "turns": self.turns,
            "ended_reason": self.ended_reason, "success": self.success,
            "duration_ms": round(self.duration_ms, 2),
        }


class ChatTarget(Protocol):
    """多轮 target：接收完整历史，返回 agent 回复。"""

    name: str

    def reply(self, history: list[Message], user_message: str) -> str: ...


class EchoChatTarget:
    """测试用：把历史拼起来回显的最小 chat target。"""

    name = "echo"

    def __init__(self, completion_marker: str | None = None, respond_every: int = 1):
        self.completion_marker = completion_marker
        self.respond_every = respond_every
        self._calls = 0

    def reply(self, history: list[Message], user_message: str) -> str:
        self._calls += 1
        base = f"收到：{user_message}（第{self._calls}轮）"
        if self.completion_marker and self._calls % self.respond_every == 0 \
                and self._calls >= 3:
            return f"{base} {self.completion_marker}"
        return base


def run_simulation(target: ChatTarget, simulator: Simulator,
                   opening_question: str | None = None, max_turns: int = 8,
                   tracer: Tracer | None = None, store: TraceStore | None = None,
                   success_marker: str | None = None) -> SimulationResult:
    """驱动多轮对话：simulator 出用户话 → target 回复 → 循环。

    结束条件：模拟器返回 None（脚本尽/agent 告完成）或达到 max_turns。
    """
    import time as _time

    if isinstance(simulator, MockSimulator):
        simulator.reset()
    started = _time.perf_counter()
    tracer = tracer or Tracer("simulation", {"target": getattr(target, "name", "?")})
    history: list[Message] = []
    user_msg = opening_question
    if user_msg is None:
        user_msg = simulator.next_user_turn([], None)
    ended_reason = "max_turns"
    marker = success_marker or getattr(simulator, "ending_marker", None)
    turns = 0
    with tracer.span("simulation", {"persona": simulator.persona.name}) as sim_span:
        while turns < max_turns and user_msg is not None:
            history.append({"role": "user", "content": user_msg})
            with tracer.span(f"turn[{turns + 1}]", {"user": user_msg[:200]}) as tspan:
                tracer.event("user.turn", {"content": user_msg})
                reply = target.reply(history, user_msg)
                tracer.event("agent.reply", {"content": reply})
                tspan.attributes["tokens"] = estimate_tokens(reply)
            history.append({"role": "assistant", "content": reply})
            turns += 1
            if marker and marker in reply:
                ended_reason = "ending_marker"
                break
            user_msg = simulator.next_user_turn(history, reply)
            if user_msg is None:
                ended_reason = "script_end"
                break
    success = bool(history and marker and marker in history[-1]["content"])
    duration = (_time.perf_counter() - started) * 1000.0
    sim_span.attributes["turns"] = turns
    sim_span.attributes["ended_reason"] = ended_reason
    run = tracer.finish(status="ok")
    if store is not None:
        store.save_run(run)
    return SimulationResult(
        transcript=history, turns=turns, ended_reason=ended_reason,
        success=success, duration_ms=duration,
    )


def case_from_simulation(case_id: str, result: SimulationResult,
                         expected: str = "", tags: list[str] | None = None) -> EvalCase:
    """把模拟对话压成单条 EvalCase（多轮评测接入 runner 的桥）。"""
    dialogue = "\n".join(f"{m['role']}: {m['content']}" for m in result.transcript)
    return EvalCase(
        id=case_id,
        input=dialogue,
        expected=expected,
        metadata={"simulation": result.to_dict()},
        tags=tags or ["simulation"],
    )
