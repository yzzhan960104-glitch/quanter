# -*- coding: utf-8 -*-
"""跨文件共享的微型测试替身（2026-08-19 测试库精简 W2 收口）。

原 _FakeProc 三份文件级逐字重复（ops/test_trading_supervisor / test_audit_ssot /
test_manage_ops_schtasks），仅参数顺序/默认值有差——统一为一份关键字友好版。
"""
from __future__ import annotations


class FakeProc:
    """subprocess.run 返回值替身（stdout 文本 + returncode）。

    兼容三种历史调用形态：位置传 stdout（supervisor 版）/ 关键字 returncode
    （schtasks 版）/ 全默认（audit 版）。
    """

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
