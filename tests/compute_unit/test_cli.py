# -*- coding: utf-8 -*-
"""__main__ CLI 测试:verify/run/summary 三子命令路由 + 退出码 + EnvDriftError 兜底。

mock run/verify/summarize,不真跑回测/不真读 parquet。
"""
from compute_unit import __main__ as cu


def _write_task(tmp_path):
    """写一个最小 task.json 供 CLI 读(mock 真实 freeze)。"""
    import json
    task = {
        "protocol_version": 1, "task_id": "t1", "created_at": "x",
        "git_commit": "a"*40, "engine_hash": "x", "parquet_sha256": "x",
        "lake_start": "2025-01-01", "embargo_days": 5,
        "snapshot_meta": {"snapshot_hash": "s", "universe_def": "u", "universe_count": 1,
                          "date_range": "dr", "lake_start": "2025-01-01"},
        "split": {"inner": {"name": "i", "start": "2025-01-01", "end": "2025-12-31"},
                  "outer": {"name": "o", "start": "2026-01-01", "end": "2026-12-31"},
                  "embargo_days": 5},
        "trials": [],
    }
    p = tmp_path / "task.json"
    p.write_text(json.dumps(task), encoding="utf-8")
    return p


def test_verify_ok(monkeypatch, tmp_path, capsys):
    """verify 子命令:verify 通过 → 退出码 0 + 打印可跑批。"""
    monkeypatch.setattr("compute_unit.env_check.verify", lambda t: None)
    p = _write_task(tmp_path)
    rc = cu.main(["verify", str(p)])
    assert rc == 0
    assert "可跑批" in capsys.readouterr().out


def test_verify_drift_returns_3(monkeypatch, tmp_path, capsys):
    """verify 漂移 → 退出码 3 + stderr 打印漂移信息。"""
    from compute_unit.env_check import EnvDriftError
    monkeypatch.setattr("compute_unit.env_check.verify",
                        lambda t: (_ for _ in ()).throw(EnvDriftError("git_commit 漂移")))
    p = _write_task(tmp_path)
    rc = cu.main(["verify", str(p)])
    assert rc == 3
    assert "环境漂移" in capsys.readouterr().err


def test_run_writes_result(monkeypatch, tmp_path):
    """run 子命令:run 通过 → 写 result.json + 退出码 0。"""
    from compute_unit.protocol import Result
    fake = Result(task_id="t1", git_commit="a"*40, parquet_sha256="x", ran_at="x", results=[])
    monkeypatch.setattr("compute_unit.runner.run", lambda t, n_proc=None: fake)
    p = _write_task(tmp_path)
    out = tmp_path / "result.json"
    rc = cu.main(["run", str(p), "-o", str(out)])
    assert rc == 0
    assert out.exists()


def test_run_drift_returns_3(monkeypatch, tmp_path):
    """run 漂移 → 退出码 3(不写 result)。"""
    from compute_unit.env_check import EnvDriftError
    monkeypatch.setattr("compute_unit.runner.run",
                        lambda t, n_proc=None: (_ for _ in ()).throw(EnvDriftError("engine 漂移")))
    p = _write_task(tmp_path)
    out = tmp_path / "result.json"
    rc = cu.main(["run", str(p), "-o", str(out)])
    assert rc == 3
    assert not out.exists()


def test_summary_prints(monkeypatch, tmp_path, capsys):
    """summary 子命令:读 result.json → 打印摘要。"""
    import json
    rfile = tmp_path / "result.json"
    rfile.write_text(json.dumps({
        "task_id": "t1", "git_commit": "a"*40, "parquet_sha256": "b"*64, "ran_at": "x",
        "results": [{"trial_id": "ok1", "status": "ok",
                     "inner": {"n": 5, "ann": 0.2, "calmar": 3.0, "max_dd": 0.07},
                     "outer": {"n": 4}, "n_total": 9}],
    }), encoding="utf-8")
    rc = cu.main(["summary", str(rfile), "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Mac 计算单元" in out and "ok1" in out
