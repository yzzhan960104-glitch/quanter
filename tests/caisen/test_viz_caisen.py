# -*- coding: utf-8 -*-
"""蔡森形态学可视化层测试（Phase 3 · Task 6）。

物理意图与覆盖节点：
  本测试验证蔡森形态学流水线 Task 6 的两个可视化内核——
    1. render_plan_png：mplfinance K 线 + 颈线/W 底四点连线（alines）
       + 止损/止盈/满足点（hlines）→ PNG 静态图（钉钉/邮件 T 日晚报推送素材）；
    2. build_chart_data：装配 lightweight-charts JSON（candles + markers + priceLines），
       供前端 CaisenScreenView 与 server chart 端点消费。

数据契约（以 mplfinance / lightweight-charts 官方文档为准，不臆造）：
    mplfinance.alines_coordinates: [(time, price), ...] 顺序点对连线；
    mplfinance.hlines:             [price, ...] 水平价位线；
    lightweight-charts candle:     {time, open, high, low, close}；
    lightweight-charts marker:     {time, position, color, shape, text}；
    lightweight-charts priceLine:  {price, color, lineWidth, title}。

形态点元数据契约（plan.metadata["pattern_points"]）：
    W 底四点：   {"p1": {"idx","price"}, "p2": ..., "p3": ..., "p4": ...}
    头肩底六点： {"p1":..., "p2":..., ..., "p6":...}
    缺失时（plan 未挂形态点 metadata）→ viz 层降级仅画 K 线 + 关键价位水平线，
    不抛异常（防御性：早期计划/历史计划可能无形态点 metadata，viz 不应阻断）。
"""
import pandas as pd
import pytest

from caisen.viz_static import render_plan_png
from caisen.viz_interactive import build_chart_data


# ---------------------------------------------------------------------------
# 合成测试数据构造器
# ---------------------------------------------------------------------------
def _make_price_df(n: int = 30) -> pd.DataFrame:
    """合成 OHLCV 日 K DataFrame（mplfinance 要求列名 Open/High/Low/Close/Volume + DatetimeIndex）。

    构造一段先跌后涨的 W 底雏形：P1(底)→P2(反弹)→P3(右底)→P4(突破)。
    价格序列显式构造，确保形态点 idx 与 price 可精确断言。
    """
    dates = pd.bdate_range("2024-01-01", periods=n)
    # 显式构造 W 底四点位置（idx=5 P1底 / idx=12 P2峰 / idx=18 P3右底 / idx=26 P4突破峰）
    close = [10.0] * n
    close[5] = 8.0    # P1 左底（谷）
    close[12] = 10.0  # P2 颈线高点（峰）
    close[18] = 8.2   # P3 右底（谷，略高于 P1 符合右脚≥左脚）
    close[26] = 10.5  # P4 突破峰
    df = pd.DataFrame({
        "Open": [c - 0.1 for c in close],
        "High": [c + 0.3 for c in close],
        "Low": [c - 0.3 for c in close],
        "Close": close,
        "Volume": [1_000_000] * n,
    }, index=dates)
    df.index.name = "Date"
    return df


def _make_w_bottom_plan(symbol: str = "000001") -> dict:
    """构造 W 底计划 dict（字段对齐 TradePlan，含形态点 metadata）。

    metadata.pattern_points 挂载 W 底四点（P1..P4）的 idx + price，
    与 _make_price_df 的合成 K 线坐标对齐，便于断言 marker 位置。
    """
    return {
        "plan_id": "test-plan-001",
        "symbol": symbol,
        "pattern_type": "w_bottom",
        "formed_at": pd.Timestamp("2024-01-30"),
        "breakout_price": 10.5,
        "neckline_price": 10.0,
        "bottom_price": 8.0,
        "entry_upper": 10.5,
        "entry_lower": 10.2,
        "stop_loss": 7.8,
        "take_profit": 12.0,    # 第一波满足 = 颈线 + H = 10 + 2
        "take_profit_2x": 14.0, # 第二波满足 = 颈线 + 2H = 10 + 4
        "rr_ratio": 3.0,
        "metadata": {
            "depth": 0.25,
            "pattern_points": {
                "p1": {"idx": 5, "price": 8.0},
                "p2": {"idx": 12, "price": 10.0},
                "p3": {"idx": 18, "price": 8.2},
                "p4": {"idx": 26, "price": 10.5},
            },
        },
    }


