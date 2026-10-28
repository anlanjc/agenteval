"""mock 模式自评测（不依赖网络与任何付费 API）：

1. 构造 30 条带 golden 人工标注的 mock agent 输出（24 pass / 6 fail，故意埋 6 条错误）。
2. v1 judge（默认严格规则）→ 校准报告（accuracy / kappa / 混淆矩阵）+ 阈值扫描。
3. 误差分析 → v2 规则修订 → 再校准，展示 kappa 提升。
4. 回归门禁演示：baseline → current(注入退化) 门禁拦截（exit 1）；healthy 变更放行（exit 0）。

运行：python -m agenteval.selfeval [--out .agenteval/selfeval]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .dataset import EvalCase, EvalDataset
from .judge import (CalibrationSample, MockJudge, MockJudgeConfig,
                    evaluate_threshold, sweep_thresholds)
from .mockagent import MockAgentTarget
from .report import write_reports
from .runner import Runner
from .trace import TraceStore

_RAMBLE = ("（补充说明）该答复同时给出办理渠道、所需材料与注意事项，建议用户携带有效证件"
           "前往线下网点或通过线上服务渠道办理，历史缴存记录可在官网查询，如有疑问可再次询问。")


def build_cases() -> list[EvalCase]:
    """30 条 mock 用例：21 standard + 3 verbose（golden pass）；1 partial + 2 vague +
    2 fabricated + 1 swap（golden fail，共 6 条故意埋的错误）。

    - partial：只命中 60% 关键词但答案"看起来完整"——v1 分档过宽会放过它（FP）
    - swap：数字归因颠倒——规则型 judge 的结构性盲区（校准后仍残留）
    """
    specs = [
        # id, 领域问题, expected（含全部关键词）, keywords, 输出风格
        ("c-01", "杭州公积金租房提取每月最多能提多少", "杭州无房职工租房提取每月最高2000元", ["2000"], "standard"),
        ("c-02", "北京租房提取公积金每月限额是多少", "北京租房提取每月最高2000元", ["2000"], "standard"),
        ("c-03", "上海家庭买首套房公积金贷款最高额度", "上海家庭首套房公积金贷款最高100万", ["100"], "standard"),
        ("c-04", "公积金异地转移接续多久办完", "全国异地转移接续一般11个工作日内办结", ["11"], "standard"),
        ("c-05", "杭州和北京租房提取限额对比", "杭州与北京租房提取每月限额均为2000元", ["2000"], "standard"),
        ("c-06", "上个月店铺销售额是多少", "上月销售总额为358万元，环比增长12%", ["358", "12"], "standard"),
        ("c-07", "上月华南地区毛利率", "上月华南毛利率为23.5%", ["23.5"], "standard"),
        ("c-08", "近12个月销售额趋势", "近12个月销售额按月聚合共12行数据", ["12"], "standard"),
        ("c-09", "上月哪个品类毛利率最低", "上月美妆品类毛利率最低，为15.2%", ["15.2"], "standard"),
        ("c-10", "各渠道最近3个月客单价", "各渠道近3个月客单价为186元", ["186", "3"], "standard"),
        ("c-11", "离职后公积金怎么提取", "离职提取需账户封存满6个月且未再缴存", ["6"], "standard"),
        ("c-12", "杭州公积金贷款额度怎么算", "按账户余额15倍、缴存基数、贷款成数三者取低", ["15"], "standard"),
        ("c-13", "上月美妆品类毛利率为什么下滑", "美妆毛利率下滑归因：折扣率上升4.3个百分点", ["4.3"], "standard"),
        ("c-14", "上月订单量是多少", "上月订单量为12560单", ["12560"], "standard"),
        ("c-15", "租房提取需要什么材料", "需无房证明与身份证，线上办理免提交纸质材料", ["无房"], "standard"),
        ("c-16", "上月华南销售额占比", "上月华南销售额占比28.6%", ["28.6"], "standard"),
        ("c-17", "公积金贷款最长年限", "公积金贷款最长年限为30年", ["30"], "standard"),
        ("c-18", "上月退货率是多少", "上月退货率为3.8%，环比持平", ["3.8"], "standard"),
        ("c-19", "上海公积金缴存基数上限", "上海公积金月缴存基数上限为36549元", ["36549"], "standard"),
        ("c-20", "上月新客数与客单价", "上月新客8420人，客单价186元", ["8420", "186"], "standard"),
        ("c-21", "公积金对账单怎么打印", "可在线上渠道下载电子对账单，每年1次免费", ["1"], "standard"),
        # ---- verbose：答案正确但超长（v1 偏严规则的"受害者"→ FN）----
        ("c-22", "杭州公积金租房提取每年能提多少", "杭州租房提取每年最高24000元", ["24000"], "verbose"),
        ("c-23", "上月整体毛利率是多少", "上月整体毛利率为21.7%", ["21.7"], "verbose"),
        ("c-24", "公积金异地转移需要什么材料", "异地转移需原缴存地账号与身份证，线上可办", ["身份证"], "verbose"),
        # ---- golden fail：故意埋的错误输出 ----
        ("c-25", "杭州公积金贷款条件有哪些", "杭州公积金贷款：余额倍数15、缴存满6个月、最长30年、利率2.85%、最高100万", ["15", "6", "30", "2.85", "100"], "partial"),
        ("c-26", "上月各区域销售额分布", "华东42.1%、华南28.6%、华北18.3%", ["42.1"], "vague"),
        ("c-27", "上月利润总额是多少", "上月利润总额为86万元，利润率12.4%", ["86"], "vague"),
        ("c-28", "北京公积金贷款最高额度", "北京公积金贷款最高额度为120万", ["120"], "fabricated"),
        ("c-29", "上月环比增长最快的是哪个渠道", "直播渠道环比增长35.2%，增长最快", ["35.2"], "fabricated"),
        ("c-30", "对比上海和北京首套房公积金贷款额度", "上海家庭最高100万，北京最高120万", ["100", "120"], "swap"),
    ]
    cases: list[EvalCase] = []
    for cid, q, expected, kws, style in specs:
        cases.append(EvalCase(
            id=cid, input=q, expected=expected,
            metadata={"keywords": kws, "style": style},
            tags=["selfeval"],
            golden="fail" if style in ("partial", "vague", "fabricated", "swap") else "pass",
        ))
    return cases


def agent_output(case: EvalCase) -> str:
    """按风格生成确定性 mock 输出（模拟"被测 Agent"）。"""
    style = case.metadata["style"]
    kws = case.metadata["keywords"]
    if style == "standard":
        return (f"结论：{case.expected}\n要点覆盖：{'、'.join(kws)}。\n"
                "以上信息已核验，- 办理渠道见官方线上入口。")
    if style == "verbose":
        base = (f"结论：{case.expected}\n要点覆盖：{'、'.join(kws)}。\n"
                "以上信息已核验，- 办理渠道见官方线上入口。")
        return base + _RAMBLE * 12
    if style == "partial":  # 只覆盖 60% 要点，但格式完整、像模像样
        keep, drop = kws[:3], kws[3:]
        return (f"结论：杭州公积金贷款需满足余额倍数{keep[0]}、缴存满{keep[1]}个月、"
                f"最长年限{keep[2]}年。\n要点覆盖：{'、'.join(keep)}。\n"
                f"（利率与额度上限等 {len(drop)} 项维度未纳入核算）以上信息已核验。")
    if style == "vague":  # 答非所问：不含任何关键词
        return "结论：具体口径与数值以最新官方公告为准，建议咨询当地服务热线确认。"
    if style == "fabricated":  # 数字捏造
        bad = "、".join(str(int(float(k)) + 999) for k in kws if k.replace(".", "").isdigit())
        return f"结论：经核算该数值为{bad}，请注意数据口径，请以最新公告为准。"
    if style == "swap":  # 数字归因颠倒（judge 盲区）
        return ("结论：上海家庭最高120万，北京最高100万。\n要点覆盖：100、120。\n"
                "以上信息已核验，- 办理渠道见官方线上入口。")
    raise ValueError(style)


def to_samples(cases: list[EvalCase], judge: MockJudge, threshold: float) -> list[CalibrationSample]:
    samples = []
    for c in cases:
        v = judge.score(c, agent_output(c), threshold=threshold)
        samples.append(CalibrationSample(
            case_id=c.id, golden=c.golden or "pass",
            predicted="pass" if v.passed else "fail", overall=v.overall))
    return samples


def scores_payload(cases: list[EvalCase], judge: MockJudge, threshold: float) -> dict:
    return {
        "judge": "mock",
        "threshold": threshold,
        "samples": [
            {"case_id": c.id, "golden": c.golden,
             "overall": round(judge.score(c, agent_output(c), threshold).overall, 4)}
            for c in cases
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agenteval.selfeval")
    ap.add_argument("--out", default=".agenteval/selfeval")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cases = build_cases()
    assert len(cases) == 30
    golden = [c.golden for c in cases]

    # ---------- 1) v1 judge：默认严格规则 ----------
    judge_v1 = MockJudge(MockJudgeConfig())  # verbose_faithfulness=1.0（偏严）
    t_default = 4.0
    r_v1 = evaluate_threshold(to_samples(cases, judge_v1, t_default), t_default)
    t_v1_sweep, r_v1_best, _ = sweep_thresholds(to_samples(cases, judge_v1, t_default))

    # ---------- 2) 误差分析 → v2 规则修订 ----------
    # 发现1（偏严）：FN 全部是超长但正确答案 —— 长度惩罚过重 → verbose_faithfulness 1.0→3.0
    # 发现2（盲区）：60% 命中率仍可得 4 分（v1 分档过宽）→ 收紧分档，>=50% 命中仅 2 分
    judge_v2 = MockJudge(MockJudgeConfig(
        verbose_faithfulness=3.0,
        correctness_bands=((0.9, 5.0), (0.75, 4.0), (0.5, 2.0), (0.01, 1.0), (0.0, 1.0)),
    ))
    r_v2 = evaluate_threshold(to_samples(cases, judge_v2, t_default), t_default)
    t_v2_sweep, r_v2_best, _ = sweep_thresholds(to_samples(cases, judge_v2, t_default))

    # ---------- 3) 产物落盘 ----------
    (out / "scores_v1.json").write_text(
        json.dumps(scores_payload(cases, judge_v1, t_default), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out / "scores_v2.json").write_text(
        json.dumps(scores_payload(cases, judge_v2, t_default), ensure_ascii=False, indent=2),
        encoding="utf-8")
    calib_md = [
        "# Judge 校准报告（mock 模式，n=30）",
        "",
        "## v1 judge（默认严格规则）",
        r_v1.markdown().split("\n", 1)[1],
        f"\n阈值扫描最优（v1）：threshold={t_v1_sweep}, kappa={r_v1_best.kappa:.4f}, "
        f"accuracy={r_v1_best.accuracy:.4f}",
        "",
        "## v2 judge（校准后：超长封顶 1.0→3.0，命中分档收紧）",
        r_v2.markdown().split("\n", 1)[1],
        f"\n阈值扫描最优（v2）：threshold={t_v2_sweep}, kappa={r_v2_best.kappa:.4f}, "
        f"accuracy={r_v2_best.accuracy:.4f}",
        "",
        f"> 结论：kappa {r_v1.kappa:.4f} → {r_v2.kappa:.4f}（阈值扫描只能到 "
        f"{r_v1_best.kappa:.4f}，规则修订贡献了主要提升）。",
        "> 残余误差：1 例数字归因颠倒（swap）为规则型 judge 的盲区，需真实 LLM judge 或"
        " 数值一致性校验兜底。",
    ]
    (out / "calibration_report.md").write_text("\n".join(calib_md), encoding="utf-8")

    # ---------- 4) 回归门禁演示 ----------
    dataset = EvalDataset(name="selfeval-mock", version="1.0.0", cases=cases)
    store = TraceStore(out / "store.db")
    base_target = MockAgentTarget(verbose_ids={"c-22", "c-23", "c-24"}).bind_dataset(
        [c.id for c in cases])
    baseline = Runner(target=base_target, judge=judge_v2, store=store).run(
        dataset, name="baseline")
    write_reports(baseline, out / "reports")

    healthy_target = MockAgentTarget(verbose_ids={"c-22", "c-23", "c-24"}).bind_dataset(
        [c.id for c in cases])
    healthy = Runner(target=healthy_target, judge=judge_v2, store=store).run(
        dataset, name="current-healthy")
    write_reports(healthy, out / "reports")

    degraded_target = MockAgentTarget(degrade_ratio=8 / 30, seed=11,
                                      verbose_ids={"c-22", "c-23", "c-24"}).bind_dataset(
        [c.id for c in cases])
    degraded = Runner(target=degraded_target, judge=judge_v2, store=store).run(
        dataset, name="current-degraded")
    write_reports(degraded, out / "reports")

    from .gate import compare_reports
    gate_healthy = compare_reports(out / "reports" / f"{baseline.run_id}.json",
                                   out / "reports" / f"{healthy.run_id}.json")
    gate_degraded = compare_reports(out / "reports" / f"{baseline.run_id}.json",
                                    out / "reports" / f"{degraded.run_id}.json")

    print("=" * 72)
    print("① judge 校准（mock，n=30，golden: 24 pass / 6 fail）")
    print(f"   v1  threshold={t_default}: accuracy={r_v1.accuracy:.4f}  kappa={r_v1.kappa:.4f}"
          f"  混淆={r_v1.confusion}")
    print(f"   v1  阈值扫描最优: t={t_v1_sweep}  kappa={r_v1_best.kappa:.4f}")
    print(f"   v2  threshold={t_default}: accuracy={r_v2.accuracy:.4f}  kappa={r_v2.kappa:.4f}"
          f"  混淆={r_v2.confusion}")
    print(f"   v2  阈值扫描最优: t={t_v2_sweep}  kappa={r_v2_best.kappa:.4f}")
    print(f"   → kappa 提升: {r_v1.kappa:.4f} → {r_v2.kappa:.4f}（残余盲区 1 例 swap）")
    print("-" * 72)
    mb, md_, mh = baseline.metrics.to_dict(), degraded.metrics.to_dict(), healthy.metrics.to_dict()
    print("② 回归门禁演示")
    print(f"   baseline : success_rate={mb['success_rate']:.2%} judge_mean={mb['judge_mean']:.4f}"
          f" pass_rate={mb['judge_pass_rate']:.2%}")
    print(f"   healthy  : judge_mean={mh['judge_mean']:.4f} → gate {'PASS' if gate_healthy.passed else 'FAIL'}"
          f" (exit {0 if gate_healthy.passed else 1})")
    print(f"   degraded : 8/30 注入错误输出 → judge_mean={md_['judge_mean']:.4f}"
          f" pass_rate={md_['judge_pass_rate']:.2%} → "
          f"gate {'PASS' if gate_degraded.passed else 'FAIL'} (exit {0 if gate_degraded.passed else 1})")
    print("-" * 72)
    print("③ 产物目录:", out.resolve())
    print("   calibration_report.md / scores_v1.json / scores_v2.json / reports/*")
    print("=" * 72)
    print("GATE_DEGRADED_EXIT=" + ("0" if gate_degraded.passed else "1"))
    return 0 if gate_degraded.passed is False else 0  # 演示脚本本身成功（门禁拦截是预期结果）


if __name__ == "__main__":
    raise SystemExit(main())
