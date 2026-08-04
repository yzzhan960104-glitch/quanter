# -*- coding: utf-8 -*-
"""ops/data_pipeline.py 脚本模式 sys.path 回归测试（C-9 A5 infra import 崩）。

背景：pipeline_then_eod 以 ``python ops/data_pipeline.py`` 方式 spawn 采集子进程，
sys.path[0]=ops/，仓库根不在 path → ``from infra.pyio import force_utf8_stdout``
抛 ModuleNotFoundError → rc=1 → eod 拒产 T+1 计划（2026-08-05 计划缺失事故）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run_script_as_module(code: str) -> subprocess.CompletedProcess:
    """以脚本方式（非 -m）加载目标文件；清 PYTHONPATH 模拟干净子进程环境。"""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=ROOT,
    )


def test_data_pipeline_script_load_puts_root_on_syspath():
    """脚本方式加载 data_pipeline 后，仓库根必须入 sys.path，infra 可导入。"""
    pipeline_py = os.path.join(ROOT, "ops", "data_pipeline.py")
    code = (
        # 模拟 `python ops/data_pipeline.py`：sys.path[0]=脚本目录，cwd 不入 path。
        "import runpy, sys; "
        "del sys.path[0]; "
        f"runpy.run_path({pipeline_py!r}, run_name='pipeline_mod'); "
        f"assert {ROOT!r} in sys.path, sys.path; "
        "import infra.pyio; print('OK')"
    )
    r = _run_script_as_module(code)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
