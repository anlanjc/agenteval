"""judge.py 测试：MockJudge 确定性、kappa 手算与边界、JSON 解析、凭据异常、校准报告。"""

import httpx
import pytest

from agenteval.dataset import EvalCase
from agenteval.judge import (CalibrationReport, CalibrationSample, JudgeError,
                             MissingCredentialsError, MockJudge, MockJudgeConfig,
                             OpenAICompatibleJudge, build_rubric_prompt, calibrate,
                             cohen_kappa, evaluate_threshold, parse_verdict_json,
                             sweep_thresholds)


def _case(**kw):
    base = dict(id="t1", input="每月限额多少", expected="每月最高2000元",
                metadata={"keywords": ["2000"]})
    base.update(kw)
    return EvalCase(**base)


# ---------------------------------------------------------------- MockJudge

GOOD = "结论：每月最高2000元。\n要点覆盖：2000。\n- 已核验"


def test_mock_judge_deterministic_and_good_score():
    j = MockJudge()
    c = _case()
    v1 = j.score(c, GOOD)
    v2 = j.score(c, GOOD)
    assert v1.to_dict() == v2.to_dict()          # 确定性：两次打分完全一致
    assert v1.scores == {"correctness": 5.0, "faithfulness": 5.0, "format": 5.0}
    assert v1.overall == pytest.approx(5.0)
    assert v1.passed is True
    assert v1.judge_name == "mock"


def test_mock_judge_detects_missing_and_fabricated():
    j = MockJudge()
    bad = "结论：每月最高1800元，请以公告为准。"
    v = j.score(_case(), bad)
    assert v.scores["correctness"] == 1.0         # 2000 未命中
    assert v.scores["faithfulness"] == 2.0        # 1800 为捏造数字
    assert v.passed is False


def test_mock_judge_verbose_strict_vs_calibrated():
    long_answer = GOOD + "补充" * 600             # 远超 900 字
    strict = MockJudge(MockJudgeConfig())                       # v1: verbose_faithfulness=1.0
    soft = MockJudge(MockJudgeConfig(verbose_faithfulness=3.0))  # v2
    v_strict = strict.score(_case(), long_answer)
    v_soft = soft.score(_case(), long_answer)
    assert v_strict.scores["faithfulness"] == 1.0
    assert v_soft.scores["faithfulness"] == 3.0
    assert v_strict.passed is False and v_soft.passed is True


def test_mock_judge_json_format_dimension():
    j = MockJudge()
    ok = j.score(_case(metadata={"keywords": ["2000"], "format": "json"}),
                 '{"answer": "2000元"}')
    bad = j.score(_case(metadata={"keywords": ["2000"], "format": "json"}),
                  '{"answer": 2000')            # 非法 JSON
    assert ok.scores["format"] == 5.0
    assert bad.scores["format"] == 1.0


# ---------------------------------------------------------------- kappa 手算与边界

def test_cohen_kappa_hand_computed():
    y1 = [1, 1, 0, 1, 0, 1, 1, 0, 1, 0]
    y2 = [1, 1, 0, 1, 0, 0, 1, 0, 1, 1]
    # p_o = 8/10 = 0.8；y1 正类边缘 0.6，y2 正类边缘 0.6 → p_e = 0.36+0.16 = 0.52
    # kappa = (0.8 - 0.52) / (1 - 0.52) = 0.28/0.48 ≈ 0.5833
    assert cohen_kappa(y1, y2) == pytest.approx(0.28 / 0.48)


def test_cohen_kappa_edges():
    assert cohen_kappa(["a", "b"], ["a", "b"]) == 1.0            # 完全一致
    assert cohen_kappa(["a", "a"], ["a", "a"]) == 1.0            # 退化：同投一类且一致
    assert cohen_kappa(["a", "a"], ["b", "b"]) == 0.0            # 退化：p_e=1 且不一致 → 约定 0
    assert cohen_kappa(["a", "b", "a"], ["b", "a", "a"]) == pytest.approx(-0.5)
    # p_o=1/3；marg 均 (2/3,1/3) → p_e=5/9 → kappa=-0.5（负值 = 低于随机一致水平）
    assert cohen_kappa(["a", "b", "b", "a"], ["b", "b", "a", "b"]) == pytest.approx(-0.5)
    # p_o=0.25, p_e=0.5 → -0.5
    with pytest.raises(ValueError):
        cohen_kappa([], [])
    with pytest.raises(ValueError):
        cohen_kappa(["a"], ["a", "b"])


# ---------------------------------------------------------------- rubric / 解析

def test_rubric_prompt_structure():
    c = _case()
    msgs = build_rubric_prompt(c, "answer")
    assert msgs[0]["role"] == "system"
    assert "correctness" in msgs[0]["content"] and "忠实度" in msgs[0]["content"]
    assert "每月限额多少" in msgs[1]["content"]


