# -*- coding: utf-8 -*-
"""brief 共享工具（BriefResult / Markdown 清洗 / 中文周几）。

历史：本模块曾是「每日行情播报文案生成器」（build_daily_brief + 大盘/板块/资金流/龙虎榜
四节）。2026-07-26 market 机器人下线，market 专用代码全删；保留下列三个被
brief_data / brief_trading / brief_strategy 共享 import 的工具：
  - BriefResult：播报结果 dataclass（date + markdown）
  - _clean_markdown：钉钉 Markdown 防御性清洗（去 <font>/<br>/表格分隔行）
  - _weekday_zh：日期 → 中文周几

物理定位：纯函数·零 IO 副作用·可单测。任一调用方失败均由调用方自己降级，本模块不抛。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class BriefResult:
    """播报结果（纯数据，供 __main__ 推送/日志/去重）。"""

    date: str       # 播报日（应播日）
    markdown: str   # 拼好并清洗的钉钉 Markdown 文案


def _weekday_zh(date: str) -> str:
    """日期 → 中文周几（如「周二」；解析失败返空串，不抛）。"""
    try:
        return "周" + "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
    except Exception:
        return ""


def _clean_markdown(text: str) -> str:
    """钉钉 Markdown 防御性清洗（内联，避免 broadcast→caisen 跨包耦合）。

    钉钉群机器人 Markdown 不支持：<font>着色、<br>、表格分隔行 |---|、代码块。
    brief 本身只用 #/列表/粗体/引用（安全）；本函数防御板块/个股名内混入的特殊字符。
    """
    text = re.sub(r"<font[^>]*>|</font>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*\|[-:\s|]+\|\s*$", "", text, flags=re.MULTILINE)
    return text
