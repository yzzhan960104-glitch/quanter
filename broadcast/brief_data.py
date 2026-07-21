# -*- coding: utf-8 -*-
"""数据机器人每日健康度播报（一期 · 纯函数·注入式·可单测）。

内容：35 数据集健康度统计（healthy/stale/missing/failed/syncing 计数）+ 最老 lag + 异常清单。
数据源注入：__main__ 调 data_service 取 datasets 列表传入（与 GET /data/datasets 同源）。
"""
from __future__ import annotations

from collections import Counter

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_data_brief(date: str, *, datasets: list[dict] | None) -> BriefResult:
    datasets = datasets or []
    weekday = _weekday_zh(date)
    cnt = Counter(d.get("status", "unknown") for d in datasets)

    # 健康分：healthy 占比
    total = len(datasets)
    healthy = cnt.get("healthy", 0)
    health_pct = f"{healthy / total * 100:.0f}%" if total else "—"

    # 异常清单（非 healthy 的）
    bad = [d for d in datasets if d.get("status") != "healthy"]
    bad_lines = []
    for d in bad[:15]:
        key = d.get("key", "?")
        st = d.get("status", "?")
        lag = d.get("freshness_hours")
        lag_s = f"（lag {lag:.0f}h）" if isinstance(lag, (int, float)) else ""
        bad_lines.append(f"- {key}：{st}{lag_s}")
    bad_block = "\n".join(bad_lines) if bad_lines else "- 全部健康 ✅"

    # 最老 lag
    lags = [d.get("freshness_hours") for d in datasets if isinstance(d.get("freshness_hours"), (int, float))]
    oldest = f"最老数据 lag {max(lags):.0f} 小时" if lags else "无 lag 数据"

    summary = " / ".join(f"{k} {v}" for k, v in sorted(cnt.items()))
    sections = [
        f"### 📊 数据机器人 · 每日健康度\n> {date}（{weekday}）\n",
        f"**健康分**：{health_pct}（{healthy}/{total} healthy）· {oldest}",
        "",
        f"**状态分布**：{summary}",
        "",
        "**异常数据集**",
        bad_block,
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)