def test_parse_verdict_json_variants():
    assert parse_verdict_json('{"correctness":5,"faithfulness":4,"format":5,"reasoning":"r"}'
                              )["correctness"] == 5
    fenced = "```json\n{\"correctness\": 3, \"faithfulness\": 3, \"format\": 3}\n```"
    assert parse_verdict_json(fenced)["format"] == 3
    noisy = '前置说明 {"correctness": 2, "faithfulness": 2, "format": 2} 后缀'
    assert parse_verdict_json(noisy)["correctness"] == 2
    with pytest.raises(JudgeError):
        parse_verdict_json("完全不是 JSON")
    with pytest.raises(JudgeError):
        parse_verdict_json('{"correctness": 9, "faithfulness": 4, "format": 5}')  # 越界


# ---------------------------------------------------------------- OpenAI 兼容 judge

def test_openai_judge_missing_key_clear_error(monkeypatch):
    monkeypatch.delenv("AGENTEVAL_LLM_API_KEY", raising=False)
    j = OpenAICompatibleJudge()
    with pytest.raises(MissingCredentialsError) as ei:
        j.score(_case(), GOOD)
    assert "AGENTEVAL_LLM_API_KEY" in str(ei.value)
    assert "--judge mock" in str(ei.value)      # 提示降级路径而不是裸崩


def _mock_llm_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert request.headers["Authorization"] == "Bearer test-key"
        body = httpx.Response(200, json={
            "choices": [{"message": {"content":
                '{"correctness": 4, "faithfulness": 5, "format": 4, "reasoning": "基本正确"}'}}]
        })
        return body
    return httpx.MockTransport(handler)


def test_openai_judge_with_mock_transport(monkeypatch):
    monkeypatch.setenv("AGENTEVAL_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AGENTEVAL_LLM_BASE_URL", "http://mock.local/v1")
    monkeypatch.setenv("AGENTEVAL_LLM_MODEL", "test-model")
    client = httpx.Client(transport=_mock_llm_transport())
    j = OpenAICompatibleJudge(client=client)
    v = j.score(_case(), GOOD)
    assert v.judge_name == "openai_compatible"
    assert v.scores == {"correctness": 4.0, "faithfulness": 5.0, "format": 4.0}
    assert v.overall == pytest.approx(0.5 * 4 + 0.3 * 5 + 0.2 * 4)
    assert v.passed is True


def test_openai_judge_invalid_model_output_then_error(monkeypatch):
    monkeypatch.setenv("AGENTEVAL_LLM_API_KEY", "test-key")
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    j = OpenAICompatibleJudge(client=client)
    with pytest.raises(JudgeError, match="两次尝试均失败"):
        j.score(_case(), GOOD)
    assert calls["n"] == 2                       # 解析失败自动重试一次


# ---------------------------------------------------------------- 校准

def _samples(overalls, goldens):
    return [CalibrationSample(case_id=f"c{i}", golden=g, predicted="pass", overall=o)
            for i, (g, o) in enumerate(zip(goldens, overalls))]


def test_calibrate_confusion_and_accuracy():
    acc, conf = calibrate(
        ["pass", "pass", "fail", "fail", "fail"],
        ["pass", "fail", "fail", "fail", "pass"],
    )
    assert acc == pytest.approx(3 / 5)
    assert conf == {"pass->pass": 1, "pass->fail": 1, "fail->pass": 1, "fail->fail": 2}


def test_evaluate_threshold_and_report_markdown():
    samples = _samples([5.0, 4.5, 3.0, 2.0], ["pass", "pass", "fail", "fail"])
    r = evaluate_threshold(samples, 4.0)
    assert r.accuracy == 1.0
    assert r.kappa == 1.0
    assert r.confusion["pass->pass"] == 2 and r.confusion["fail->fail"] == 2
    md = r.markdown()
    assert "Cohen's kappa" in md and "| pass |" in md


def test_sweep_thresholds_picks_best_kappa():
    # good 分高、bad 分低、中间有个 4.2 的"半对"bad → 阈值压到 (4.2, 4.5] 才最优
    overalls = [5.0, 5.0, 4.5, 4.2, 2.0]
    goldens = ["pass", "pass", "pass", "fail", "fail"]
    samples = _samples(overalls, goldens)
    t, best, sweep = sweep_thresholds(samples)
    assert 4.2 < t <= 4.5
    assert best.kappa == 1.0
    assert len(sweep) == 41                      # 1.0..5.0 step 0.1
    # CalibrationReport 序列化可用
    d = best.to_dict()
    assert d["n"] == 5 and "confusion" in d


def test_calibration_report_render():
    r = CalibrationReport(threshold=4.0, n=4, accuracy=0.75, kappa=0.4,
                          confusion={"pass->pass": 2, "pass->fail": 0,
                                     "fail->pass": 1, "fail->fail": 1},
                          notes="x")
    assert "0.7500" in r.markdown() and "0.4000" in r.markdown()
