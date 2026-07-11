# -*- coding: utf-8 -*-
"""蔡森形态学静态可视化（mplfinance K 线 + alines/hlines 标注 → PNG）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"T 日晚报静态图"生成器——把一个 TradePlan（dict 形态）
    与对应价格 DataFrame 合成为一张带颈线/形态点连线/止损止盈水平线的 PNG，供
    core.notifier 推送到钉钉/邮件，或 server 层 HTTP 返回图片附件。

    本模块只做"标注装配 + mplfinance 调用"，不做任何识别/过滤/下单。所有坐标
    （形态点 P1-P4/P6、颈线、止损、止盈）均取自 plan dict，无任何二次推导。

mplfinance 数据契约（以官方文档为准）：
    - mpf.plot(data, type="candle", alines=..., hlines=..., savefig=...)
    - data：DataFrame，列名 Open/High/Low/Close/Volume，DatetimeIndex（name="Date"）
    - alines：形态点连线，格式 [["Date", price], ...] 或 [(Date, price), ...] 的列表
              （ mplfinance alines_coordinates 接受 time-string / pd.Timestamp 作 X 轴）
    - hlines：水平价位线，格式 [price, ...] 或 dict {hlines, colors, linestyle, linewidths}
    - savefig：输出路径，触发 headless 渲染（matplotlib Agg backend）

防御性边界（CLAUDE.md 量化风控·边界审查）：
    - headless 环境（无 display）：显式 mpl.use("Agg") 兜底，避免 Windows/Linux
      CI 无显示设备时 matplotlib 报 RuntimeError("Invalid DISPLAY")；
    - pattern_points 缺失（早期/历史计划无形态点 metadata）：降级仅画 K 线 + 关键价位
      hlines，不抛异常（viz 不应阻断推送链路）；
    - plan 字段缺失（如 bottom_price/neckline_price）：逐字段 try/except 收集，
      拿到哪个画哪个，缺字段不阻塞整图渲染。
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

# 必须在 import mplfinance / matplotlib.pyplot 之前切 Agg backend。
# Why 提前：mplfinance 内部 import matplotlib，一旦 pyplot 被 backend 设置为
# 默认交互式 backend（TkAgg/Qt5Agg），headless 环境首次 draw_figure 会抛
# RuntimeError("Invalid DISPLAY") → 整张 PNG 生成失败 → 钉钉晚报断流。
# Agg 是纯文件 backend，无 display 依赖，CI/Windows 服务/容器环境通用。
import matplotlib
matplotlib.use("Agg")
import mplfinance as mpf  # noqa: E402  (须在 use("Agg") 之后)

_logger = logging.getLogger(__name__)


def render_plan_png(plan: dict, price_df: pd.DataFrame, out_path: str) -> str:
    """mplfinance K 线 + alines(颈线/W底四点连线) + hlines(止损/止盈/满足点) → PNG。

    参数：
        plan:     蔡森交易计划（dict 形态，对齐 caisen.plan.TradePlan + storage 序列化字段）。
                  形态点从 plan["metadata"]["pattern_points"] 取，结构：
                      {"p1": {"idx": int, "price": float}, "p2": ..., ...}
                  W 底 p1-p4 / 头肩底 p1-p6。缺失时降级仅画 K 线 + 关键价位 hlines。
        price_df: OHLCV DataFrame（列 Open/High/Low/Close/Volume + DatetimeIndex）。
                  必须与形态点 idx 对齐（idx 为 price_df 的整数位置下标）。
        out_path: PNG 输出绝对路径（目录须存在；调用方负责，如 os.makedirs）。

    返回：
        out_path（与入参一致，便于链式调用 core.notifier 推送）。

    渲染要素：
        1. K 线主体（type="candle"）—— price_df 全段；
        2. alines 形态点连线 —— W 底 P1→P2→P3→P4，头肩底 P1→...→P6
           （蔡森原著形态学骨架可视化，红虚线连接四/六个 pivot）；
        3. hlines 关键价位 —— 止损（红实线）/止盈第一波（绿实线）/止盈第二波（绿虚线）
           /颈线（橙实线）/突破价（蓝虚线），每个价位独立颜色便于人工审阅。
    """
    # —— 0. price_df 列名规范化（mplfinance 强约束 Open/High/Low/Close/Volume + Date index）——
    # 兼容上游可能传入小写列名（caisen 内部 screener 用 close 而非 Close），统一转首字母大写
    df = _normalize_ohlc_columns(price_df)

    # —— 1. 装配 mplfinance kwargs ——
    kwargs: dict[str, Any] = {
        "type": "candle",
        "style": "charles",       # mplfinance 内置深色风格，对比度强，适合钉钉缩略图
        "volume": False,          # 主图仅 K 线，成交量另起子图会撑高 PNG 不利推送
        "savefig": out_path,      # 触发 headless 文件输出
        "returnfig": False,       # 不返回 fig/ax，直接写文件（推送场景无需返回对象）
        "figscale": 1.2,          # 略放大图幅，K 线实体清晰可见（默认 1.0 在钉钉缩略图过小）
        "tight_layout": True,     # 去白边，PNG 紧凑
    }

    # —— 2. 装配 alines（形态点连线）——
    # alines 格式：[[pd.Timestamp, price], ...] 列表 —— mplfinance 按时间轴画折线
    alines = _build_alines(plan, df)
    if alines:
        # alines dict 形式：显式颜色 + 线宽，红虚线突出形态骨架
        kwargs["alines"] = {
            "alines": alines,
            "colors": ["#cc0000"],
            "linestyle": "dashed",
            "linewidths": [1.2],
        }

    # —— 3. 装配 hlines（关键价位水平线）——
    # hlines dict 形式：价位列表 + 逐线颜色 + 线型 + 线宽，蔡森实战关键价位一目了然
    hlines_data = _build_hlines(plan)
    if hlines_data["hlines"]:
        kwargs["hlines"] = hlines_data

    # —— 4. 标题：symbol + 形态类型 + 止盈止损摘要（钉钉晚报缩略图信息密度）——
    # 用 ASCII 标签避免 mplfinance 默认 DejaVu Sans 字体缺 CJK glyph 的 UserWarning
    # （标题字形缺失仅 cosmetic，不影响 K 线/标注渲染；正式生产可配置中文字体）。
    symbol = plan.get("symbol", "?")
    ptype = plan.get("pattern_type", "?")
    title = f"{symbol} | {_pattern_type_label(ptype)}"
    if "stop_loss" in plan and "take_profit" in plan:
        title += f" | SL={plan['stop_loss']:.2f} TP={plan['take_profit']:.2f}"
    kwargs["title"] = title

    # —— 5. 调 mplfinance 渲染（Agg backend，headless 安全）——
    try:
        mpf.plot(df, **kwargs)
    except Exception as exc:
        # mplfinance 渲染异常不应阻断推送链路：降级写一个空 PNG + 记 error 日志
        # （让 notifier 仍能发文字版晚报，图缺失好过整条晚报断流）
        _logger.error("render_plan_png mplfinance 渲染失败 plan_id=%s err=%s",
                      plan.get("plan_id", "<unknown>"), exc)
        with open(out_path, "wb") as f:
            f.write(b"")   # 占位空文件，调用方判 size 可知渲染失败

    return out_path


# ---------------------------------------------------------------------------
# 内部辅助：OHLC 列名规范化
# ---------------------------------------------------------------------------
def _normalize_ohlc_columns(df: pd.DataFrame) -> pd.DataFrame:
    """把 OHLCV 列名统一为 mplfinance 要求的 Open/High/Low/Close/Volume。

    兼容上游多种命名：首字母大写（Open）/ 全小写（open）/ 中文（开盘）。
    物理意图：mplfinance 内部强制按首字母大写列名查找，列名不匹配直接抛 KeyError。
    缺 Volume 列时补一列 0（mplfinance 在 volume=True 时才需要，这里 volume=False
    仅留作扩展，不阻断渲染）。

    DatetimeIndex：mplfinance 要求 index 为 DatetimeIndex 且 name="Date"。
    上游若传 RangeIndex/Int64Index 会抛 ValueError，此处强制转 datetime。
    """
    rename_map = {
        "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume",
        "开盘": "Open", "最高": "High", "最低": "Low", "收盘": "Close", "成交量": "Volume",
    }
    df2 = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}).copy()

    # DatetimeIndex 兜底：上游若传非 datetime index，按 pd.to_datetime 强转
    if not isinstance(df2.index, pd.DatetimeIndex):
        df2.index = pd.to_datetime(df2.index)
    df2.index.name = "Date"

    # Volume 缺失补 0（mplfinance 内部检查列存在性，不强制非空）
    if "Volume" not in df2.columns:
        df2["Volume"] = 0

    return df2


# ---------------------------------------------------------------------------
# 内部辅助：alines（形态点连线）装配
# ---------------------------------------------------------------------------
def _build_alines(plan: dict, df: pd.DataFrame) -> list[list]:
    """从 plan.metadata.pattern_points 装配 alines 坐标序列。

    返回 [[pd.Timestamp, price], ...] 列表，按 P1→P2→...→PN 顺序连接。
    缺 pattern_points 时返回空列表（调用方判空决定是否加 alines）。

    物理意图：蔡森形态学的"骨架"可视化——W 底的四点折线直观展示"左底→颈线→
    右底→突破"的形态结构，人工审阅时一眼可辨形态是否成立（vs 仅看 K 线难以
    主观判定 pivot 位置）。
    """
    points = _extract_pattern_points(plan)
    if not points:
        return []

    alines: list[list] = []
    for label in _ordered_point_labels(points):
        pt = points[label]
        idx = pt.get("idx")
        price = pt.get("price")
        if idx is None or price is None:
            continue
        # idx → DatetimeIndex 对齐：mplfinance alines 用时间作 X 轴（非整数 idx）
        if idx < 0 or idx >= len(df):
            _logger.debug("alines 形态点 %s idx=%s 越界(0,%s)，跳过", label, idx, len(df))
            continue
        ts = df.index[int(idx)]
        alines.append([ts, float(price)])

    return alines


# ---------------------------------------------------------------------------
# 内部辅助：hlines（关键价位水平线）装配
# ---------------------------------------------------------------------------
def _build_hlines(plan: dict) -> dict:
    """从 plan 字段装配关键价位水平线（止损/止盈/颈线/突破）。

    返回 mplfinance hlines dict {hlines, colors, linestyle, linewidths}。
    每条线独立颜色，蔡森实战关键价位一眼可辨：
        止损 stop_loss        —— 红实线（破位即离场）
        第一波满足 take_profit —— 绿实线（部分止盈）
        第二波满足 take_profit_2x —— 绿虚线（主要止盈）
        颈线 neckline_price    —— 橙实线（突破确认基准）
        突破价 breakout_price  —— 蓝虚线（回踩挂单上限）
        底部价 bottom_price    —— 灰虚线（C 波低点参考）

    缺失字段逐个跳过（plan 可能只有部分字段，如早期计划无 take_profit_2x）。
    """
    prices: list[float] = []
    colors: list[str] = []
    linestyles: list[str] = []
    linewidths: list[float] = []

    def _add(price_key: str, color: str, linestyle: str, linewidth: float) -> None:
        v = plan.get(price_key)
        if v is not None and not pd.isna(v):
            prices.append(float(v))
            colors.append(color)
            linestyles.append(linestyle)
            linewidths.append(linewidth)

    # 顺序即 mplfinance 图例顺序（从上到下画，关键止损位置顶便于审阅）
    _add("take_profit_2x", "#009933", "dashed", 1.0)   # 第二波满足（绿虚线）
    _add("take_profit",    "#009933", "solid",  1.2)   # 第一波满足（绿实线）
    _add("breakout_price", "#0066cc", "dashed", 1.0)   # 突破价（蓝虚线）
    _add("neckline_price", "#ff8800", "solid",  1.0)   # 颈线（橙实线）
    _add("bottom_price",   "#888888", "dashed", 0.8)   # 底部价（灰虚线）
    _add("stop_loss",      "#cc0000", "solid",  1.4)   # 止损（红实线·加粗）

    return {
        "hlines": prices,
        "colors": colors,
        "linestyle": linestyles,
        "linewidths": linewidths,
    }


# ---------------------------------------------------------------------------
# 内部辅助：pattern_points 提取
# ---------------------------------------------------------------------------
def _extract_pattern_points(plan: dict) -> dict:
    """从 plan.metadata.pattern_points 提取形态点 dict（容错多层嵌套）。

    支持两种存放形式（防御性兼容上游序列化差异）：
        1. plan["metadata"]["pattern_points"]（标准契约，Task 6 推荐）；
        2. plan["pattern_points"]（早期计划可能平铺在顶层）。

    返回 {} 时表示无形态点，调用方降级处理。
    """
    metadata = plan.get("metadata") or {}
    if isinstance(metadata, dict):
        pts = metadata.get("pattern_points")
        if isinstance(pts, dict) and pts:
            return pts
    pts = plan.get("pattern_points")
    if isinstance(pts, dict) and pts:
        return pts
    return {}


def _ordered_point_labels(points: dict) -> list[str]:
    """按 P1..PN 顺序返回 label 列表（W 底 P1-P4 / 头肩底 P1-P6）。

    容错：points 可能用 "p1" 小写或 "P1" 大写键。统一小写比对，按数字升序排序，
    保证连线顺序正确（否则 markers/alines 会乱序导致折线交叉）。
    """
    labels = [k for k in points.keys() if isinstance(k, str) and k.lower().startswith("p")]
    # 按 P 后数字排序：p1 < p2 < ... < p10（字符串排序会误判 p10 < p2，故提取数字）
    def _num(label: str) -> int:
        try:
            return int(label[1:])
        except (ValueError, IndexError):
            return 999
    return sorted(labels, key=_num)


def _pattern_type_label(ptype: str) -> str:
    """pattern_type 枚举 → ASCII 短标签（避免 CJK 字体缺字 UserWarning）。

    生产环境若配置了中文字体（SimHei/Microsoft YaHei），可改回中文映射 W底/头肩底。
    """
    mapping = {
        "w_bottom": "W-Bottom",
        "head_shoulder": "HeadShoulder",
        "triangle_bottom": "Triangle",
    }
    return mapping.get(ptype, ptype)
