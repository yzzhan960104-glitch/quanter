# -*- coding: utf-8 -*-
"""数据机器人每日健康度播报（一期 · 纯函数·注入式·可单测）。

内容：35 数据集健康度统计（healthy/stale/missing/failed/syncing 计数）+ 最老 lag + 异常清单。
数据源注入：__main__ 调 data_service 取 datasets 列表传入（与 GET /data/datasets 同源）。
"""
from __future__ import annotations

from collections import Counter

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_data_brief(
    date: str,
    *,
    datasets: list[dict] | None,
    freshness: list | None = None,
    ready_signal: bool | None = None,
) -> BriefResult:
    """渲染数据机器人每日播报 Markdown（健康度 + 可选实时性段 + 就绪单口）。

    参数：
        date:      播报日（YYYY-MM-DD）。
        datasets:  健康度快照（key/status/freshness_hours，来自 data_service.list_datasets）。
        freshness: 数据实时性检查结果列表（FreshnessResult，来自 data.freshness.check_freshness）。
                   None 或空列表 → 跳过实时性段（向后兼容，Task5 前的调用语义不变）。
        ready_signal: W5 数据就绪单口判定（state_store.get_ready(date)），True=内容校验①
                   AND pipeline 台账② 全绿。None=未注入（跳过该段，向后兼容）。

    双口径物理意图（Task5）：
    - 健康度口径（被动）：看 parquet mtime 新不新鲜——会被「刚重写但内容仍是旧数据」骗过。
    - 实时性口径（主动）：比对交易日历期望日 vs 数据湖内容最新日，真正回答「T/T-1 到没到」。
      两个口径互补：健康度看管线有没有动，实时性看数据对不对，单口径都有盲区。

    W5 就绪单口（spec #13 · T10）：健康度+实时性都是「观测口径」，pre_open 挂单决策
    用 get_ready（内容校验① + pipeline 台账②）。brief 补一行单口信号让研究员一眼对账
    「观测 healthy vs 决策 ready」是否一致——暴露 spec #13「播报 healthy、挂单拒」漂移。
    """
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

    # ── 实时性段（Task5 新增）──
    # Why 放健康度统计与异常清单之间：运营先看健康度概览 → 再看实时性（T/T-1 落湖了吗）
    # → 再看异常明细，三层信息自上而下从粗到细，符合「先总后分」阅读习惯。
    # freshness 为 None/空 → 跳过整段（保持 Task5 前的 markdown 完全一致，零回归）。
    # FreshnessResult 字段：key / ok / latest_date / expected_date / message；
    # ok=True 用 ✅，False 用 ⚠️（让告警状态一眼可见，与异常清单的 emoji 风格一致）。
    freshness_lines: list[str] = []
    if freshness:
        freshness_lines.append("**数据实时性**（T/T-1 落湖检查）")
        for fr in freshness:
            mark = "✅" if fr.ok else "⚠️"
            # latest_date 缺失（parquet 读失败/不存在）→ 渲染「缺失」，避免「None」污染文案
            latest_s = fr.latest_date if fr.latest_date else "缺失"
            freshness_lines.append(
                f"- {mark} {fr.key}：最新 {latest_s} / 期望 {fr.expected_date}"
            )
        freshness_lines.append("")

    # ── W5 就绪单口段（spec #13 · T10）──
    # 物理意图：健康度+实时性是「观测口径」（mtime + 内容最新日），pre_open 挂单决策
    # 用 get_ready（内容校验① + pipeline 台账②）。补一行单口信号让研究员对账
    # 「观测 healthy vs 决策 ready」——若 healthy 但 ready=False 即暴露 spec #13 漂移。
    # ready_signal=None → 跳过（向后兼容，T10 前调用语义不变）。
    ready_line = ""
    if ready_signal is not None:
        mark = "✅" if ready_signal else "⚠️"
        verdict = "就绪（内容+台账双绿）" if ready_signal else "未就绪（内容或台账漂移，详见日志）"
        ready_line = f"\n**挂单就绪单口**：{mark} {verdict}\n"

    sections = [
        f"### 📊 数据机器人 · 每日健康度\n> {date}（{weekday}）\n",
        f"**健康分**：{health_pct}（{healthy}/{total} healthy）· {oldest}",
        "",
        f"**状态分布**：{summary}",
        "",
        *freshness_lines,
        ready_line,
        "**异常数据集**",
        bad_block,
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)
