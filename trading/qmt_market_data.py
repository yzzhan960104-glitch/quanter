# -*- coding: utf-8 -*-
"""QMT 行情垫片（2026-08-27 · QMT 退役 P3）。

原物理真身已随 broker/qmt_quote.py 删除（掘金为唯一实盘平台，现价巡检走 gm tick
的 pilot 单文件）。本模块保留为【盲价桩】：get_quotes 恒返 {}、get_quote 恒返
None——持有本路径的调用方（stop_loss 现价批量取数等，引擎 dormant 态）与
monkeypatch("trading.qmt_market_data.get_quotes") 的测试面零改动可用，缺失现价
走既有盲价防御分支。历史实现见 archive/qmt-stack-final 分支。
"""
from __future__ import annotations


async def get_quote(sym: str):
    return None


async def get_quotes(syms):
    return {}
