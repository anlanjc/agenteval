"""CLI 数据集解析回归：--dataset 任意 JSONL 路径（AE-1）与两层白名单语义。

AE-1 修复（评审 4.0）：AGT-2 数据集白名单（8a503eb）越界杀伤 CLI——cli._resolve_dataset
不传 extra_roots，builtin 目录外任意 JSONL 路径一律 HTTPException 422 且以未捕获
traceback 退出，而 --dataset help 明确承诺「JSONL 路径」。修法：CLI 是可信本地方，
路径 spec 以其所在目录作 extra_root 放行；API 服务端白名单不放松（两层语义分开）。
"""

from __future__ import annotations

import json

import pytest

from agenteval.cli import main as cli_main


def _write_jsonl(path, n=3):
    path.write_text(
        "\n".join(json.dumps({"id": f"ext-{i}", "input": f"问题{i}", "expected": "答案{i}"})
                  for i in range(n)) + "\n", encoding="utf-8")
    return path


def test_cli_run_accepts_jsonl_path_outside_builtin(tmp_path):
    """builtin 目录外的 JSONL 路径经 CLI 可跑（help 承诺的核心输入形态）。"""
    ds = _write_jsonl(tmp_path / "my_eval.jsonl")
    out = tmp_path / "reports"
    rc = cli_main([
        "run", "--dataset", str(ds), "--target", "mock", "--judge", "mock",
        "--db", str(tmp_path / "store.db"), "--out", str(out), "--name", "ext-jsonl",
    ])
    assert rc == 0
    files = list(out.glob("*.json"))
    assert files, "CLI run 未产出报告 JSON"
    report = json.loads(files[0].read_text(encoding="utf-8"))
    assert report["metrics"]["case_count"] == 3


def test_cli_dataset_error_clean_exit(tmp_path, capsys):
    """数据集不可用 → 干净的 stderr 报错 + 退出码 2，非未捕获 HTTPException traceback。"""
    with pytest.raises(SystemExit) as ei:
        cli_main([
            "run", "--dataset", str(tmp_path / "missing.jsonl"),
            "--target", "mock", "--judge", "mock",
            "--db", str(tmp_path / "store.db"), "--out", str(tmp_path / "reports"),
        ])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "数据集" in err and "HTTPException" not in err and "Traceback" not in err


def test_api_dataset_whitelist_not_relaxed(tmp_path):
    """两层语义分开：API 侧（默认 extra_roots=()）对 builtin 外路径仍拒绝，
    显式传 extra_roots 才放行——CLI 放行不得放松 API 白名单。"""
    from fastapi import HTTPException

    from agenteval.api import resolve_dataset

    ds = _write_jsonl(tmp_path / "my_eval.jsonl")
    with pytest.raises(HTTPException) as ei:
        resolve_dataset(str(ds))
    assert ei.value.status_code == 422
    # 显式 extra_root 放行（CLI 修复走的正是这一机制）
    resolved = resolve_dataset(str(ds), extra_roots=(tmp_path,))
    assert len(resolved) == 3
