"""评测集数据增强器：从既有用例生成"鲁棒性变体"与"待人工复核候选"。

两条增强路径（诚实边界内置）：
1. **噪声变体**（golden 保留）：空白/大小写/语气词等不改变语义的扰动——
   expected 不变，构成"噪声鲁棒性"评测维度（模型应答对扰动不变）；
2. **实体替换候选**（golden 置空）：把 input 里的实体换成同位新值——语义可能
   成立也可能不成立，**系统无法自动判定 expected**，所以 golden=None 标记为
   "待人工复核"，dataset.with_golden() 会把它们排除出自动评测。

设计红线：增强器永远不猜 expected——宁可产出待复核候选，不制造伪标注。
"""
from __future__ import annotations

import random
import re
from typing import Any

from .dataset import EvalCase, EvalDataset

_NOISE_FILLERS = ("  ", "，", "？", "麻烦", "请帮忙")
_ENTITY_SLOT_HINTS = ("华南", "华北", "华东", "西南", "东北",
                      "美妆", "个护", "食品",
                      "直播", "门店", "电商")
_ENTITY_REPLACEMENTS = ("华东", "西北", "母婴", "线下")


def noise_variants(case: EvalCase, seed: int = 42) -> list[EvalCase]:
    """生成 3 条噪声变体：多空白 / 全角问号 / 前缀客套词。expected 原样保留。"""
    rng = random.Random(seed)
    variants = []
    base = case.input
    prefix = rng.choice(_NOISE_FILLERS[3:])              # 客套前缀
    forms = [
        f"  {base}",                                     # 前导空白
        f"{base}。谢谢",                                  # 尾部客套
        f"{prefix}{base}",                               # 前缀客套词
    ]
    for i, v in enumerate(forms):
        variants.append(EvalCase(
            id=f"{case.id}-noise{i + 1}",
            input=v,
            expected=case.expected,                       # 不变
            metadata={**(case.metadata or {}), "augmentation": f"noise{i + 1}",
                      "source_case": case.id},
            tags=list(case.tags) + ["augmented", "noise"],
            golden=case.golden,                            # 噪声不改变期望
        ))
    return variants


def entity_swap_candidates(case: EvalCase, seed: int = 42) -> list[EvalCase]:
    """实体替换候选：input 中的已知实体换新值——golden 置 None（待人工复核）。"""
    rng = random.Random(seed + 7)
    out = []
    found = [e for e in _ENTITY_SLOT_HINTS if e in case.input]
    for old in found:
        new = rng.choice([r for r in _ENTITY_REPLACEMENTS if r != old])
        swapped = case.input.replace(old, new)
        out.append(EvalCase(
            id=f"{case.id}-swap-{new}",
            input=swapped,
            expected=case.expected,                        # 占位：语义可能已变
            metadata={**(case.metadata or {}), "augmentation": f"entity_swap:{old}->{new}",
                      "source_case": case.id, "needs_review": True},
            tags=list(case.tags) + ["augmented", "needs_review"],
            golden=None,                                   # 系统不猜：待人工复核
        ))
    return out


def augment_dataset(ds: EvalDataset, noise: bool = True, entity_swap: bool = True,
                    seed: int = 42) -> EvalDataset:
    """对整个数据集做增强，返回只含变体的数据集（golden 标记见上）。"""
    cases: list[EvalCase] = []
    for c in ds.cases:
        if noise:
            cases.extend(noise_variants(c, seed=seed))
        if entity_swap:
            cases.extend(entity_swap_candidates(c, seed=seed))
    return EvalDataset(name=f"{ds.name}-augmented", version=ds.version, cases=cases)
