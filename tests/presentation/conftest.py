# -*- coding: utf-8 -*-
"""presentation 测试专用 conftest：QUANTER_TESTING=1 测试隔离。

Why：lifespan 测试会真实执行 main.py 的日志装配——若不标记 QUANTER_TESTING=1，
生产文件 handler 会被挂进 root logger，pytest 日志写进 logs/quanter.log
（08-06 实证污染）。该目录下所有测试统一标记，lifespan 看到 env 即跳过文件 handler。
"""
import pytest


@pytest.fixture(autouse=True)
def _quanter_testing_env(monkeypatch):
    monkeypatch.setenv("QUANTER_TESTING", "1")
