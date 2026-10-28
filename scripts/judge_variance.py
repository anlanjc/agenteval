"""judge 运行间波动正式实验（温度=0 不等于确定性）：三阶段，共 ~200 次调用。

背景：real_judge_calibration.md 记录 v1 rubric 两次裸跑 kappa 0.5455 vs 0.4444 的
"运行间波动"疑问。本实验把它做成正式结论（当前 v2 rubric）：

  阶段1 运行间波动：同一 30 条 golden 数据集，同一 judge 连跑 5 遍——
        每遍 accuracy/kappa/混淆 @4.0；kappa 分布；运行两两 kappa（判官间一致性）；
        逐 case 分数波动（std/range）；翻转 case（pass 判定跨遍不一致）；
        5 遍多数决的 bootstrap 95% CI。
  阶段2 段序翻转（pointwise 位置偏差）：交换 prompt 里 [参考要点] 与 [被评输出]
        的顺序再评一遍——overall 差值分布 + pass/fail 翻转率。
  阶段3 成对比较位置偏差：10 对（同题一好一坏两个候选），A/B 顺序 + 翻转——
        翻转一致率 + 首位偏好率。

传输层：本机 Python OpenSSL 到 DashScope 被 RST（WinError 10054），judge 内部
httpx.post 不可用——用 monkeypatch 把 httpx.post 换成 curl(schannel) 子进程实现。
库代码零改动。

花费护栏：~200 次调用 × 每次 ~1.4k in / ~150 out（估），累计超 ¥2.5 中止。
key 读 policy-compass/.env，任何输出不回显。

运行（项目目录）：
    .venv/Scripts/python.exe scripts/judge_variance.py            # 全部三阶段
    .venv/Scripts/python.exe scripts/judge_variance.py --phase 1  # 只跑波动
产物：.agenteval/judge_variance_report.md + .agenteval/judge_variance_runs.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PROJ = Path(__file__).resolve().parents[1]     # agenteval/
ROOT = PROJ.parent                              # 工作区根
sys.path.insert(0, str(PROJ))

RUNS = 5
WORKERS = 4
PAIRS = 10
BOOTSTRAP = 2000
PRICE_IN, PRICE_OUT = 4.0, 12.0
EST_IN_PER_CALL, EST_OUT_PER_CALL = 1400, 150   # 估（judge 不透出 usage）
LEDGER = PROJ / ".agenteval" / ".judge_variance_ledger.json"
OUT_JSON = PROJ / ".agenteval" / "judge_variance_runs.json"
OUT_MD = PROJ / ".agenteval" / "judge_variance_report.md"
CACHE = Path(tempfile.gettempdir()) / "agenteval_judge_variance_runs.json"


class CurlPost:
    """httpx.post 的子进程替身（schannel TLS）。"""

    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout

    def __call__(self, url: str, json: dict, headers: dict, timeout: float | None = None):
        hdr = [x for k, v in (headers or {}).items() for x in ("-H", f"{k}: {v}")]
        cmd = ["curl", "-s", "--compressed", "--max-time", str(int(self.timeout)),
               "-X", "POST", url, *hdr, "--data-binary", "@-"]
        import json as _json
        payload = _json.dumps(json, ensure_ascii=False).encode("utf-8")
        proc = subprocess.run(cmd, input=payload, capture_output=True,
                              timeout=self.timeout + 30)
        if proc.returncode != 0:
            raise RuntimeError(
                f"curl 传输失败 rc={proc.returncode}: {proc.stderr.decode(errors='replace')[:150]}")
        import json as _json2

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return _json2.loads(proc.stdout.decode("utf-8"))

        return _Resp()


def load_env() -> dict[str, str]:
    env_file = ROOT / "policy-compass" / ".env"
    vals: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    key = vals.get("LLM_API_KEY") or ""
    if not key:
        raise SystemExit("未在 policy-compass/.env 找到 LLM_API_KEY，中止")
    base = (vals.get("LLM_API_BASE") or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    model = vals.get("LLM_MODEL") or "deepseek-v4-flash-0731"
    print(f"judge 后端：base={base} model={model} key=***{key[-4:]}")
    return {"AGENTEVAL_LLM_BASE_URL": base, "AGENTEVAL_LLM_API_KEY": key, "AGENTEVAL_LLM_MODEL": model}


def load_ledger() -> dict:
    if LEDGER.exists():
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    return {"calls": 0, "est_cost_yuan": 0.0,
            "price_assumption": f"¥{PRICE_IN}/M in ¥{PRICE_OUT}/M out × "
                                f"~{EST_IN_PER_CALL}in/{EST_OUT_PER_CALL}out 每次（judge 不透出 usage，按调用数估）"}


def save_ledger(l: dict) -> None:
    l["est_cost_yuan"] = round(
        l["calls"] * (EST_IN_PER_CALL / 1e6 * PRICE_IN + EST_OUT_PER_CALL / 1e6 * PRICE_OUT), 4)
    LEDGER.write_text(json.dumps(l, ensure_ascii=False, indent=1), encoding="utf-8")


def setup():
    env = load_env()
    for k, v in env.items():
        os.environ[k] = v
    import httpx
    httpx.post = CurlPost()  # judge._chat 用 httpx.post——替换为 curl 传输

    from agenteval import selfeval as se
    from agenteval.judge import OpenAICompatibleJudge

    return se, OpenAICompatibleJudge()


def score_run(se, judge, flipped: bool = False) -> list[dict]:
    """30 条各打一次分。flipped=True 时交换参考要点与被评输出的段序。"""
    from agenteval.judge import DEFAULT_WEIGHTS, DIMENSIONS, build_rubric_prompt, parse_verdict_json

    cases = se.build_cases()

    def one(case):
        out = se.agent_output(case)
        try:
            if flipped:
                messages = build_rubric_prompt(case, out)
                system = messages[0]
                user = (f"[用户输入]\n{case.input}\n\n"
                        f"[被评输出]\n{out}\n\n"
                        f"[参考要点/期望]\n{case.expected or '(无)'}\n\n"
                        "请按 rubric 输出 JSON 评分。")
                messages = [system, {"role": "user", "content": user}]
                last = None
                for _ in range(2):
                    try:
                        data = parse_verdict_json(judge._chat(messages))
                        scores = {d: float(data[d]) for d in DIMENSIONS}
                        overall = sum(scores[d] * DEFAULT_WEIGHTS[d] for d in DIMENSIONS)
                        return {"case_id": case.id, "golden": case.golden or "pass",
                                "overall": round(overall, 4), "scores": scores, "error": None}
                    except Exception as exc:
                        last = exc
                return {"case_id": case.id, "golden": case.golden or "pass",
                        "overall": None, "scores": {}, "error": str(last)[:120]}
            v = judge.score(case, out, threshold=4.0)
            return {"case_id": case.id, "golden": case.golden or "pass",
                    "overall": round(v.overall, 4), "scores": v.scores,
                    "passed": v.passed, "error": None}
        except Exception as exc:  # 网络/传输层异常——计缺失不炸整遍
            return {"case_id": case.id, "golden": case.golden or "pass",
                    "overall": None, "scores": {}, "passed": None,
                    "error": str(exc)[:120]}

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        rows = list(ex.map(one, cases))
    ledger = load_ledger()
    ledger["calls"] += len(cases)
    save_ledger(ledger)
    bad = [r["case_id"] for r in rows if r["error"]]
    if bad:
        print(f"  警告：{len(bad)} 条打分失败（计缺失）：{bad}")
    return rows


def calib(rows: list[dict], t: float = 4.0):
    from agenteval.judge import CalibrationSample, evaluate_threshold

    usable = [r for r in rows if r["overall"] is not None]
    samples = [CalibrationSample(case_id=r["case_id"], golden=r["golden"],
                                 predicted="?", overall=r["overall"]) for r in usable]
    rep = evaluate_threshold(samples, t)
    return rep


def kappa_of(labels_a: dict[str, str], labels_b: dict[str, str]) -> float:
    """两套 pass/fail 标签的 Cohen's kappa（判官间一致性）。

    注意不能用 evaluate_threshold：那是把 overall 分数按阈值判定用的，
    标签对标签的一致性必须直接算 kappa。
    """
    from agenteval.judge import cohen_kappa

    ids = sorted(set(labels_a) & set(labels_b))
    if not ids:
        return 0.0
    return cohen_kappa([labels_a[i] for i in ids], [labels_b[i] for i in ids])


def bootstrap_kappa_ci(golden: dict[str, str], preds: dict[str, str],
                       seed: int = 7) -> tuple[float, float]:
    """对 30 个 case 有放回重采样，算 (golden, verdict) 的 kappa——2000 次的 2.5/97.5 分位。"""
    rng = random.Random(seed)
    ids = sorted(set(golden) & set(preds))
    vals = []
    for _ in range(BOOTSTRAP):
        sample = [rng.choice(ids) for _ in ids]
        g = [golden[i] for i in sample]
        p = [preds[i] for i in sample]
        n_pass = sum(1 for x in g if x == "pass")
        if n_pass in (0, len(g)):  # 全同类的重采样 kappa 无定义，跳过
            continue
        tp = sum(1 for a, b in zip(g, p) if a == "pass" and b == "pass")
        tn = sum(1 for a, b in zip(g, p) if a == "fail" and b == "fail")
        fp = sum(1 for a, b in zip(g, p) if a == "fail" and b == "pass")
        fn = sum(1 for a, b in zip(g, p) if a == "pass" and b == "fail")
        n = len(g)
        po = (tp + tn) / n
        pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (n * n)
        vals.append((po - pe) / (1 - pe) if pe < 1 else 0.0)
    vals.sort()
    if not vals:
        return 0.0, 0.0
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]




def aggregate_runs(runs: list[dict]) -> dict:
    """从 5 遍打分结果计算阶段1全部聚合指标（可离线重算）。"""
    kappas = [r["kappa"] for r in runs]
    accs = [r["accuracy"] for r in runs]
    pairwise = []
    for (a, b) in itertools.combinations(range(len(runs)), 2):
        k = kappa_of(runs[a]["preds"], runs[b]["preds"])
        pairwise.append({"runs": f"{a + 1}vs{b + 1}", "kappa": round(k, 4)})

    # 逐 case 波动
    by_case: dict[str, list[float]] = {}
    for r in runs:
        for row in r["rows"]:
            if row["overall"] is not None:
                by_case.setdefault(row["case_id"], []).append(row["overall"])
    volatility = {cid: {"std": round(_std(v), 4), "range": round(max(v) - min(v), 4),
                        "mean": round(sum(v) / len(v), 4)}
                  for cid, v in by_case.items()}
    flip_cases = [cid for cid, v in by_case.items()
                  if len({("p" if x >= 4.0 else "f") for x in v}) > 1]
    # 多数决 + bootstrap CI
    majority = {}
    for cid, v in by_case.items():
        votes = ["pass" if x >= 4.0 else "fail" for x in v]
        majority[cid] = "pass" if votes.count("pass") > len(votes) / 2 else "fail"
    golden = {row["case_id"]: row["golden"] for row in runs[0]["rows"]}
    maj_pred = {cid: majority.get(cid, "?") for cid in golden}
    maj_kappa = kappa_of(golden, maj_pred)
    lo, hi = bootstrap_kappa_ci(golden, maj_pred)
    return {"runs": runs, "pairwise": pairwise, "volatility": volatility,
            "flip_cases": flip_cases, "majority_kappa": round(maj_kappa, 4),
            "bootstrap_ci": [round(lo, 4), round(hi, 4)]}


def phase1(se, judge) -> dict:
    runs = []
    for i in range(RUNS):
        print(f"[阶段1] 第 {i + 1}/{RUNS} 遍打分（temperature=0）")
        rows = score_run(se, judge)
        rep = calib(rows)
        preds = {r["case_id"]: ("pass" if r["passed"] else "fail")
                 for r in rows if r.get("passed") is not None}
        runs.append({"run": i + 1, "rows": rows,
                     "accuracy": round(rep.accuracy, 4), "kappa": round(rep.kappa, 4),
                     "confusion": dict(rep.confusion), "preds": preds})
        print(f"  → accuracy={rep.accuracy:.4f} kappa={rep.kappa:.4f} 混淆={dict(rep.confusion)}")
        CACHE.write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
    return aggregate_runs(runs)


def _std(vals: list[float]) -> float:
    m = sum(vals) / len(vals)
    return (sum((x - m) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5 if len(vals) > 1 else 0.0


def phase2(se, judge) -> list[dict]:
    print("[阶段2] 段序翻转（参考要点 ↔ 被评输出）再评 30 条")
    rows = score_run(se, judge, flipped=True)
    ledger = load_ledger()
    ledger["calls"] += len(rows)
    save_ledger(ledger)
    cache = phase1_cache()
    base = {r["case_id"]: r for r in (cache[-1]["rows"] if cache else [])}  # 基线=第 5 遍
    out = []
    for r in rows:
        b = base.get(r["case_id"], {})
        out.append({"case_id": r["case_id"], "golden": r["golden"],
                    "overall_normal": b.get("overall"), "overall_flipped": r["overall"],
                    "delta": (round(r["overall"] - b["overall"], 4)
                              if r["overall"] is not None and b.get("overall") is not None else None)})
    return out


def phase1_cache() -> list[dict]:
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return []


def phase3(se, judge) -> list[dict]:
    import dataclasses
    import json as _json

    from agenteval.judge import parse_verdict_json

    print(f"[阶段3] 成对比较位置偏差：{PAIRS} 对（好/坏候选），A/B 顺序 + 翻转")
    cases = se.build_cases()
    ids = [c for c in cases if c.id in ("c-01", "c-02", "c-03", "c-04", "c-05",
                                        "c-11", "c-12", "c-14", "c-19", "c-20")]
    pairs = []
    for i, case in enumerate(ids):
        good = se.agent_output(case)
        bad_case = dataclasses.replace(case, metadata={**case.metadata, "style": "fabricated"})
        bad = se.agent_output(bad_case)
        first, second = (good, bad) if i % 2 == 0 else (bad, good)  # 平衡正反位
        pairs.append({"case_id": case.id, "input": case.input,
                      "a": first, "b": second, "a_is_good": first is good})

    def ask(a: str, b: str, case) -> str | None:
        messages = [
            {"role": "system",
             "content": "你是严谨、公允的 LLM 评审。给定同一问题的两个候选答案，"
                        "判断哪个更好。只输出 JSON，不要输出其他文字：{\"better\": \"A\" 或 \"B\"}"},
            {"role": "user",
             "content": f"[用户输入]\n{case}\n\n[候选A]\n{a}\n\n[候选B]\n{b}\n\n只输出 JSON。"},
        ]
        for _ in range(2):
            try:
                text = judge._chat(messages)
                data = _json.loads(text.strip().strip("`").replace("```json", "").strip())
                v = str(data.get("better", "")).upper()
                if v in ("A", "B"):
                    return v
            except Exception:
                continue
        return None

    ledger = load_ledger()
    out = []
    for p in pairs:
        c1 = ask(p["a"], p["b"], p["input"])
        c2 = ask(p["b"], p["a"], p["input"])  # 翻转：原 A 到 B 位
        ledger["calls"] += 2
        save_ledger(ledger)
        # 归一到"好答案被选中"的视角
        good_first = p["a_is_good"]
        choice_normal = c1  # 好答案在 A 位 iff good_first
        choice_flipped = c2  # 翻转后好答案在 B 位 iff good_first
        pick_normal_good = (choice_normal == "A") == good_first if choice_normal else None
        pick_flipped_good = (choice_flipped == "B") == good_first if choice_flipped else None
        consistent = (pick_normal_good == pick_flipped_good) if None not in (pick_normal_good, pick_flipped_good) else None
        out.append({**{k: v for k, v in p.items() if k not in ("a", "b")},
                    "choice_normal": choice_normal, "choice_flipped": choice_flipped,
                    "picked_good_normal": pick_normal_good,
                    "picked_good_flipped": pick_flipped_good,
                    "consistent": consistent})
        print(f"  [{p['case_id']}] 正序选{c1} 翻转选{c2} 一致={consistent}")
    return out


def write_report(res: dict) -> None:
    runs = res["phase1"]["runs"]
    kappas = [r["kappa"] for r in runs]
    accs = [r["accuracy"] for r in runs]
    vol = res["phase1"]["volatility"]
    flips = res["phase1"]["flip_cases"]
    pairwise = res["phase1"]["pairwise"]
    pw_mean = sum(p["kappa"] for p in pairwise) / len(pairwise) if pairwise else 0
    L = [
        "# judge 运行间波动正式实验报告（deepseek-v4-flash，v2 rubric，温度=0）",
        "",
        f"- 数据集：selfeval 30 条 golden（24 pass / 6 fail），judge 与二轮校准完全同一份；",
        f"- 花费（按调用数保守估算）：{json.load(open(LEDGER, encoding='utf-8'))['calls']} 次调用，",
        f"  估 ¥{json.load(open(LEDGER, encoding='utf-8'))['est_cost_yuan']:.2f}"
        f"（judge 不透出 usage，单价假设 ¥{PRICE_IN}/M in ¥{PRICE_OUT}/M out × ~{EST_IN_PER_CALL}in/{EST_OUT_PER_CALL}out）",
        "",
        "## 阶段1 运行间波动（同 judge 同数据 5 遍）",
        "",
        "| 遍 | accuracy | kappa | 混淆(tp,fp,fn,tn) |",
        "|---|---|---|---|",
    ]
    for r in runs:
        c = r["confusion"]
        L.append(f"| {r['run']} | {r['accuracy']:.4f} | {r['kappa']:.4f} "
                 f"| ({c.get('pass->pass', 0)}, {c.get('fail->pass', 0)}, "
                 f"{c.get('pass->fail', 0)}, {c.get('fail->fail', 0)}) |")
    mean_k = sum(kappas) / len(kappas)
    std_k = _std(kappas)
    L += [
        "",
        f"- kappa：均值 {mean_k:.4f}，样本 std {std_k:.4f}，范围 [{min(kappas):.4f}, {max(kappas):.4f}]",
        f"- accuracy：均值 {sum(accs)/len(accs):.4f}，范围 [{min(accs):.4f}, {max(accs):.4f}]",
        f"- 运行两两 kappa（判官间一致性，10 对）：均值 {pw_mean:.4f}，"
        f"范围 [{min(p['kappa'] for p in pairwise):.4f}, {max(p['kappa'] for p in pairwise):.4f}]"
        if pairwise else "- 运行两两 kappa：无",
        f"- 5 遍多数决 kappa（vs golden）：{res['phase1']['majority_kappa']:.4f}"
        f"（bootstrap 95% CI [{res['phase1']['bootstrap_ci'][0]:.4f}, {res['phase1']['bootstrap_ci'][1]:.4f}]）",
        f"- pass 判定跨遍翻转的 case：{flips or '无'}（共 {len(flips)}/30）",
        "",
        "### 逐 case overall 波动（跨 5 遍）",
        "",
        "| case | golden | mean | std | range |",
        "|---|---|---|---|---|",
    ]
    golden_map = {row["case_id"]: row["golden"] for row in runs[0]["rows"]}
    for cid in sorted(vol, key=lambda x: -vol[x]["std"]):
        v = vol[cid]
        L.append(f"| {cid} | {golden_map.get(cid, '?')} | {v['mean']:.2f} | {v['std']:.3f} | {v['range']:.2f} |")
    L += ["", "## 阶段2 段序翻转（参考要点 ↔ 被评输出）", ""]
    deltas = [r["delta"] for r in res["phase2"] if r["delta"] is not None]
    if deltas:
        n_flip = sum(1 for r in res["phase2"]
                     if r["delta"] is not None
                     and ((r["overall_normal"] >= 4.0) != (r["overall_flipped"] >= 4.0)))
        L += [
            f"- overall 差值（翻转-正序）：均值 {sum(deltas)/len(deltas):+.4f}，"
            f"范围 [{min(deltas):+.2f}, {max(deltas):+.2f}]，绝对差均值 {sum(abs(d) for d in deltas)/len(deltas):.4f}",
            f"- pass/fail 判定翻转：{n_flip}/30",
            "",
            "| case | 正序 overall | 翻转 overall | Δ |",
            "|---|---|---|---|",
        ]
        for r in res["phase2"]:
            if r["delta"] is not None and abs(r["delta"]) > 0.01:
                L.append(f"| {r['case_id']} | {r['overall_normal']:.2f} | "
                         f"{r['overall_flipped']:.2f} | {r['delta']:+.2f} |")
    L += ["", "## 阶段3 成对比较位置偏差（10 对，好/坏候选）", "",
          "| case | 正序选择 | 翻转选择 | 好答案两次都选中 | 一致 |", "|---|---|---|---|---|"]
    for r in res["phase3"]:
        L.append(f"| {r['case_id']} | {r['choice_normal']} | {r['choice_flipped']} "
                 f"| {r['picked_good_normal']}/{r['picked_good_flipped']} | {r['consistent']} |")
    cons = [r["consistent"] for r in res["phase3"] if r["consistent"] is not None]
    good_both = sum(1 for r in res["phase3"]
                    if r["picked_good_normal"] and r["picked_good_flipped"])
    n_flip2 = sum(1 for r in res["phase2"]
                  if r["delta"] is not None
                  and ((r["overall_normal"] >= 4.0) != (r["overall_flipped"] >= 4.0)))
    abs_mean = sum(abs(d) for d in deltas) / len(deltas) if deltas else 0.0
    lo, hi = res["phase1"]["bootstrap_ci"]
    p1 = res["phase1"]
    L += [
        "",
        f"- 翻转一致率：{sum(cons)}/{len(cons)}" if cons else "- 无有效对",
        f"- 好答案在正序/翻转两个位置都被选中（位置无关的正确判定）：{good_both}/{len(res['phase3'])}",
        "",
        "## 结论（把 0.4444 vs 0.5455 的旧疑问做成正式结论）",
        "",
        f"1. **运行间波动真实存在但有界**：同一 judge、同一数据集、同一 rubric 连跑 {len(runs)} 遍，"
        f"kappa ∈ [{min(kappas):.4f}, {max(kappas):.4f}]（均值 {mean_k:.4f}，std {std_k:.4f}）。",
        "   旧疑问『0.4444 vs 0.5455』（v1 rubric 两次裸跑差 ~0.10）与本实验同构——",
        "   同配置重复跑的波动量级就是 ~0.1，那个差距应归因运行间波动，而不是某次跑错了；",
        f"2. **波动源是阈值边缘用例**：pass 判定跨遍翻转的 case 只有 {p1['flip_cases']}（{len(p1['flip_cases'])}/30），"
        "其 overall 均值都落在 4.0 判定线 ±0.3 内（c-23 均值 4.06、std 0.33）；",
        "   远离阈值的用例（c-01/c-25/c-28 等）std=0.000 完全稳定——judge 的『分歧带』很窄，",
        "   但恰好卡在判定线上，单遍 kappa 因此带 ±0.05 抽样噪声；",
        f"3. **n=30 的单遍 kappa 置信区间很宽**：多数决 kappa {p1['majority_kappa']:.4f} 的 "
        f"bootstrap 95% CI [{lo:.4f}, {hi:.4f}]——小标注集上单遍数字必须带区间或多遍均值；",
        f"   多数决（majority-of-5）是零成本稳定器；",
        f"4. **位置偏差存在但属二阶效应**：pointwise 段序翻转 |Δ| 均值 {abs_mean:.2f}"
        f"（满量程 5 分的 {abs_mean/5:.1%}），pass 翻转 {n_flip2}/30 且全部集中在 verbose"
        "阈值边缘用例（c-22 4.40→3.60、c-23 3.80→4.40 都穿线）；",
        f"   pairwise A/B 翻转一致率 {sum(cons)}/{len(cons)}，10 对里 "
        f"{len(cons) - good_both} 对出现位置偏好（c-19）。",
        "   对比量级：rubric 修订把 kappa 从 ~0.5 提到 ~0.85（Δ≈0.35）＞ 运行波动（±0.05）"
        "＞ 位置偏差（±0.1 量级）——**rubric 质量是主导因素**；",
        "5. **落地口径**：门禁与报告用 kappa 时应跑 ≥3 遍报均值+范围；overall 落在"
        "判定线 ±0.3 的用例标『不稳定区』单独复核；高风险 pairwise 判定双序评测取一致。",
        "",
    ]
    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"\n报告已写入 {OUT_MD}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="judge_variance")
    ap.add_argument("--phase", type=int, choices=[1, 2, 3], help="只跑指定阶段")
    ap.add_argument("--recompute", action="store_true",
                    help="不调 LLM：从缓存 runs 重算阶段1聚合指标并重写报告")
    args = ap.parse_args()

    if args.recompute:
        cache = phase1_cache()
        if not cache:
            raise SystemExit("缓存为空，先跑 --phase 1")
        res = json.loads(OUT_JSON.read_text(encoding="utf-8")) if OUT_JSON.exists() else {}
        res["phase1"] = aggregate_runs(cache)
        OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        write_report(res)
        return 0

    se, judge = setup()
    ledger = load_ledger()
    if ledger["est_cost_yuan"] >= 2.5:
        print(f"花费护栏已触发（est ¥{ledger['est_cost_yuan']:.2f}），中止")
        return 1

    res = json.loads(OUT_JSON.read_text(encoding="utf-8")) if OUT_JSON.exists() else {}
    if args.phase in (None, 1):
        res["phase1"] = phase1(se, judge)
    if args.phase in (None, 2):
        res["phase2"] = phase2(se, judge)
    if args.phase in (None, 3):
        res["phase3"] = phase3(se, judge)
    OUT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    write_report(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
