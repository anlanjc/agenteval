"""dataset.py 测试：JSONL 往返、过滤、确定性抽样、关键词抽取、字段校验。"""

import pytest

from agenteval.dataset import EvalCase, EvalDataset, extract_keywords


def _write(tmp_path, lines):
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_jsonl_roundtrip(tmp_path):
    p = _write(tmp_path, [
        '{"_version": "2.1"}',
        '{"id":"c1","input":"q1","expected":"e1","metadata":{"k":1},"tags":["a"],"golden":"pass"}',
        '{"id":"c2","input":"q2","expected":"e2","tags":["b"],"golden":"fail"}',
    ])
    ds = EvalDataset.load_jsonl(p)
    assert ds.version == "2.1"
    assert len(ds) == 2
    assert ds.cases[0].metadata == {"k": 1}
    out = tmp_path / "out.jsonl"
    ds.save_jsonl(out)
    ds2 = EvalDataset.load_jsonl(out)
    assert ds2.version == "2.1"
    assert ds2.ids() == ["c1", "c2"]
    assert ds2.cases[0].golden == "pass"


def test_filter_by_tag_and_predicate():
    ds = EvalDataset(name="t", version="1", cases=[
        EvalCase("a", "q", tags=["smoke"]),
        EvalCase("b", "q", tags=["deep"]),
        EvalCase("c", "q", tags=["smoke", "deep"]),
    ])
    assert ds.filter(tag="smoke").ids() == ["a", "c"]
    assert ds.filter(lambda c: "deep" in c.tags).ids() == ["b", "c"]
    # 原数据不变（过滤返回新对象）
    assert len(ds) == 3
    assert ds.with_golden().cases == []  # 无 golden 标注


def test_sample_deterministic():
    cases = [EvalCase(f"c{i:02d}", "q") for i in range(10)]
    ds = EvalDataset(name="t", version="1", cases=cases)
    s1 = ds.sample(5, seed=42)
    s2 = ds.sample(5, seed=42)
    assert s1.ids() == s2.ids()
    assert len(s1) == 5
    assert sorted(s1.ids()) == sorted(s1.ids())
    # n >= len → 全量
    assert len(ds.sample(99)) == 10
    assert len(ds.sample(10)) == 10


def test_extract_keywords():
    kws = extract_keywords("每月最高2000元，上限24000元")
    assert "2000" in kws and "24000" in kws
    kws2 = extract_keywords("支持无房证明办理 process_id")
    assert any("无房证明办理" in k for k in kws2)
    assert "process_id" in kws2
    assert extract_keywords("") == []


def test_case_validation():
    with pytest.raises(ValueError, match="缺少必填字段"):
        EvalCase.from_dict({"id": "x"})
    with pytest.raises(ValueError, match="缺少必填字段"):
        EvalCase.from_dict({"input": "q"})
    c = EvalCase.from_dict({"id": "ok", "input": "q"})
    assert c.tags == [] and c.metadata == {} and c.golden is None
