"""LLM-as-judge 评分器 + 校准模块（judge 输出 vs 人工 golden 标注）。

组成：
- JudgeVerdict / Dimension：评分结果模型（正确性 / 忠实度 / 格式，各 1-5 分）
- build_rubric_prompt：评分 rubric 提示词
- MockJudge：确定性规则打分（关键词命中 + 长度 + 数字捏造检测），测试与离线自评测用
- OpenAICompatibleJudge：httpx 调 OpenAI 兼容接口（DashScope compatible-mode 等），
  凭据全部读环境变量，缺 key 抛 MissingCredentialsError（清晰提示而不是崩）
- 校准：cohen_kappa / 混淆矩阵 / threshold 扫描 → CalibrationReport
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from .dataset import EvalCase, extract_keywords

# ---------------------------------------------------------------- 数据模型

DIMENSIONS = ("correctness", "faithfulness", "format")
DIMENSION_NAMES_CN = {
    "correctness": "正确性",
    "faithfulness": "忠实度",
    "format": "格式",
}

# 维度加权：正确性为主
DEFAULT_WEIGHTS = {"correctness": 0.5, "faithfulness": 0.3, "format": 0.2}


@dataclass
class JudgeVerdict:
    case_id: str
    scores: dict[str, float]           # 每维度 1-5
    overall: float                     # 加权平均 1-5
    passed: bool                       # overall >= threshold
    threshold: float = 4.0
    reasoning: str = ""
    judge_name: str = "mock"
    error: str | None = None           # judge 自身失败时记录（case 记 error 而非误判）

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "scores": self.scores,
            "overall": round(self.overall, 4),
            "passed": self.passed,
            "threshold": self.threshold,
            "reasoning": self.reasoning,
            "judge_name": self.judge_name,
            "error": self.error,
        }


class JudgeError(RuntimeError):
    """judge 调用/解析失败。"""


class MissingCredentialsError(JudgeError):
    """LLM 凭据未配置（环境变量缺失）。"""


class JudgeProvider(Protocol):
    """judge 抽象：输入用例与被测输出，输出结构化评分。"""

    name: str

    def score(self, case: EvalCase, output: str, threshold: float = 4.0) -> JudgeVerdict: ...


# ---------------------------------------------------------------- rubric 提示词

_RUBRIC = {
    "correctness": (
        "正确性：答案是否实质回应用户问题，事实/数值是否与参考要点一致。"
        "5=参考要点中的关键数值/事实全部出现且无矛盾（额外的背景或提示性补充不扣分）；"
        "4=关键数值出现但有一处无关紧要的表述偏差；3=缺少部分关键数值或要点覆盖不足；"
        "2=多数错误；1=答非所问或关键数值错误。"
    ),
    "faithfulness": (
        "忠实度：答案是否只基于给定上下文/参考要点，不编造数字与事实。"
        "5=无任何捏造——答案的长度、详略与补充性说明（如办理渠道提示）不作为扣分依据；"
        "4=有轻微引申但无事实错误；3=有明显编造成分；2=大量编造；1=与给定信息矛盾。"
    ),
    "format": (
        "格式：答案结构是否清晰、符合任务要求（要点分条/字段完整/可直接使用）。"
        "5=结构清晰完整；4=基本规范（超长但有条理仍可评 4-5）；3=有小问题；"
        "2=影响理解；1=完全不符合要求。长度本身不是格式缺陷。"
    ),
}


def build_rubric_prompt(case: EvalCase, output: str,
                        dimensions: Sequence[str] = DIMENSIONS) -> list[dict[str, str]]:
    """构造评分 prompt（system + user），要求模型只输出严格 JSON。"""
    rubric_txt = "\n".join(f"- {dim}（{DIMENSION_NAMES_CN[dim]}）：{_RUBRIC[dim]}"
                           for dim in dimensions)
    system = (
        "你是严谨、公允的 LLM 评审（LLM-as-judge）。评分原则："
        "① 只依据答案与参考要点的事实一致性评分——长度、详略与补充性说明本身不扣分；"
        "② 先核对参考要点中的关键数值/事实是否在答案中出现且无矛盾，这决定 correctness 的高分档；"
        "③ 短答案与长答案都可能是正确答案，不要因风格偏好压分。"
        "按 rubric 逐维度打分（1-5 整数），只输出 JSON，不要输出其他文字：\n"
        '{"correctness": <1-5>, "faithfulness": <1-5>, "format": <1-5>, "reasoning": "<50字内理由>"}'
        f"\n评分维度：\n{rubric_txt}"
    )
    user = (
        f"[用户输入]\n{case.input}\n\n"
        f"[参考要点/期望]\n{case.expected or '(无)'}\n\n"
        f"[被评输出]\n{output}\n\n"
        "请按 rubric 输出 JSON 评分。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_verdict_json(text: str) -> dict[str, Any]:
    """从模型回复中解析评分 JSON（容忍 markdown 代码围栏）。"""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise JudgeError(f"judge 回复中未找到 JSON：{text[:120]!r}")
    data = json.loads(cleaned[start:end + 1])
    for dim in DIMENSIONS:
        v = data.get(dim)
        if not isinstance(v, (int, float)) or not 1 <= v <= 5:
            raise JudgeError(f"维度 {dim} 分数非法: {v!r}")
    return data


# ---------------------------------------------------------------- MockJudge


@dataclass
class MockJudgeConfig:
    """确定性规则打分的可调参数（校准实验的对象）。

    correctness_bands: (命中率下限含, 分数) 降序取第一个命中。
    verbose_faithfulness: 输出超过 max_chars 时 faithfulness 的封顶分
        （v1 默认 1.0 —— "偏严"规则：把超长一票打成不忠实；校准后调到 3.0）。
    """

    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    max_chars: int = 900              # 超过视为"超长"
    verbose_faithfulness: float = 1.0
    correctness_bands: tuple[tuple[float, float], ...] = (
        (1.0, 5.0),   # 全命中
        (0.8, 4.0),   # >=80%
        (0.5, 3.0),   # >=50%
        (0.01, 2.0),  # 有命中
        (0.0, 1.0),   # 全落空
    )


class MockJudge:
    """确定性 judge：不依赖网络，打分可复现，用于测试与 mock 自评测。

    规则（对输出做规则判断）：
    - correctness：参考关键词命中率（keywords 来自 metadata 或从 expected 自动抽取）
    - faithfulness：输出中的数字必须都在 expected 里出现（防捏造）；超长扣分（v1 偏严规则）
    - format：需包含结论标记（"结论"/"答"/markdown 分条/JSON 合法）
    """

    name = "mock"

    def __init__(self, config: MockJudgeConfig | None = None):
        self.cfg = config or MockJudgeConfig()

    # ---- 各维度 ------------------------------------------------------------
    def _keywords(self, case: EvalCase) -> list[str]:
        kws = case.metadata.get("keywords")
        if isinstance(kws, list) and kws:
            return [str(k) for k in kws]
        return extract_keywords(case.expected)

    def _correctness(self, case: EvalCase, output: str) -> tuple[float, dict[str, Any]]:
        kws = self._keywords(case)
        if not kws:
            return 3.0, {"hit_ratio": None, "note": "no keywords"}
        hits = [k for k in kws if k in output]
        ratio = len(hits) / len(kws)
        score = 1.0
        for lo, s in self.cfg.correctness_bands:
            if ratio >= lo:
                score = s
                break
        return score, {"hit_ratio": round(ratio, 3), "missing": [k for k in kws if k not in output]}

    def _faithfulness(self, case: EvalCase, output: str) -> tuple[float, dict[str, Any]]:
        expected = case.expected
        out_numbers = set(re.findall(r"\d+(?:\.\d+)?", output))
        fabricated = sorted(n for n in out_numbers if expected and n not in expected)
        note: dict[str, Any] = {"fabricated_numbers": fabricated}
        score = 5.0
        if fabricated:
            score = 2.0
        if len(output) > self.cfg.max_chars:
            score = min(score, self.cfg.verbose_faithfulness)
            note["verbose"] = len(output)
        return score, note

    def _format(self, case: EvalCase, output: str) -> tuple[float, dict[str, Any]]:
        fmt = case.metadata.get("format") if isinstance(case.metadata, dict) else None
        if fmt == "json":
            try:
                json.loads(output)
                return 5.0, {"json": True}
            except Exception:
                return 1.0, {"json": False}
        ok_markers = ("结论", "答", "综上", "\n-", "\n*", "1.", "\n1.")
        if any(m in output for m in ok_markers):
            return 5.0, {"marker": True}
        return 3.0, {"marker": False}

    # ---- 汇总 --------------------------------------------------------------
    def overall(self, scores: dict[str, float]) -> float:
        w = self.cfg.weights
        total_w = sum(w.get(d, 0.0) for d in scores) or 1.0
        return sum(scores[d] * w.get(d, 0.0) for d in scores) / total_w

    def score(self, case: EvalCase, output: str, threshold: float = 4.0) -> JudgeVerdict:
        cs, cnote = self._correctness(case, output)
        fs, fnote = self._faithfulness(case, output)
        fmt_s, fmtnote = self._format(case, output)
        scores = {"correctness": cs, "faithfulness": fs, "format": fmt_s}
        overall = self.overall(scores)
        reasoning = (
            f"关键词命中={cnote.get('hit_ratio')}, 捏造数字={fnote.get('fabricated_numbers')}, "
            f"格式标记={fmtnote.get('marker', fmtnote.get('json'))}"
        )
        return JudgeVerdict(
            case_id=case.id,
            scores=scores,
            overall=overall,
            passed=overall >= threshold,
            threshold=threshold,
            reasoning=reasoning,
            judge_name=self.name,
        )


# ---------------------------------------------------------------- OpenAI 兼容 judge

ENV_BASE_URL = "AGENTEVAL_LLM_BASE_URL"
ENV_API_KEY = "AGENTEVAL_LLM_API_KEY"
ENV_MODEL = "AGENTEVAL_LLM_MODEL"

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-plus"


class OpenAICompatibleJudge:
    """调 OpenAI 兼容 /chat/completions 的 judge（DashScope compatible-mode 等）。

    环境变量：
        AGENTEVAL_LLM_BASE_URL  默认 DashScope compatible-mode
        AGENTEVAL_LLM_API_KEY   必填，缺失时抛 MissingCredentialsError
        AGENTEVAL_LLM_MODEL     默认 qwen-plus
    """

    name = "openai_compatible"

    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float = 60.0,
                 client: Any = None):
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get(ENV_API_KEY)
        self.model = model or os.environ.get(ENV_MODEL) or DEFAULT_MODEL
        self.timeout = timeout
        self._client = client  # 测试可注入 httpx.Client/MockTransport

    def require_credentials(self) -> None:
        if not self.api_key:
            raise MissingCredentialsError(
                "缺少 LLM API key：请设置环境变量 "
                f"{ENV_API_KEY}（可选 {ENV_BASE_URL}、{ENV_MODEL}）。"
                "mock 模式不需要 key：使用 --judge mock。"
            )

    def _chat(self, messages: list[dict[str, str]]) -> str:
        import httpx

        self.require_credentials()
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,  # judge 要稳定
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self._client is not None:
            resp = self._client.post(url, json=payload, headers=headers)
        else:
            resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise JudgeError(f"响应缺少 choices[0].message.content: {data!r}") from exc

    def score(self, case: EvalCase, output: str, threshold: float = 4.0) -> JudgeVerdict:
        self.require_credentials()  # 缺凭据快速失败，不进入重试
        messages = build_rubric_prompt(case, output)
        last_err: Exception | None = None
        for attempt in range(2):  # 解析失败重试一次
            try:
                text = self._chat(messages)
                data = parse_verdict_json(text)
            except JudgeError as exc:
                last_err = exc
                continue
            except Exception as exc:  # 网络等
                raise JudgeError(f"judge 调用失败: {exc}") from exc
            scores = {d: float(data[d]) for d in DIMENSIONS}
            overall = sum(scores[d] * DEFAULT_WEIGHTS[d] for d in DIMENSIONS)
            return JudgeVerdict(
                case_id=case.id,
                scores=scores,
                overall=overall,
                passed=overall >= threshold,
                threshold=threshold,
                reasoning=str(data.get("reasoning", "")),
                judge_name=self.name,
            )
        raise JudgeError(f"judge 两次尝试均失败: {last_err}")


# ---------------------------------------------------------------- 校准模块


def cohen_kappa(y1: Sequence[Any], y2: Sequence[Any]) -> float:
    """Cohen's kappa = (p_o - p_e) / (1 - p_e)。

    边界约定：
    - 两序列长度不同或为空 → ValueError
    - 完全一致（p_o == 1）→ 1.0（含两 rater 都只投同一类的退化情况）
    - p_e == 1 且不一致（两 rater 各自只投一个不同类）→ 0.0（分母为 0，按约定取 0）
    """
    if len(y1) != len(y2):
        raise ValueError("两个标注序列长度必须一致")
    n = len(y1)
    if n == 0:
        raise ValueError("标注序列不能为空")
    labels = sorted(set(y1) | set(y2), key=str)
    agree = sum(1 for a, b in zip(y1, y2) if a == b)
    p_o = agree / n
    if p_o == 1.0:
        return 1.0
    marg1 = {lb: sum(1 for a in y1 if a == lb) / n for lb in labels}
    marg2 = {lb: sum(1 for b in y2 if b == lb) / n for lb in labels}
    p_e = sum(marg1[lb] * marg2[lb] for lb in labels)
    if p_e >= 1.0:
        return 0.0
    return (p_o - p_e) / (1.0 - p_e)


@dataclass
class CalibrationSample:
    """一条校准样本：golden 人工标注 + judge 决策。"""

    case_id: str
    golden: str            # "pass" | "fail"
    predicted: str         # "pass" | "fail"
    overall: float


@dataclass
class CalibrationReport:
    threshold: float
    n: int
    accuracy: float
    kappa: float
    confusion: dict[str, int]      # "golden->predicted" 计数
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "n": self.n,
            "accuracy": round(self.accuracy, 4),
            "kappa": round(self.kappa, 4),
            "confusion": self.confusion,
            "notes": self.notes,
        }

    def markdown(self) -> str:
        c = self.confusion
        lines = [
            f"## Judge 校准报告（threshold={self.threshold}, n={self.n}）",
            "",
            f"- accuracy: **{self.accuracy:.4f}**",
            f"- Cohen's kappa: **{self.kappa:.4f}**",
            "",
            "| golden \\ predicted | pass | fail |",
            "| --- | --- | --- |",
            f"| pass | {c.get('pass->pass', 0)} | {c.get('pass->fail', 0)} |",
            f"| fail | {c.get('fail->pass', 0)} | {c.get('fail->fail', 0)} |",
            "",
        ]
        if self.notes:
            lines.append(f"> {self.notes}")
        return "\n".join(lines)


def calibrate(goldens: Sequence[str], predicted: Sequence[str]) -> tuple[float, dict[str, int]]:
    """accuracy + 混淆矩阵（行=golden，列=predicted）。"""
    if len(goldens) != len(predicted):
        raise ValueError("golden 与 predicted 数量不一致")
    n = len(goldens)
    if n == 0:
        raise ValueError("校准样本为空")
    labels = sorted(set(goldens) | set(predicted), key=str)
    confusion = {f"{g}->{p}": 0 for g in labels for p in labels}
    for g, p in zip(goldens, predicted):
        confusion[f"{g}->{p}"] += 1
    accuracy = sum(1 for g, p in zip(goldens, predicted) if g == p) / n
    return accuracy, confusion


def evaluate_threshold(samples: Sequence[CalibrationSample], threshold: float) -> CalibrationReport:
    goldens = [s.golden for s in samples]
    predicted = ["pass" if s.overall >= threshold else "fail" for s in samples]
    accuracy, confusion = calibrate(goldens, predicted)
    kappa = cohen_kappa(goldens, predicted)
    return CalibrationReport(
        threshold=threshold, n=len(samples), accuracy=accuracy,
        kappa=kappa, confusion=confusion,
    )


def sweep_thresholds(samples: Sequence[CalibrationSample],
                     grid: Sequence[float] | None = None) -> tuple[float, CalibrationReport, list[CalibrationReport]]:
    """在阈值网格上找 kappa 最大的决策阈值（并列取最高阈值：在同等 kappa 下偏好更严格）。

    返回 (best_threshold, best_report, sweep 明细)。
    """
    if grid is None:
        grid = [i / 10 for i in range(10, 51)]  # 1.0 .. 5.0
    reports = [evaluate_threshold(samples, t) for t in grid]
    best = max(reports, key=lambda r: (r.kappa, r.accuracy, r.threshold))
    return best.threshold, best, reports
