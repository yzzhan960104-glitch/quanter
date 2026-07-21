# -*- coding: utf-8 -*-
"""策略机器人每日健康度播报（一期 · 纯函数·注入式·可单测）。

内容：颈线法当日扫描信号数 + 参数迭代状态 + 近期回测胜率/回撤/年化。

物理定位：取数由 ``__main__._fetch_strategy_snapshot`` 完成（读 plans/<date>.json +
logs/param_iter_state.json + replay_runs/index.json），本函数零 IO 副作用，仅做
模板渲染与百分比格式化，便于单测。任一字段缺失均降级为「—」或「无记录」文案，绝不抛。

鲁棒性（CLAUDE.md 量化风控·边界审查）：
- ``scan_count`` 非 int（如 None）→ 渲染「—」，不阻断；
- ``param_iter_state`` None / 缺 best_annual / 缺 iter → 对应位降级「—」；
- ``recent_runs`` 空 / 字段类型错 → 渲染「近期无回测记录」或单条「—」。
"""
from __future__ import annotations

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_strategy_brief(date, *, scan_count, param_iter_state, recent_runs) -> BriefResult:
    """生成策略机器人每日健康度 Markdown。

    参数（全部注入式，本函数不读文件/不联网）：
        date: 播报日（YYYY-MM-DD）。
        scan_count: int|None，当日颈线法扫描信号数（plans/<date>.json 的 len(plans)）。
        param_iter_state: dict|None，期望字段 ``best_annual``（float, 如 0.997）
            与 ``iter``（int, 第几轮）；由 ``__main__`` 从真实
            ``logs/param_iter_state.json``（结构 ``{tried: {...}}``）适配而来。
        recent_runs: list[dict]|None，近期回测摘要，每条期望字段
            ``run_id/win_rate/max_drawdown/annualized_return``（与 replay_runs/index.json 同源）。

    返回：BriefResult(date, markdown)。任一字段缺失均降级，绝不抛。
    """
    weekday = _weekday_zh(date)
    recent_runs = recent_runs or []

    # 扫描信号：非 int（None/异常）→ 「—」降级，避免文案出现「None 个」噪声
    sc = scan_count if isinstance(scan_count, int) else "—"
    scan_block = f"- 当日颈线法扫描信号：{sc} 个"

    # 参数迭代：best_annual float → 百分比；iter int → 第 N 轮；缺字段 → 「—」
    pi = param_iter_state or {}
    best = pi.get("best_annual")
    it = pi.get("iter")
    best_s = f"{best * 100:.1f}%" if isinstance(best, (int, float)) else "—"
    iter_s = it if it is not None else "—"
    param_block = f"- 参数迭代最优年化：{best_s}（第 {iter_s} 轮）"

    # 近期回测：最多列 5 条防刷屏；run_id 截前 8 位；胜率/回撤/年化走 _pct 容错
    run_lines = []
    for r in recent_runs[:5]:
        rid = r.get("run_id", "?")[:8]
        wr = _pct(r.get("win_rate"))
        dd = _pct(r.get("max_drawdown"))
        ar = _pct(r.get("annualized_return"))
        run_lines.append(f"- {rid}：胜率 {wr} / 回撤 {dd} / 年化 {ar}")
    runs_block = "\n".join(run_lines) if run_lines else "- 近期无回测记录"

    sections = [
        f"### 🧠 策略机器人 · 每日健康度\n> {date}（{weekday}）\n",
        "**颈线法信号**",
        scan_block,
        param_block,
        "",
        "**近期回测**",
        runs_block,
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)


def _pct(v) -> str:
    """浮点 → 百分比字符串（如 0.55 → 「55.0%」）；None/异常 → 「—」。"""
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"
