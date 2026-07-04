"""_tushare_compat 单测：代理/直连切换逻辑（_use_proxy / source_name）。

get_pro 涉及真实 tnskhdata/tushare import + 网络，留给端到端验证；本测试聚焦
切换逻辑（TNSKHDATA_TOKEN 环境变量决定代理/直连），不依赖网络/凭证。
"""
from data._tushare_compat import _use_proxy, source_name


def test_use_proxy_true_when_token_set(monkeypatch):
    """TNSKHDATA_TOKEN 非空 → 走代理 tnskhdata。"""
    monkeypatch.setenv("TNSKHDATA_TOKEN", "dummy_token_123")
    assert _use_proxy() is True
    assert source_name() == "tnskhdata"


def test_use_proxy_false_when_token_empty(monkeypatch):
    """TNSKHDATA_TOKEN 空/未设 → 回退直连 tushare。"""
    monkeypatch.delenv("TNSKHDATA_TOKEN", raising=False)
    assert _use_proxy() is False
    assert source_name() == "tushare"


def test_use_proxy_false_when_token_whitespace(monkeypatch):
    """TNSKHDATA_TOKEN 仅空白 → 视为未设（strip 兜底）。"""
    monkeypatch.setenv("TNSKHDATA_TOKEN", "   ")
    assert _use_proxy() is False
    assert source_name() == "tushare"