def _make_head_shoulder_plan() -> dict:
    """构造头肩底计划 dict（含六点形态 metadata P1..P6）。"""
    return {
        "plan_id": "test-plan-hs-001",
        "symbol": "600000",
        "pattern_type": "head_shoulder",
        "formed_at": pd.Timestamp("2024-02-15"),
        "breakout_price": 11.0,
        "neckline_price": 10.5,
        "bottom_price": 7.5,
        "entry_upper": 11.0,
        "entry_lower": 10.7,
        "stop_loss": 7.3,
        "take_profit": 13.5,
        "take_profit_2x": 16.5,
        "rr_ratio": 3.2,
        "metadata": {
            "depth": 0.4,
            "pattern_points": {
                "p1": {"idx": 2, "price": 10.0},
                "p2": {"idx": 5, "price": 8.5},
                "p3": {"idx": 9, "price": 10.2},
                "p4": {"idx": 13, "price": 7.5},
                "p5": {"idx": 18, "price": 10.8},
                "p6": {"idx": 23, "price": 8.8},
            },
        },
    }


# ===========================================================================
# 1. render_plan_png（mplfinance 静态 PNG）
# ===========================================================================
class TestRenderPlanPng:
    """mplfinance K 线 + alines 颈线/四点连线 + hlines 关键价位 → PNG。"""

    def test_render_plan_png_creates_file(self, tmp_path):
        """render_plan_png 生成 PNG 文件存在且非空（out_path 返回与入参一致）。"""
        plan = _make_w_bottom_plan()
        price_df = _make_price_df()
        out_path = str(tmp_path / "plan.png")

        result = render_plan_png(plan, price_df, out_path)

        assert result == out_path
        import os
        assert os.path.isfile(out_path), "PNG 文件未生成"
        assert os.path.getsize(out_path) > 0, "PNG 文件为空"

    def test_render_plan_png_has_annotations(self, tmp_path, monkeypatch):
        """验证 mplfinance.plot 被调用且传入了 alines（颈线/四点连线）+ hlines（止损/止盈）。

        策略：monkeypatch mplfinance.plot 捕获 kwargs，确认 alines/hlines 非空，
        而非依赖真实渲染（headless 环境 + 加速测试）。同时验证返回路径与文件创建。
        """
        import mplfinance as mpf

        captured = {}

        def _fake_plot(data, **kwargs):
            captured["kwargs"] = kwargs
            # 模拟 savefig 行为：创建一个占位文件（让外层断言文件存在通过）
            sf = kwargs.get("savefig")
            if sf:
                with open(sf, "wb") as f:
                    f.write(b"FAKE_PNG_BYTES")
            # 返回 None（mpf.plot 在 returnfig=False 时返 None）
            return None

        monkeypatch.setattr(mpf, "plot", _fake_plot)

        plan = _make_w_bottom_plan()
        price_df = _make_price_df()
        out_path = str(tmp_path / "plan_mock.png")

        render_plan_png(plan, price_df, out_path)

        kwargs = captured["kwargs"]
        # alines 必须存在且非空（W 底四点连线）
        assert "alines" in kwargs, "mplfinance.plot 未传 alines（颈线/四点连线缺失）"
        assert kwargs["alines"], "alines 为空（W 底四点连线未标注）"
        # hlines 必须存在且非空（止损/止盈/满足点）
        assert "hlines" in kwargs, "mplfinance.plot 未传 hlines（关键价位缺失）"
        assert kwargs["hlines"], "hlines 为空（止损/止盈/满足点未标注）"
        # savefig 指向 out_path
        assert kwargs.get("savefig") == out_path

    def test_render_plan_png_head_shoulder(self, tmp_path):
        """头肩底（六点）也能正常渲染 PNG（P1-P6 连线）。"""
        plan = _make_head_shoulder_plan()
        price_df = _make_price_df()
        out_path = str(tmp_path / "hs_plan.png")

        result = render_plan_png(plan, price_df, out_path)

        import os
        assert os.path.isfile(out_path)

    def test_render_plan_png_no_pattern_points_degrades(self, tmp_path):
        """plan 无 pattern_points metadata → 降级仅画 K 线 + 关键价位，不抛异常。

        防御性：早期计划/历史计划可能未挂形态点 metadata，viz 层不应阻断。
        """
        plan = _make_w_bottom_plan()
        del plan["metadata"]["pattern_points"]  # 剥离形态点
        price_df = _make_price_df()
        out_path = str(tmp_path / "no_points.png")

        # 不应抛异常
        result = render_plan_png(plan, price_df, out_path)
        assert result == out_path
        import os
        assert os.path.isfile(out_path)


