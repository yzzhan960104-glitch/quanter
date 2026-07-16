# -*- coding: utf-8 -*-
"""每日行情播报文案生成器（spec §5.5/§5.6 · 纯函数·可单测）。

物理定位：取数(DataLakeReader) + pandas 聚合 + 模板渲染 → Markdown 字符串。
零 IO 副作用（不碰网络/不写文件）；reader 与 resolver 注入式，便于单测。

内容四节（spec 决策 3 MVP）：
1. 大盘 8 宽基：index_daily 当日 close + 近 2 日 close 现算涨跌幅（index_daily 无 pct 列）。
2. 板块 Top5/Bottom5：ths_daily 自带 pct_change，直接排序。
3. 主力净流入 Top5：moneyflow net_mf_amount 降序（tushare moneyflow 标准单位万元 → /1e4 转亿）。
4. 龙虎榜：dragon_list hit==1 标的代码列表（落湖仅 hit 标记，无明细）。

鲁棒性（spec §6 边界拷问）：
- 缺数据降级：任一湖 get_cross_section 返空 DF（lake_reader 离线契约 lake_reader.py:252）
  → 该节渲染「（XX 数据未落湖，跳过）」，其余节照常，绝不抛。
- NaN 守护：涨跌幅样本不足 / NaN / 除零 → 渲染「—」，不崩。
- 名称：全部经 name_resolver 转（未命中返原 code，文案代码兜底）。
- Markdown 钉钉子集：#/列表/粗体/引用，禁表格/<font>/---；_clean_markdown 防御性清洗。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from broadcast import name_resolver as _default_resolver

logger = logging.getLogger(__name__)

# 涨跌幅回看窗口（天）：覆盖周末/节假日，保证 tail(2) 拿到 2 个交易日。
_PCT_LOOKBACK_DAYS = 10


@dataclass
class BriefResult:
    """播报结果（纯数据，供 __main__ 推送/日志/去重）。"""

    date: str       # 播报日（应播日，通常 index_daily 最新日）
    markdown: str   # 拼好并清洗的钉钉 Markdown 文案


def build_daily_brief(
    date: str,
    *,
    reader,
    resolver=_default_resolver,
) -> BriefResult:
    """生成某日行情播报 Markdown。

    参数：
        date: 播报日（YYYY-MM-DD）。
        reader: DataLakeReader（或测试 fake），提供 get_cross_section / get_timeseries。
        resolver: name_resolver 模块（默认 broadcast.name_resolver，测试可注入 fake）。

    返回：BriefResult(date, markdown)。任一节数据缺失均降级，绝不抛。
    """
    weekday = _weekday_zh(date)
    sections = [
        "**大盘宽基**",
        _section_index(date, reader, resolver),
        "",
        "**板块涨幅榜（同花顺概念）**",
        _section_ths(date, reader, resolver),
        "",
        "**主力资金净流入 Top5**",
        _section_moneyflow(date, reader, resolver),
        "",
        "**龙虎榜**",
        _section_dragon(date, reader, resolver),
    ]
    header = f"### 📈 Quanter · 每日行情播报\n> {date}（{weekday}）收盘 · 数据截至 {date}\n"
    footer = "\n> 数据来源 Tushare data_lake · 下次播报明日 19:00"
    markdown = _clean_markdown(f"{header}\n" + "\n".join(sections) + footer)
    return BriefResult(date=date, markdown=markdown)


# ------------------------------------------------------------------ 工具

def _weekday_zh(date: str) -> str:
    """日期 → 中文周几（如「周二」；解析失败返空串，不抛）。"""
    try:
        return "周" + "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
    except Exception:
        return ""


def _start_date(date: str, lookback: int = _PCT_LOOKBACK_DAYS) -> str:
    """date 前 lookback 天（涨跌幅回看窗口起点）。失败返 date 自身。"""
    try:
        return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=lookback)).strftime("%Y-%m-%d")
    except Exception:
        return date


def _fmt_pct(close_prev, close_curr) -> str:
    """涨跌幅格式化：▲1.23% / ▼0.45%；样本不足 / NaN / 除零 → 「—」。"""
    try:
        if close_prev is None or close_curr is None:
            return "—"
        prev, curr = float(close_prev), float(close_curr)
        if prev != prev or curr != curr or prev == 0:  # NaN 或除零
            return "—"
        pct = (curr / prev - 1.0) * 100.0
        arrow = "▲" if pct >= 0 else "▼"
        return f"{arrow}{abs(pct):.2f}%"
    except (TypeError, ValueError):
        return "—"


def _safe_tail2(ts) -> tuple:
    """从时序 DF 取最后 2 个非 NaN close，返 (prev, curr)；不足/无 close → 空 tuple。"""
    if ts is None or getattr(ts, "empty", True) or "close" not in ts.columns:
        return ()
    closes = ts["close"].dropna()
    if len(closes) < 2:
        return ()
    return (closes.iloc[-2], closes.iloc[-1])


# ------------------------------------------------------------------ 四节

def _section_index(date: str, reader, resolver) -> str:
    """大盘 8 宽基：当日 close + 近 2 日 close 现算涨跌幅（index_daily 无 pct 列）。"""
    xs = reader.get_cross_section(date, lake="index_daily")
    if xs is None or getattr(xs, "empty", True):
        return "- （大盘数据未落湖，跳过）"
    start = _start_date(date)
    lines = []
    for sym in list(xs.index):
        try:
            close = xs.loc[sym, "close"]
        except (KeyError, IndexError):
            continue
        prev_curr = _safe_tail2(reader.get_timeseries(sym, start, date, lake="index_daily"))
        pct_str = _fmt_pct(*prev_curr) if prev_curr else "—"
        name = resolver.resolve_index_name(sym)
        try:
            close_str = f"{float(close):.2f}"
        except (TypeError, ValueError):
            close_str = "—"
        lines.append(f"- {name}：{close_str} {pct_str}")
    return "\n".join(lines) if lines else "- （大盘数据未落湖，跳过）"


def _section_ths(date: str, reader, resolver) -> str:
    """板块 Top5/Bottom5：ths_daily 自带 pct_change，直接排序。"""
    xs = reader.get_cross_section(date, lake="ths_daily")
    if xs is None or getattr(xs, "empty", True) or "pct_change" not in xs.columns:
        return "- （板块数据未落湖，跳过）"
    top = xs.sort_values("pct_change", ascending=False).head(5)
    bot = xs.sort_values("pct_change", ascending=True).head(5)
    top_s = " / ".join(_fmt_board(s, xs.loc[s, "pct_change"], resolver) for s in top.index)
    bot_s = " / ".join(_fmt_board(s, xs.loc[s, "pct_change"], resolver) for s in bot.index)
    return f"- 🔺 Top：{top_s}\n- 🔻 Bottom：{bot_s}"


def _fmt_board(sym, pct, resolver) -> str:
    name = resolver.resolve_ths_name(sym)
    try:
        return f"{name} {float(pct):+.2f}%"
    except (TypeError, ValueError):
        return f"{name} —"


def _section_moneyflow(date: str, reader, resolver) -> str:
    """主力净流入 Top5：moneyflow net_mf_amount 降序（万元 → /1e4 转亿）。"""
    xs = reader.get_cross_section(date, lake="moneyflow")
    if xs is None or getattr(xs, "empty", True) or "net_mf_amount" not in xs.columns:
        return "- （资金流数据未落湖，跳过）"
    top = xs.sort_values("net_mf_amount", ascending=False).head(5)
    lines = []
    for sym in list(top.index):
        name = resolver.resolve_stock_name(sym)
        try:
            yi = float(top.loc[sym, "net_mf_amount"]) / 1e4  # 万元 → 亿
            lines.append(f"- {name} 净流入 {yi:+.2f}亿")
        except (TypeError, ValueError):
            lines.append(f"- {name} 净流入 —")
    return "\n".join(lines) if lines else "- （资金流数据未落湖，跳过）"


def _section_dragon(date: str, reader, resolver) -> str:
    """龙虎榜：dragon_list hit==1 标的代码列表（落湖仅 hit，无原因/金额明细）。"""
    xs = reader.get_cross_section(date, lake="dragon_list")
    if xs is None or getattr(xs, "empty", True) or "hit" not in xs.columns:
        return "- （龙虎榜数据未落湖，跳过）"
    hits = xs[xs["hit"] == 1].index.tolist()
    if not hits:
        return "- 今日无上榜标的"
    names = [resolver.resolve_stock_name(s) for s in hits[:20]]
    return f"- 今日上榜 {len(hits)} 只：{' / '.join(names)}"


def _clean_markdown(text: str) -> str:
    """钉钉 Markdown 防御性清洗（内联，避免 broadcast→caisen 跨包耦合）。

    钉钉群机器人 Markdown 不支持：<font>着色、<br>、表格分隔行 |---|、代码块。
    brief 本身只用 #/列表/粗体/引用（安全）；本函数防御板块/个股名内混入的特殊字符。
    """
    text = re.sub(r"<font[^>]*>|</font>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\|[-:\s|]+\|\s*$", "", text, flags=re.MULTILINE)
    return text
