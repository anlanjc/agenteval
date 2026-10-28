"""确定性 Mock Agent：不依赖网络的被测对象。

按 case 的 metadata.keywords 生成"规范答案"（关键词全覆盖 → judge 满分路径）；
degrade_ratio > 0 时用种子随机挑选部分 case 注入错误输出（数字改写/关键词丢失），
用于回归门禁演示与 API 的开箱即跑。
"""

from __future__ import annotations

import random
import time

from .dataset import EvalCase, extract_keywords
from .runner import TargetResult

_RAMBLE = "（补充说明）该答复同时给出办理渠道与注意事项，建议用户携带有效证件前往线下网点或通过线上服务渠道办理，如有疑问可再次询问。"


class MockAgentTarget:
    name = "mock_agent"

    def __init__(self, degrade_ratio: float = 0.0, seed: int = 7,
                 verbose_ids: set[str] | None = None, sleep_ms: float = 0.0):
        self.degrade_ratio = degrade_ratio
        self._rng = random.Random(seed)
        self.verbose_ids = set(verbose_ids or ())
        self.sleep_ms = sleep_ms
        self._degraded_ids: set[str] = set()

    @property
    def degraded_ids(self) -> set[str]:
        return set(self._degraded_ids)

    def bind_dataset(self, case_ids: list[str]) -> "MockAgentTarget":
        """预先确定哪些 case 会被降级（确定性的回归演示）。"""
        n = int(round(len(case_ids) * self.degrade_ratio))
        self._degraded_ids = set(self._rng.sample(sorted(case_ids), n)) if n else set()
        return self

    @staticmethod
    def _keywords(case: EvalCase) -> list[str]:
        kws = case.metadata.get("keywords")
        if isinstance(kws, list) and kws:
            return [str(k) for k in kws]
        return extract_keywords(case.expected)

    def good_output(self, case: EvalCase) -> str:
        # 只输出 expected 里确实包含的关键词，避免 mock agent 自相矛盾地"捏造"数字
        kws = [k for k in self._keywords(case) if k in case.expected]
        body = "、".join(kws) if kws else "已按参考要点回答"
        text = (f"结论：{case.expected}\n要点覆盖：{body}。\n"
                "以上信息已核验，- 办理渠道见官方线上入口。")
        if case.id in self.verbose_ids:
            text += _RAMBLE * 12  # 制造超长答案（judge 校准的"偏严"陷阱）
        return text

    def bad_output(self, case: EvalCase) -> str:
        """错误输出：数字 +999（捏造）、非数字关键词丢失 → judge 应判 fail。"""
        kws = self._keywords(case)
        broken: list[str] = []
        for k in kws:
            if k.isdigit():
                broken.append(str(int(k) + 999))
            else:
                broken.append("略")
        body = "、".join(broken) or "无法确定"
        return f"结论：该问题答复为 {body}，请以最新公告为准。"

    def __call__(self, case: EvalCase) -> TargetResult:
        started = time.perf_counter()
        if self.sleep_ms:
            time.sleep(self.sleep_ms / 1000.0)
        is_bad = case.id in self._degraded_ids
        output = self.bad_output(case) if is_bad else self.good_output(case)
        return TargetResult(
            output=output,
            meta={"mock": True, "degraded": is_bad},
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
