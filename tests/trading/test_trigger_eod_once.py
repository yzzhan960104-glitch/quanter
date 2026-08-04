# -*- coding: utf-8 -*-
"""trigger_eod_once 工具回归测试（2026-08-05 事故：脚本直跑 import 失败 + 复核查错日期）。"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_script_as_module(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=ROOT,
    )


def test_trigger_script_load_puts_root_on_syspath():
    """脚本方式加载 trigger_eod_once 后，仓库根必须入 sys.path（trading 可导入）。"""
    trigger_py = os.path.join(ROOT, "trading", "tools", "trigger_eod_once.py")
    code = (
        # 模拟 `python trading/tools/trigger_eod_once.py`：脚本目录入 path，cwd 不入。
        "import runpy, sys; "
        "del sys.path[0]; "
        f"ns = runpy.run_path({trigger_py!r}, run_name='trigger_mod'); "
        f"assert {ROOT!r} in sys.path, sys.path; "
        "assert callable(ns['_run']); print('OK')"
    )
    r = _run_script_as_module(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_plan_path_for_uses_next_trading_day(monkeypatch, tmp_path):
    """复核落盘文件必须查 plan_date（T+1），而不是 today（补跑场景会查错）。"""
    from trading.tools import trigger_eod_once as t
    monkeypatch.setattr(t.calendar, "next_trading_day", lambda d: "2026-08-05")
    monkeypatch.setenv("TRADE_PLAN_DIR", str(tmp_path))
    p = t._plan_path_for("2026-08-04")
    assert p.name == "plan_2026-08-05.json"
    assert p.parent == tmp_path
