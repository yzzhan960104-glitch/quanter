# -*- coding: utf-8 -*-
"""symbol_names 映射测试（#1 标的→企业名）。

覆盖：load_all 建 ts_code→name dict（mock pro.stock_basic）、get_name 未加载/未命中兜底、
load_all 异常降级不崩。
"""
from data import symbol_names


def test_load_all_builds_map_from_stock_basic(monkeypatch):
    """load_all 调 pro.stock_basic 建 ts_code→name 内存 dict。"""
    symbol_names.reset_for_test()

    class _FakePro:
        def stock_basic(self, **kwargs):
            import pandas as pd
            return pd.DataFrame({
                "ts_code": ["600519.SH", "000001.SZ", "300750.SZ"],
                "name": ["贵州茅台", "平安银行", "宁德时代"],
            })

    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: _FakePro())
    n = symbol_names.load_all()
    assert n == 3
    assert symbol_names.get_name("600519.SH") == "贵州茅台"
    assert symbol_names.get_name("000001.SZ") == "平安银行"
    assert symbol_names.get_name("300750.SZ") == "宁德时代"


def test_get_name_returns_symbol_when_not_loaded_or_missing():
    """未加载或未命中 → 返 symbol 本身（前端兜底显代号，不白屏）。"""
    symbol_names.reset_for_test()
    assert symbol_names.get_name("600519.SH") == "600519.SH"   # 未加载


def test_load_all_degrades_silently_on_exception(monkeypatch):
    """get_pro 抛异常（无凭证/网络）→ 降级空 dict + get_name 返 symbol，不崩。"""
    symbol_names.reset_for_test()

    def _boom():
        raise RuntimeError("无 Tushare 凭证")

    monkeypatch.setattr("data._tushare_compat.get_pro", _boom)
    n = symbol_names.load_all()
    assert n == 0
    assert symbol_names.get_name("600519.SH") == "600519.SH"


def test_load_all_is_idempotent(monkeypatch):
    """load_all 幂等：第二次调不重复请求 get_pro。"""
    symbol_names.reset_for_test()
    calls = []

    class _FakePro:
        def stock_basic(self, **kwargs):
            calls.append(1)
            import pandas as pd
            return pd.DataFrame({"ts_code": ["000001.SZ"], "name": ["平安银行"]})

    monkeypatch.setattr("data._tushare_compat.get_pro", lambda: _FakePro())
    symbol_names.load_all()
    symbol_names.load_all()   # 第二次应跳过
    assert len(calls) == 1
