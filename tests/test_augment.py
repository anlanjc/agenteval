"""评测集增强器测试：噪声变体（golden 保留）、实体替换候选（golden 置空）、确定性、过滤。"""
import json

from agenteval.augment import (
    augment_dataset,
    entity_swap_candidates,
    noise_variants,
)
from agenteval.dataset import EvalCase, EvalDataset


def make_case(**over):
    base = dict(id="c1", input="上月华南地区的毛利率是多少？", expected="32%",
                metadata={"category": "query"}, tags=["smoke"], golden="pass")
    base.update(over)
    return EvalCase(**base)


def test_noise_variants_keep_expected():
    vs = noise_variants(make_case())
    assert len(vs) == 3
    for v in vs:
        assert v.expected == "32%"
        assert v.golden == "pass"
        assert v.metadata["source_case"] == "c1"
        assert "noise" in v.tags


def test_noise_variants_actually_perturb():
    vs = noise_variants(make_case())
    assert all(v.input != make_case().input for v in vs)   # 至少真变了
    assert len({v.input for v in vs}) == 3                 # 三条互不相同


def test_entity_swap_marks_needs_review():
    cands = entity_swap_candidates(make_case())
    assert cands, "应至少生成一个替换候选"
    for c in cands:
        assert c.golden is None                            # 系统不猜 expected
        assert c.metadata["needs_review"] is True
        assert c.input != make_case().input


def test_augment_dataset_combined(tmp_path):
    ds = EvalDataset(name="demo", version="v1", cases=[
        make_case(),
        make_case(id="c2", input="上月华东地区的销售额是多少？", expected="120万", golden="pass"),
    ])
    aug = augment_dataset(ds, noise=True, entity_swap=True, seed=7)
    expected_total = sum(3 + len(entity_swap_candidates(c)) for c in ds.cases)
    assert len(aug.cases) == expected_total
    # 无 golden 的候选会被 with_golden 过滤——自动评测天然排除待复核项
    auto = aug.with_golden()
    assert all(c.golden in ("pass", "fail") for c in auto.cases)


def test_seed_determinism():
    a = noise_variants(make_case(), seed=7)
    b = noise_variants(make_case(), seed=7)
    assert [v.input for v in a] == [v.input for v in b]
    a2 = entity_swap_candidates(make_case(), seed=7)
    b2 = entity_swap_candidates(make_case(), seed=7)
    assert [v.input for v in a2] == [v.input for v in b2]


def test_save_load_augmented_dataset(tmp_path):
    ds = EvalDataset(name="demo", version="v1", cases=[make_case()])
    aug = augment_dataset(ds, noise=True, entity_swap=True)
    f = tmp_path / "aug.jsonl"
    aug.save_jsonl(f)
    loaded = EvalDataset.load_jsonl(f)
    assert len(loaded.cases) == len(aug.cases)
    # golden=None 的行保存/加载后仍是 None（待复核状态不丢）
    assert any(c.golden is None for c in loaded.cases)
