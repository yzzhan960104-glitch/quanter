# -*- coding: utf-8 -*-
"""name_resolver 单测：指数硬编码命中/兜底、个股降级、板块降级/命中、统一入口。"""
from broadcast import name_resolver


def test_index_known_8_widths():
    # 8 大宽基全部命中中文名（播报核心可读性）
    assert name_resolver.resolve_index_name("000300.SH") == "沪深300"
    assert name_resolver.resolve_index_name("000001.SH") == "上证指数"
    assert name_resolver.resolve_index_name("000016.SH") == "上证50"
    assert name_resolver.resolve_index_name("000905.SH") == "中证500"
    assert name_resolver.resolve_index_name("000852.SH") == "中证1000"
    assert name_resolver.resolve_index_name("000688.SH") == "科创50"
    assert name_resolver.resolve_index_name("399001.SZ") == "深证成指"
    assert name_resolver.resolve_index_name("399006.SZ") == "创业板指"
    assert len(name_resolver.INDEX_NAMES) == 8


def test_index_unknown_passthrough():
    # 未知指数返原 code（兜底，绝不抛/绝不返 None）
    assert name_resolver.resolve_index_name("999999.XX") == "999999.XX"
    assert name_resolver.resolve_index_name("") == ""


def test_ths_degrades_to_code_when_empty():
    # 板块字典空（当前数据源不可用的真实态）→ 返原 code，不抛
    name_resolver._THS_NAMES.clear()
    assert name_resolver.resolve_ths_name("885572.TI") == "885572.TI"


def test_ths_hits_when_dict_filled():
    # 字典填入后自动命中（模拟数据源接通后）
    name_resolver._THS_NAMES["885572.TI"] = "CPO概念"
    try:
        assert name_resolver.resolve_ths_name("885572.TI") == "CPO概念"
    finally:
        name_resolver._THS_NAMES.clear()


def test_stock_degrades_when_no_credentials(monkeypatch):
    # symbol_names 已加载但空 dict（无权限降级态）→ 返原 code，不抛、不触发网络
    import data.symbol_names as sn

    monkeypatch.setattr(sn, "_LOADED", True)
    monkeypatch.setattr(sn, "_NAME_MAP", {})
    assert name_resolver.resolve_stock_name("000001.SZ") == "000001.SZ"


def test_stock_hits_when_map_filled(monkeypatch):
    # symbol_names 有映射 → 命中中文名（模拟有权限环境）
    import data.symbol_names as sn

    monkeypatch.setattr(sn, "_LOADED", True)
    monkeypatch.setattr(sn, "_NAME_MAP", {"600519.SH": "贵州茅台"})
    assert name_resolver.resolve_stock_name("600519.SH") == "贵州茅台"


def test_resolve_dispatch_by_kind(monkeypatch):
    import data.symbol_names as sn

    monkeypatch.setattr(sn, "_LOADED", True)
    monkeypatch.setattr(sn, "_NAME_MAP", {})
    name_resolver._THS_NAMES.clear()

    assert name_resolver.resolve("000300.SH", "index") == "沪深300"
    assert name_resolver.resolve("000001.SZ", "stock") == "000001.SZ"  # 降级
    assert name_resolver.resolve("885572.TI", "ths") == "885572.TI"    # 降级
    assert name_resolver.resolve("xxx", "unknown") == "xxx"             # 未知 kind 兜底
