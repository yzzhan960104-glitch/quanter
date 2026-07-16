# -*- coding: utf-8 -*-
"""标的代码→中文名映射（播报文案可读性 · spec §8 #1-3）。

三层来源，按可靠性排序：
1. 指数：硬编码 INDEX_NAMES（8 大宽基，data_lake/index_daily 全部 symbol，固定·零依赖·100% 可用）。
2. 个股：复用 data.symbol_names（Tushare pro.stock_basic 全量内存 dict）。
3. 同花顺板块：_THS_NAMES dict（当前数据源不可用，留空降级）。

【2026-07-16 实测约束】当前 Tushare token 对 stock_basic / ths_daily / ths_index / index_classify
均无权限，akshare 同花顺概念接口断网（ConnectionAborted）。故：
- 指数名：硬编码，100% 中文（播报核心一定可读）。
- 个股名：symbol_names.load_all 会降级（无权限→空 dict），get_name 返原 code。
- 板块名：_THS_NAMES 空，resolve_ths_name 返原 code（如 885572.TI）。
权限恢复 / 数据源接通后，无需改本模块即可自动生效（symbol_names 重新 load_all / 填 _THS_NAMES）。

设计铁律（量化鲁棒性）：所有 resolve 未命中返原 code，**绝不抛/绝不返 None**——
文案至少有代码兜底，不白屏、不中断播报。
"""
from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# 8 大宽基指数（data_lake/index_daily 全部 symbol，2026-07-16 实测固定 8 个）。
# 物理意图：指数池极稳定（几乎不变），硬编码比每次查表更可靠、零依赖、零网络。
INDEX_NAMES: Dict[str, str] = {
    "000001.SH": "上证指数",
    "000016.SH": "上证50",
    "000300.SH": "沪深300",
    "000688.SH": "科创50",
    "000852.SH": "中证1000",
    "000905.SH": "中证500",
    "399001.SZ": "深证成指",
    "399006.SZ": "创业板指",
}

# 同花顺概念板块 code→中文名（ths_daily 的 symbol 形如 885572.TI）。
# 当前数据源不可用（ths_* 无权限 + akshare 断网）→ 空 dict 降级返代码。
# 后续接通（tushare 权限恢复 / akshare stock_board_concept_name_ths / 本地字典）后
# 填入此 dict 即自动生效，resolve_ths_name 无需改动。
_THS_NAMES: Dict[str, str] = {}


def resolve_index_name(code: str) -> str:
    """指数代码→中文名。命中 8 宽基返中文，未命中返原 code（新指数兜底显代码）。"""
    return INDEX_NAMES.get(code, code)


def resolve_stock_name(code: str) -> str:
    """个股代码→企业名。复用 data.symbol_names（stock_basic 全量内存 dict）。

    降级链：未加载则惰性 load_all → 无权限/网络则 symbol_names 内部降级为空 dict →
    get_name 返原 code。任何异常（import/网络）兜底返原 code，绝不抛。
    """
    try:
        from data import symbol_names

        # 惰性加载：首次调用触发 stock_basic 全量拉取（幂等，symbol_names 内部守卫）。
        if not symbol_names._LOADED:
            symbol_names.load_all()
        return symbol_names.get_name(code)
    except Exception as exc:
        # 防御性兜底：symbol_names import/加载任何异常 → 返原 code，不拖垮播报。
        logger.warning("resolve_stock_name 降级（%s → 返原 code）：%s", code, exc)
        return code


def resolve_ths_name(code: str) -> str:
    """同花顺概念板块 code→中文名。_THS_NAMES 命中返中文，否则返原 code。"""
    return _THS_NAMES.get(code, code)


def resolve(code: str, kind: str) -> str:
    """统一入口：按 kind 路由到对应 resolver。

    kind: "index" | "stock" | "ths"。未知 kind 返原 code（防御性，绝不抛）。
    """
    if kind == "index":
        return resolve_index_name(code)
    if kind == "stock":
        return resolve_stock_name(code)
    if kind == "ths":
        return resolve_ths_name(code)
    return code
