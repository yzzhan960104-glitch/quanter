# -*- coding: utf-8 -*-
"""蔡森形态学交互可视化数据装配（lightweight-charts JSON 契约）。

物理定位（CLAUDE.md 极简 + 显式原则）：
    本模块是蔡森形态学流水线的"前端图表数据装配器"——把一个 TradePlan（dict 形态）
    与对应价格 DataFrame 装配为 lightweight-charts 前端库可直接消费的 JSON dict，
    供 server /caisen/plans/{plan_id}/chart 端点返回 + 前端 CaisenScreenView 渲染。

    本模块只做"数据契约装配"，不做任何渲染/识别/下单。所有坐标（形态点 P1-P4/P6、
    颈线、止损、止盈）均取自 plan dict + price_df，无任何二次推导。

lightweight-charts 数据契约（以官方文档为准，不臆造字段名）：
    candle:  {time, open, high, low, close}
             time 类型：UTCTimestamp（秒）或 'yyyy-mm-dd' 商业日字符串。
             本模块用 'yyyy-mm-dd' 字符串（日 K 级，与 A 股交易日粒度匹配，
             前端 lightweight-charts 自动按时间轴去重排序）。
    marker:  {time, position, color, shape, text}
             position: 'aboveBar' | 'belowBar' | 'inBar' | 'atPointTop' | 'atPointBottom'
             shape:    'circle' | 'square' | 'arrowUp' | 'arrowDown'
             text:     悬浮提示文本（前端 tooltip）
    priceLine: {price, color, lineWidth, lineStyle, axisLabelVisible, title}
             lineStyle: 0(Solid) | 1(Dotted) | 2(Dashed) | 3(LargeDashed) | 4(SparseDotted)
             lineWidth: 1 | 2 | 3 | 4
             title: 轴标签文字（价格轴右侧）

    markers/priceLines 数组顺序即前端绘制顺序（lightweight-charts 内部按时间排序 markers）。

防御性边界（CLAUDE.md 量化风控·边界审查）：
    - pattern_points 缺失：markers 返回空列表（不抛异常），candles/priceLines 仍正常
      装配（早期/历史计划无形态点时前端仍能看 K 线 + 关键价位）；
    - price_df 行数与形态点 idx 越界：逐点 try/except 跳过，不阻断整批 markers 装配；
    - time 字符串化统一用 strftime('%Y-%m-%d')，避免时区/微秒精度污染前端排序。
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

_logger = logging.getLogger(__name__)


def build_chart_data(plan: dict, price_df: pd.DataFrame) -> dict:
    """装配 lightweight-charts JSON: candles + markers + priceLines。

    参数：
        plan:     蔡森交易计划（dict 形态，对齐 TradePlan + storage 序列化字段）。
                  形态点从 plan["metadata"]["pattern_points"] 取（W 底 P1-P4 / 头肩底 P1-P6）。
        price_df: OHLCV DataFrame（列 Open/High/Low/Close + DatetimeIndex）。
                  形态点 idx 为 price_df 的整数位置下标（与 render_plan_png 共享契约）。

    返回：
        dict，三键：
            candles:    [{time, open, high, low, close}, ...] K 线序列；
            markers:    [{time, position, color, shape, text}, ...] 形态点标注
                        （W 底 4 个 / 头肩底 6 个，按 P1..PN 顺序）；
            priceLines: [{price, color, lineWidth, lineStyle, title}, ...] 关键价位水平线
                        （止损/止盈/颈线/突破/底部价）。

    降级行为：
        plan 无 pattern_points metadata → markers=[]，candles/priceLines 仍正常。
        （保证前端 CaisenScreenView 在历史计划无形态点时不白屏。）
    """
    # —— 1. candles K 线序列装配 ——
    candles = _build_candles(price_df)

    # —— 2. markers 形态点标注装配（依赖 pattern_points metadata）——
    markers = _build_markers(plan, price_df)

    # —— 3. priceLines 关键价位水平线装配 ——
    price_lines = _build_price_lines(plan)

    return {
        "candles": candles,
        "markers": markers,
        "priceLines": price_lines,
    }


# ---------------------------------------------------------------------------
# 内部辅助：candles K 线序列装配
# ---------------------------------------------------------------------------
def _build_candles(df: pd.DataFrame) -> list[dict]:
    """price_df → [{time, open, high, low, close}, ...] lightweight-charts candle 序列。

    列名兼容：首字母大写（Open）/ 全小写（open）/ 中文（开盘）统一识别。
    time 用 strftime('%Y-%m-%d') 商业日字符串（日 K 级，避免 UTCTimestamp 时区陷阱）。
    """
    # 列名查找：首字母大写 > 全小写 > 中文（与 viz_static._normalize_ohlc_columns 对齐）
    def _col(*candidates: str) -> str:
        for c in candidates:
            if c in df.columns:
                return c
        raise KeyError(f"OHLCV 列缺失，候选：{candidates}")

    open_c, high_c, low_c, close_c = (
        _col("Open", "open", "开盘"),
        _col("High", "high", "最高"),
        _col("Low", "low", "最低"),
        _col("Close", "close", "收盘"),
    )

    candles: list[dict] = []
    for ts, row in df.iterrows():
        ts_pd = pd.Timestamp(ts)
        candles.append({
            "time": ts_pd.strftime("%Y-%m-%d"),
            "open": float(row[open_c]),
            "high": float(row[high_c]),
            "low": float(row[low_c]),
            "close": float(row[close_c]),
        })
    return candles


# ---------------------------------------------------------------------------
# 内部辅助：markers 形态点标注装配
# ---------------------------------------------------------------------------
def _build_markers(plan: dict, df: pd.DataFrame) -> list[dict]:
    """从 plan.metadata.pattern_points 装配 lightweight-charts markers。

    返回 [{time, position, color, shape, text}, ...]，按 P1..PN 顺序。
    缺 pattern_points 返回空列表（降级，不抛异常）。

    物理意图：蔡森形态点的可视化标注——W 底 P1(左底)/P3(右底) 用 belowBar+arrowUp
    （低位向上箭头，强调"底"），P2(颈线峰)/P4(突破峰) 用 aboveBar+arrowDown
    （高位向下箭头，强调"峰"）。头肩底同理：底用 arrowUp，峰用 arrowDown。
    """
    points = _extract_pattern_points(plan)
    if not points:
        return []

    markers: list[dict] = []
    for label in _ordered_point_labels(points):
        pt = points[label]
        if not isinstance(pt, dict):
            continue
        idx = pt.get("idx")
        price = pt.get("price")
        if idx is None or price is None:
            continue
        # idx 越界防御：形态点 idx 超出 price_df 范围时跳过（不阻断整批 markers）
        if not isinstance(idx, (int, float)) or idx < 0 or idx >= len(df):
            _logger.debug("markers 形态点 %s idx=%s 越界(0,%s)，跳过", label, idx, len(df))
            continue

        ts = pd.Timestamp(df.index[int(idx)])
        # 峰谷判定：用 pattern_type + label 位置推断 position/shape
        # W 底：P1=底 P2=峰 P3=底 P4=峰；头肩底：P1=峰 P2=底 P3=峰 P4=底 P5=峰 P6=底
        is_peak = _is_peak_point(plan.get("pattern_type", ""), label)
        if is_peak:
            position = "aboveBar"
            color = "#cc6600"   # 橙：峰
            shape = "arrowDown"
        else:
            position = "belowBar"
            color = "#009933"   # 绿：底
            shape = "arrowUp"

        markers.append({
            "time": ts.strftime("%Y-%m-%d"),
            "position": position,
            "color": color,
            "shape": shape,
            "text": f"{label.upper()} {float(price):.2f}",
        })

    return markers


def _is_peak_point(pattern_type: str, label: str) -> bool:
    """根据 pattern_type + label 判定该点是否为"峰"（用于 marker position/shape）。

    蔡森形态点定义（与 caisen/patterns/w_bottom.py、head_shoulder.py 对齐）：
        W 底（pivot 序列 谷-峰-谷-峰）：
            P1=谷(左底), P2=峰(颈线高点), P3=谷(右底), P4=峰(突破峰)
        头肩底（pivot 序列 峰-谷-峰-谷-峰-谷-峰）：
            P1=峰(起点), P2=谷(左肩底), P3=峰(左颈), P4=谷(头底),
            P5=峰(右颈), P6=谷(右肩底)

    返回 True=峰（aboveBar + arrowDown）；False=谷（belowBar + arrowUp）。
    """
    try:
        n = int(label[1:])
    except (ValueError, IndexError):
        return False

    if pattern_type == "w_bottom":
        # W 底：奇数 P1/P3=谷，偶数 P2/P4=峰
        return n % 2 == 0
    if pattern_type == "head_shoulder":
        # 头肩底：奇数 P1/P3/P5=峰，偶数 P2/P4/P6=谷
        return n % 2 == 1
    # 未知 pattern_type：默认按 W 底规则（多数场景为 W 底，保守降级）
    return n % 2 == 0


# ---------------------------------------------------------------------------
# 内部辅助：priceLines 关键价位装配
# ---------------------------------------------------------------------------
def _build_price_lines(plan: dict) -> list[dict]:
    """从 plan 字段装配 lightweight-charts priceLines（止损/止盈/颈线/突破/底部价）。

    返回 [{price, color, lineWidth, lineStyle, title}, ...]。
    lineStyle 枚举（lightweight-charts 官方）：
        0=Solid, 1=Dotted, 2=Dashed, 3=LargeDashed, 4=SparseDotted
    """
    lines: list[dict] = []

    def _add(price_key: str, color: str, line_width: int, line_style: int, title: str) -> None:
        v = plan.get(price_key)
        if v is not None and not pd.isna(v):
            lines.append({
                "price": float(v),
                "color": color,
                "lineWidth": line_width,
                "lineStyle": line_style,
                "axisLabelVisible": True,
                "title": title,
            })

    # 顺序即前端绘制顺序（关键价位从上到下）
    _add("take_profit_2x", "#009933", 1, 2, "第二波满足")   # 绿虚线
    _add("take_profit",    "#009933", 2, 0, "止盈·第一波满足") # 绿实线加粗
    _add("breakout_price", "#0066cc", 1, 2, "突破价")        # 蓝虚线
    _add("neckline_price", "#ff8800", 1, 0, "颈线")          # 橙实线
    _add("bottom_price",   "#888888", 1, 2, "C波低点")       # 灰虚线
    _add("stop_loss",      "#cc0000", 2, 0, "止损")          # 红实线加粗

    return lines


# ---------------------------------------------------------------------------
# 内部辅助：pattern_points 提取（与 viz_static 共享契约，本模块独立实现避免循环依赖）
# ---------------------------------------------------------------------------
def _extract_pattern_points(plan: dict) -> dict:
    """从 plan.metadata.pattern_points 提取形态点 dict（容错多层嵌套）。

    与 viz_static._extract_pattern_points 完全一致，此处复制而非 import 以保持
    两模块职责独立（静态/交互可视化未来可能拆分独立包，避免跨依赖耦合）。
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
    """按 P1..PN 顺序返回 label 列表（与 viz_static 共享逻辑）。"""
    labels = [k for k in points.keys() if isinstance(k, str) and k.lower().startswith("p")]

    def _num(label: str) -> int:
        try:
            return int(label[1:])
        except (ValueError, IndexError):
            return 999
    return sorted(labels, key=_num)