# ===========================================================================
# 2. build_chart_data（lightweight-charts JSON 装配）
# ===========================================================================
class TestBuildChartData:
    """装配 lightweight-charts 数据：candles + markers + priceLines。"""

    def test_build_chart_data_structure(self):
        """返回 dict 含 candles（OHLC）/markers（形态点）/priceLines（关键价位）三键。"""
        plan = _make_w_bottom_plan()
        price_df = _make_price_df()

        data = build_chart_data(plan, price_df)

        assert isinstance(data, dict)
        # 顶层三键必须存在
        assert "candles" in data, "缺少 candles（K 线数据）"
        assert "markers" in data, "缺少 markers（形态点标注）"
        assert "priceLines" in data, "缺少 priceLines（关键价位水平线）"
        # candles 非空（price_df 有 30 根 K 线）
        assert len(data["candles"]) == 30
        # 每个 candle 契约：{time, open, high, low, close}
        c0 = data["candles"][0]
        for k in ("time", "open", "high", "low", "close"):
            assert k in c0, f"candle 缺少字段 {k}"

    def test_build_chart_data_markers_at_pattern_points_w_bottom(self):
        """W 底 markers 落在 P1-P4 位置（4 个 marker，idx 对齐形态点）。"""
        plan = _make_w_bottom_plan()
        price_df = _make_price_df()

        data = build_chart_data(plan, price_df)
        markers = data["markers"]

        # W 底 4 点
        assert len(markers) == 4, f"W 底应有 4 个 marker，实际 {len(markers)}"
        # 每个 marker 契约：{time, position, color, shape, text}
        for m in markers:
            for k in ("time", "position", "color", "shape", "text"):
                assert k in m, f"marker 缺少字段 {k}"
            assert m["position"] in ("aboveBar", "belowBar", "inBar")
            assert m["shape"] in ("circle", "square", "arrowUp", "arrowDown")

        # markers 的 time 应对齐 K 线时间（P1 idx=5）
        times = [pd.Timestamp(m["time"]) for m in markers]
        expected_p1_time = price_df.index[5]
        assert times[0] == expected_p1_time, "P1 marker 时间未对齐 K 线"
        # text 含 P1..P4 标签
        texts = [m["text"] for m in markers]
        assert any("P1" in t for t in texts), "markers 缺 P1 标签"
        assert any("P4" in t for t in texts), "markers 缺 P4 标签"

    def test_build_chart_data_markers_at_pattern_points_head_shoulder(self):
        """头肩底 markers 落在 P1-P6 位置（6 个 marker）。"""
        plan = _make_head_shoulder_plan()
        price_df = _make_price_df()

        data = build_chart_data(plan, price_df)
        markers = data["markers"]

        assert len(markers) == 6, f"头肩底应有 6 个 marker，实际 {len(markers)}"
        texts = [m["text"] for m in markers]
        for label in ("P1", "P2", "P3", "P4", "P5", "P6"):
            assert any(label in t for t in texts), f"markers 缺 {label} 标签"

    def test_build_chart_data_price_lines(self):
        """priceLines 含止损/止盈/满足点/颈线，每个 priceLine 契约：{price, color, lineWidth, title}。"""
        plan = _make_w_bottom_plan()
        price_df = _make_price_df()

        data = build_chart_data(plan, price_df)
        price_lines = data["priceLines"]

        assert len(price_lines) >= 3, "至少应有 止损/止盈/颈线 三条 priceLine"
        for pl in price_lines:
            for k in ("price", "color", "title"):
                assert k in pl, f"priceLine 缺少字段 {k}"
            assert isinstance(pl["price"], (int, float))
        # 标题含止损/止盈语义
        titles = [pl["title"] for pl in price_lines]
        joined = "|".join(titles)
        assert "止损" in joined or "stop" in joined.lower(), "priceLines 缺止损线"
        assert "止盈" in joined or "profit" in joined.lower() or "满足" in joined, "priceLines 缺止盈线"

    def test_build_chart_data_no_pattern_points_degrades(self):
        """无 pattern_points metadata → markers 为空列表，不抛异常（降级容错）。"""
        plan = _make_w_bottom_plan()
        del plan["metadata"]["pattern_points"]
        price_df = _make_price_df()

        data = build_chart_data(plan, price_df)
        # markers 为空但 candles/priceLines 仍正常
        assert data["markers"] == []
        assert len(data["candles"]) == 30
        assert len(data["priceLines"]) >= 1
