"""构建 smoke 数据集 v2.0.0（10→30 条，对齐两主项目各自 v2 评测集的代表性子集）。

数据来源：对两主项目 mock 模式服务全量探测的基线输出（见 evidence/ci_gate_demo.md 构建过程）。
- expected = 基线正确输出快照（mock 口径下答案确定，快照即金标；faithfulness 靠它核对数字）
- metadata.keywords = 语义锚点（人工挑选，脚本断言全部出现在基线输出中）
- metadata.source_id = 主项目 v2 评测集原条目 id（可追溯）

用法：python scripts/build_smoke_v2.py --probe-askbi probe_askbi.json --probe-pc probe_pc.json
探测方式（需先起两主项目 mock 服务）见 docs/ci_integration.md。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DATASETS = HERE / "agenteval" / "datasets"

# ---------------------------------------------------------------- askbi 选例
# (source_id, keywords, role)——keywords 必须逐个出现在该条基线输出中
ASKBI_SEL: list[tuple[str, list[str], str]] = [
    # query ×17（含 south/north 行级权限各 1）
    ("v2-001", ["销售额", "3,988,676.33"], "boss"),
    ("v2-002", ["订单量", "4,593"], "boss"),
    ("v2-004", ["客单价", "868.43"], "boss"),
    ("v2-006", ["毛利率", "31.93"], "boss"),
    ("v2-015", ["category", "销售额", "共 6 行"], "boss"),
    ("v2-017", ["channel", "订单量", "共 5 行"], "boss"),
    ("v2-021", ["channel", "毛利率", "共 5 行"], "boss"),
    ("v2-033", ["channel", "共 3 行"], "boss"),
    ("v2-041", ["month", "销售额", "共 6 行"], "boss"),
    ("v2-042", ["month", "订单量", "共 12 行"], "boss"),
    ("v2-047", ["month", "销量", "共 12 行"], "boss"),
    ("v2-051", ["销售额", "1,171,401.49"], "boss"),
    ("v2-053", ["订单量", "3,244"], "boss"),
    ("v2-057", ["销量", "26,102"], "boss"),
    ("v2-058", ["净销售额", "1,066,438.95"], "boss"),
    ("v2-072", ["销售额", "1,171,401.49", "华南"], "south"),
    ("v2-076", ["订单量", "979", "华北"], "north"),
    # drill ×4（mock 口径下归因题返回对应指标值/明细）
    ("v2-081", ["毛利率", "44.02"], "boss"),
    ("v2-083", ["销售额", "2,792,581.61"], "boss"),
    ("v2-085", ["退货率", "3.11"], "boss"),
    ("v2-086", ["category", "毛利率", "共 6 行"], "boss"),
    # clarify ×5（mock 澄清话术固定）
    ("v2-093", ["哪个指标"], "boss"),
    ("v2-095", ["哪个指标"], "boss"),
    ("v2-096", ["哪个指标"], "boss"),
    ("v2-101", ["哪个指标"], "boss"),
    ("v2-103", ["哪个指标"], "boss"),
    # refusal ×4（预测 / 指标不存在 ×2 / 非经营类）
    ("v2-105", ["2026-09", "无法预测"], "boss"),
    ("v2-109", ["不存在该指标"], "boss"),
    ("v2-112", ["不存在该指标"], "boss"),
    ("v2-114", ["不是经营数据查询"], "boss"),
]

# ---------------------------------------------------------------- policy-compass 选例
PC_SEL: list[tuple[int, list[str]]] = [
    # fact ×11（覆盖 7 城 + 全国通用）
    (105, ["直付房租", "试点", "2025年1月10日"]),
    (106, ["4000", "提取限额", "市场租赁住房"]),
    (110, ["80万", "160万"]),
    (112, ["68岁", "退休年龄后5年"]),
    (113, ["百分之六十五", "第十一条"]),
    (115, ["实际月租金", "公共租赁住房"]),
    (118, ["1800", "2.16万"]),
    (120, ["32969"]),
    (121, ["5%", "不得低于"]),
    (124, ["3年内不得提取", "5年内不得提取"]),
    (125, ["第四十九条", "自愿缴存"]),
    # compare ×4（mock 检索 on-topic 的城市场景）
    # PC-3（2026-11-02）重探重建：沪杭贷款真实化+检索扩展机制修复后，
    # 旧锚点（×10倍/×15倍/50万）是已替换的演示口径——新锚点全部取自
    # 现行真实文件数字（沪管委会〔2026〕1号 100/200万、穗〔2024〕2号等）
    (129, ["100万元", "200万元", "房价成数"]),
    (134, ["按实际月租金提取", "1500", "2000"]),
    (135, ["200万", "100万", "80万", "160万"]),
    (140, ["2000", "穗公积金中心规字〔2023〕1号", "杭房公委〔2025〕4号"]),
    # calc ×4（mock 返回规则条文/工具结果）
    (145, ["100万", "200万", "房价成数"]),
    (147, ["缴存年限挂钩", "月收入60%", "2.6%"]),
    (153, ["缴存年限挂钩", "月收入60%"]),
    (155, ["15倍调整为20倍", "90万"]),
    # refuse ×4（界外拒答，零编造）
    (158, ["不做猜测性回答", "12329"]),
    (160, ["不做猜测性回答", "12329"]),
    (161, ["不做猜测性回答", "12329"]),
    (162, ["不做猜测性回答", "12329"]),
    # timeliness ×3（时效演进）
    (170, ["2025年12月15日"]),
    (174, ["1500", "2000"]),
    (175, ["2000", "广州"]),
    # slang ×3（黑话改写）
    (176, ["购房资格", "断缴"]),
    (178, ["封存满6个月", "销户"]),
    (182, ["账随人走", "转移接续"]),
    # multi ×1（多跳）
    (185, ["满3个月", "无自有住房"]),
]


def build(probe_path: Path, sel: list, id_prefix: str, kind_key: str) -> list[dict]:
    probe = {o["id"]: o for o in json.loads(probe_path.read_text(encoding="utf-8"))}
    cases: list[dict] = []
    problems: list[str] = []
    for i, entry in enumerate(sel, 1):
        source_id, keywords = entry[0], entry[1]
        role = entry[2] if len(entry) > 2 else None
        o = probe[source_id]
        if o["err"]:
            problems.append(f"{id_prefix}-{i:03d} ({source_id}): 探测输出带 error {o['err']}")
            continue
        missing = [k for k in keywords if k not in o["out"]]
        if missing:
            problems.append(f"{id_prefix}-{i:03d} ({source_id}): keywords 不在输出中 {missing}")
        if len(o["out"]) > 900:
            problems.append(f"{id_prefix}-{i:03d} ({source_id}): 输出 {len(o['out'])} 字超 MockJudge 900 上限")
        meta: dict = {"kind" if kind_key == "kind" else "type": o[kind_key],
                      "source_id": source_id, "keywords": keywords}
        if role:
            meta["role"] = role
        cases.append({
            "id": f"{id_prefix}-{i:03d}",
            "input": o["q"],
            "expected": o["out"],
            "metadata": meta,
            "tags": ["smoke", o[kind_key]],
        })
    if problems:
        raise SystemExit("构建断言失败：\n" + "\n".join(problems))
    return cases


def write_jsonl(path: Path, cases: list[dict], version: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"_version": version}, ensure_ascii=False) + "\n")
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-askbi", default=str(HERE / "evidence" / "ci_gate_probe_askbi.json"))
    ap.add_argument("--probe-pc", default=str(HERE / "evidence" / "ci_gate_probe_pc.json"))
    args = ap.parse_args()

    ab = build(Path(args.probe_askbi), ASKBI_SEL, "ab", "kind")
    pc = build(Path(args.probe_pc), PC_SEL, "pc", "type")
    write_jsonl(DATASETS / "askbi_smoke.jsonl", ab, "2.0.0")
    write_jsonl(DATASETS / "policy_compass_smoke.jsonl", pc, "2.0.0")
    for name, cases in (("askbi_smoke", ab), ("policy_compass_smoke", pc)):
        kinds: dict[str, int] = {}
        for c in cases:
            k = c["tags"][1]
            kinds[k] = kinds.get(k, 0) + 1
        print(f"{name}: {len(cases)} 条  分层={kinds}")


if __name__ == "__main__":
    main()
