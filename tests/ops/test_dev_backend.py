# -*- coding: utf-8 -*-
"""A5：dev.py 后端统一走 python -m trading + dev env 契约（connect bot 解耦）。"""
from __future__ import annotations

from ops import dev


def test_backend_cmd_uses_python_m_trading():
    """A5: 后端命令 = venv python -m trading（与生产同一入口，不再直跑 uvicorn）。"""
    cmd = dev._backend_cmd(reload=False)
    assert cmd[-2:] == ["-m", "trading"]


def test_backend_env_default_no_reload_and_skip_bots():
    """A5: dev env 标记 QUANTER_DEV_*：默认关热重载、跳过 connect bot、锁 8000。"""
    env = dev._backend_env(reload=False)
    assert env["QUANTER_DEV_MODE"] == "1"
    assert env["QUANTER_DEV_SKIP_CONNECT_BOTS"] == "1"
    assert env["QUANTER_DEV_NO_RELOAD"] == "1"
    assert env["SERVER_PORT"] == "8000"


def test_backend_env_reload_enables_hot_reload():
    """A5: --reload → QUANTER_DEV_NO_RELOAD=0（热重载开关由 env 表达）。"""
    env = dev._backend_env(reload=True)
    assert env["QUANTER_DEV_NO_RELOAD"] == "0"
