"""评测集管理：JSONL（id / input / expected / metadata / tags），加载/抽样/过滤/版本。"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class EvalCase:
    """一条评测用例。

    - id:          稳定唯一 id（回归对比按 id 对齐）
    - input:       发给被测 Agent 的输入（字符串；HTTP target 会按其协议包装）
    - expected:    期望答案/期望要点（人类可读；judge 用 metadata.keywords 做硬校验）
    - metadata:    任意附加信息（路由期望、gold_sql、角色……）
    - tags:        标签（smoke / regression / drill …），用于过滤
    - golden:      可选人工标注 "pass"/"fail"，校准模块使用
    """

    id: str
    input: str
    expected: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    golden: str | None = None  # "pass" | "fail" | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input,
            "expected": self.expected,
            "metadata": self.metadata,
            "tags": self.tags,
            "golden": self.golden,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvalCase":
        missing = [k for k in ("id", "input") if not d.get(k)]
        if missing:
            raise ValueError(f"EvalCase 缺少必填字段: {missing} in {d!r}")
        return cls(
            id=str(d["id"]),
            input=str(d["input"]),
            expected=str(d.get("expected", "")),
            metadata=dict(d.get("metadata") or {}),
            tags=list(d.get("tags") or []),
            golden=d.get("golden"),
        )


def extract_keywords(text: str, min_len: int = 2) -> list[str]:
    """从 expected 里自动抽关键词：数字串 + 2 字符以上中文/英文词。"""
    if not text:
        return []
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    words = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z_]{3,}", text)
    seen: list[str] = []
    for k in numbers + words:
        if k not in seen:
            seen.append(k)
    return seen[:12]


@dataclass
class EvalDataset:
    name: str
    version: str
    cases: list[EvalCase] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cases)

    # ---- 构造 --------------------------------------------------------------
    @classmethod
    def load_jsonl(cls, path: str | Path, name: str | None = None) -> "EvalDataset":
        p = Path(path)
        cases: list[EvalCase] = []
        version = "0"
        with p.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                d = json.loads(line)
                if "_version" in d and len(cases) == 0:
                    version = str(d["_version"])
                    continue
                cases.append(EvalCase.from_dict(d))
        return cls(
            name=name or p.stem,
            version=version,
            cases=cases,
        )

    def save_jsonl(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"_version": self.version}, ensure_ascii=False) + "\n")
            for c in self.cases:
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")

    # ---- 变换（返回新对象，保持原数据不可变语义） ---------------------------
    def filter(self, predicate: Callable[[EvalCase], bool] | None = None,
               tag: str | None = None) -> "EvalDataset":
        if tag is not None:
            predicate = lambda c: tag in c.tags  # noqa: E731
        if predicate is None:
            predicate = lambda c: True  # noqa: E731
        return EvalDataset(
            name=self.name,
            version=self.version,
            cases=[c for c in self.cases if predicate(c)],
        )

    def sample(self, n: int, seed: int = 42) -> "EvalDataset":
        """确定性抽样：固定 seed 保证回归可比。"""
        if n >= len(self.cases):
            return EvalDataset(name=self.name, version=self.version, cases=list(self.cases))
        rng = random.Random(seed)
        picked = sorted(rng.sample(range(len(self.cases)), n))
        return EvalDataset(
            name=self.name,
            version=self.version,
            cases=[self.cases[i] for i in picked],
        )

    def with_golden(self) -> "EvalDataset":
        return self.filter(lambda c: c.golden in ("pass", "fail"))

    def ids(self) -> list[str]:
        return [c.id for c in self.cases]
