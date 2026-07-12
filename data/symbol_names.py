# -*- coding: utf-8 -*-
"""symbol→企业名映射（Tushare pro.stock_basic 启动全量加载 + 内存 dict 查询）。

物理定位（#1 候选计划显企业名而非代号）：
    A 股约 5000 只，pro.stock_basic 一次返 <1MB，启动全量加载内存 dict。
    企业名变化频率极低（新股/退市/更名），无需实时。降级（无凭证/网络）返 symbol 本身，
    前端兜底显代号，不白屏。

设计（极简 + 显式）：
- 模块级单例 dict + loaded 标记，load_all 幂等（重复调只加载一次）；
- get_name O(1) 查 dict，未命中/未加载返 symbol（前端兜底）。
"""
import logging
from typing import Dict

logger = logging.getLogger(__name__)

_NAME_MAP: Dict[str, str] = {}
_LOADED: bool = False


def load_all() -> int:
    """启动全量加载 A 股 symbol→name（Tushare pro.stock_basic，ts_code→name）。

    幂等：已加载则跳过（lifespan 重启/reload 不重复请求）。
    降级：get_pro 抛异常（无凭证/网络）→ 空 dict + WARNING，get_name 返 symbol。

    返回：加载数量（0=降级或已加载跳过）。
    """
    global _NAME_MAP, _LOADED
    if _LOADED:
        return len(_NAME_MAP)
    try:
        from data._tushare_compat import get_pro
        pro = get_pro()
        df = pro.stock_basic(list_status="L", fields="ts_code,name")
        _NAME_MAP = dict(zip(df["ts_code"], df["name"]))
        _LOADED = True
        logger.info("symbol_names 加载 %d 只 A 股企业名", len(_NAME_MAP))
        return len(_NAME_MAP)
    except Exception as exc:
        # 降级：无 Tushare 凭证/网络/接口异常 → 空 dict，get_name 返 symbol（前端兜底显代号）
        logger.warning("symbol_names 加载失败（降级：get_name 返 symbol）：%s", exc)
        _NAME_MAP = {}
        _LOADED = True   # 标记已尝试，避免 lifespan 重复请求
        return 0


def get_name(symbol: str) -> str:
    """symbol→企业名。未加载/未命中返 symbol 本身（前端兜底显代号，不白屏）。"""
    return _NAME_MAP.get(symbol, symbol)


def reset_for_test() -> None:
    """测试专用：重置模块状态（便于 monkeypatch 后重跑 load_all）。"""
    global _NAME_MAP, _LOADED
    _NAME_MAP = {}
    _LOADED = False
