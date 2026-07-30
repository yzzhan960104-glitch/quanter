from strategies.base import Strategy
from strategies.neckline.strategy import NecklineMethodStrategy


def test_protocol_declares_required_data_keys():
    # Protocol 属性存在
    assert hasattr(Strategy, "required_data_keys")


def test_neckline_defaults_to_daily():
    strat = NecklineMethodStrategy()
    assert strat.required_data_keys == frozenset({"daily"})


def test_default_value_is_daily():
    # NecklineMethodStrategy 不显式覆盖 → 继承默认 {"daily"}
    assert "daily" in NecklineMethodStrategy().required_data_keys
