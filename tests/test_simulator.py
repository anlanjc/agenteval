"""simulator.py 测试：脚本化多轮、ending marker、隐藏政策、LLM 模拟器缺 key。"""

import pytest

from agenteval.judge import MissingCredentialsError
from agenteval.simulator import (EchoChatTarget, HiddenPolicy, LLMSimulator,
                                 MockSimulator, Persona, case_from_simulation,
                                 run_simulation)


def test_mock_simulator_scripted_multi_turn():
    sim = MockSimulator(["退货怎么操作？", "订单号 A1001", "好的谢谢"], persona=Persona("小王"))
    target = EchoChatTarget()
    result = run_simulation(target, sim, max_turns=6)
    assert result.turns == 3
    assert result.ended_reason == "script_end"
    assert len(result.transcript) == 6           # user/assistant 交替
    assert result.transcript[0]["role"] == "user"
    assert result.transcript[0]["content"] == "退货怎么操作？"
    assert result.transcript[1]["role"] == "assistant"
    assert "退货怎么操作" in result.transcript[1]["content"]   # echo target 回显
    # echo target 看到的历史逐轮增长
    assert "A1001" in result.transcript[3]["content"]


def test_simulation_ending_marker_success():
    sim = MockSimulator(["问题1", "问题2", "问题3"], ending_marker="[RESOLVED]")
    target = EchoChatTarget(completion_marker="[RESOLVED]")
    result = run_simulation(target, sim)
    assert result.ended_reason == "ending_marker"
    assert result.success is True
    assert result.turns == 3                     # EchoChatTarget 第 3 轮才带 marker


def test_simulation_max_turns_guard():
    sim = MockSimulator(["q1", "q2", "q3", "q4", "q5"])
    result = run_simulation(EchoChatTarget(), sim, max_turns=2)
    assert result.turns == 2
    assert result.ended_reason == "max_turns"
    assert result.success is False


def test_hidden_policy_revealed_on_question():
    policy = HiddenPolicy(rules=["订单 7 天内可无理由退货"],
                          secret_facts={"订单号": "我的订单号是 A2002"})
    sim = MockSimulator(["我要退货", "请提供订单号", "好的我等待处理"], policy=policy)
    # EchoChatTarget 回显用户内容 → agent 第2轮回复包含"订单号" → 模拟器在下一轮按政策透露
    result = run_simulation(EchoChatTarget(), sim, max_turns=4)
    user_msgs = [m["content"] for m in result.transcript if m["role"] == "user"]
    assert any("A2002" in m for m in user_msgs)


def test_case_from_simulation():
    sim = MockSimulator(["你好"])
    result = run_simulation(EchoChatTarget(), sim, max_turns=1)
    case = case_from_simulation("sim-1", result, expected="期望回复")
    assert case.id == "sim-1"
    assert case.tags == ["simulation"]
    assert "user: 你好" in case.input
    assert case.metadata["simulation"]["turns"] == 1


def test_llm_simulator_missing_key_clear_error(monkeypatch):
    monkeypatch.delenv("AGENTEVAL_LLM_API_KEY", raising=False)
    sim = LLMSimulator(persona=Persona("测试用户"))
    with pytest.raises(MissingCredentialsError) as ei:
        sim.next_user_turn([], None)
    assert "AGENTEVAL_LLM_API_KEY" in str(ei.value)
    assert "MockSimulator" in str(ei.value)


def test_mock_simulator_empty_script_rejected():
    with pytest.raises(ValueError):
        MockSimulator([])
