# -*- coding: utf-8 -*-
"""策略机器人每日健康度播报（一期 · 纯函数·注入式·可单测）。

内容：颈线法当日扫描信号数 + 参数迭代状态 + 近期回测胜率/回撤/年化。

物理定位：取数由 ``__main__._fetch_strategy_snapshot`` 完成（读
logs/trading_plans/plan_<T+1>.json + experiment.db ACTIVE（参数迭代状态单一真相源）+
data/replay_tasks.db；2026-08-03 起回测结果以 SQLite 为单一真相源，replay_runs/
JSON 归档仅作只读回退），本函数零 IO 副作用，仅做
模板渲染与百分比格式化，便于单测。任一字段缺失均降级为「—」或「无记录」文案，绝不抛。

鲁棒性（CLAUDE.md 量化风控·边界审查）：
- ``scan_count`` 非 int（如 None）→ 渲染「—」，不阻断；
- ``param_iter_state`` None / 缺 experiment_id / 缺 version → 对应位降级「—」；
- ``recent_runs`` 空 / 字段类型错 → 渲染「近期无回测记录」或单条「—」。
"""
from __future__ import annotations

from datetime import datetime

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_strategy_brief(date, *, scan_count, param_iter_state, recent_runs,
                         now=None) -> BriefResult:
    """生成策略机器人每日健康度 Markdown。

    参数（全部注入式，本函数不读文件/不联网）：
        date: 播报日（YYYY-MM-DD）。
        scan_count: int|None，当日颈线法扫描信号数（T 日盘后 EOD 落盘计划文件的
            len(orders)，见 __main__ 候选链）。
        param_iter_state: dict|None，两种形态：
            - 实验中心形态（B3 2026-08-05 起唯一真相源）：``experiment_id``（str）+
              ``version``（int）+ 可选 ``best_annual``（float，ACTIVE 的 outer 去偏年化）；
            - legacy 形态（已退役，仅单测兜底）：``best_annual``（float, 如 0.997）+
              ``iter``（int, 第几轮）。生产取数已不注入 legacy 形态（双轨治理收口）。
        recent_runs: list[dict]|None，近期回测摘要，每条期望字段
            ``run_id/win_rate/max_drawdown/annualized_return``（与 replay_runs/index.json 同源）。

    返回：BriefResult(date, markdown)。任一字段缺失均降级，绝不抛。
    """
    weekday = _weekday_zh(date)
    recent_runs = recent_runs or []

    # 扫描信号：非 int（None/异常）→ 「—」降级，避免文案出现「None 个」噪声
    sc = scan_count if isinstance(scan_count, int) else "—"
    scan_block = f"- 当日颈线法扫描信号：{sc} 个"

    # 参数迭代：实验中心形态优先（当前生效实验 + 版本 + outer 年化）；
    # legacy 形态兜底（best_annual float → 百分比；iter int → 第 N 轮）。
    pi = param_iter_state or {}
    exp_id = pi.get("experiment_id")
    if exp_id:
        # 实验中心单一真相源（2026-08-03）：播报展示 ACTIVE 版本 + outer 去偏年化，
        # 不再展示 legacy param_iter 的 kelly 口径数字（115.8% 类不可实现收益）。
        # experiment_id 形如 "neckline_disc_20260725_25c602"：尾部含 trial 短码，
        # 截尾部 8 位便于人眼追溯 discovery 来源（前段 "neckline_disc_" 无区分度）。
        exp_short = str(exp_id)[-8:]
        ver = pi.get("version")
        ver_s = f"v{ver}" if isinstance(ver, int) else "—"
        best = pi.get("best_annual")
        ann_s = f"（outer 年化 {best * 100:.1f}%）" if isinstance(best, (int, float)) else ""
        param_block = f"- 当前生效实验：{exp_short}（{ver_s}）{ann_s}"
    else:
        best = pi.get("best_annual")
        it = pi.get("iter")
        best_s = f"{best * 100:.1f}%" if isinstance(best, (int, float)) else "—"
        iter_s = it if it is not None else "—"
        param_block = f"- 参数迭代最优年化：{best_s}（第 {iter_s} 轮）"

    # 近期回测：最多列 5 条防刷屏；run_id 截前 8 位；胜率/年化走 _pct 容错。
    # 回撤口径（2026-08-02 对齐 compute_unit/summary.py）：replay_runs 的
    # max_drawdown 是**累计 rr 峰谷（风险倍数）**，不是净值百分比——旧实现把它
    # 当百分比渲染（如单笔亏损 rr=-1 → 显示「回撤 0.0%」，既漏算又错单位）。
    # n_hits=0 的回测无交易样本，胜率/回撤/年化全是 0.0 无意义 → 显式「无成交」。
    # 进行中任务（pending=True）置顶提示「回测进行中」，不冒充已完成结果。
    # 新鲜度（2026-08-03）：最近完成回测距今 >7 天 → 显式标注「N 天前」，
    # 让「近期回测」的滞后可见（旧数据 + 无提示 = 被误读为当天的健康度）。
    run_lines = []
    latest_dt = None
    for r in recent_runs[:5]:
        if r.get("pending"):
            run_lines.append("- 回测进行中：任务已提交，等待调度器完成")
            continue
        rid = r.get("run_id", "?")[:8]
        n_hits = r.get("n_hits")
        ca = r.get("created_at")
        try:
            dt = datetime.fromisoformat(str(ca)) if ca else None
        except ValueError:
            dt = None
        if dt is not None and (latest_dt is None or dt > latest_dt):
            latest_dt = dt
        if isinstance(n_hits, int) and n_hits <= 0:
            run_lines.append(f"- {rid}：无成交")
            continue
        wr = _pct(r.get("win_rate"))
        dd = _fmt_dd(r.get("max_drawdown"))
        ar = _pct(r.get("annualized_return"))
        run_lines.append(f"- {rid}：胜率 {wr} / 回撤 {dd} / 年化 {ar}")
    # 新鲜度提示：最近完成回测距今 >7 天（now 注入，缺省 datetime.now()）
    now = now or datetime.now()
    stale_note = ""
    if latest_dt is not None:
        age_days = (now - latest_dt).days
        if age_days > 7:
            stale_note = (
                f"\n> ⚠️ 最近回测已是 {age_days} 天前（{latest_dt.date().isoformat()}）"
            )
    runs_block = "\n".join(run_lines) if run_lines else "- 近期无回测记录"

    sections = [
        f"### 🧠 策略机器人 · 每日健康度\n> {date}（{weekday}）\n",
        "**颈线法信号**",
        scan_block,
        param_block,
        "",
        "**近期回测**",
        runs_block + stale_note,
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)


def _pct(v) -> str:
    """浮点 → 百分比字符串（如 0.55 → 「55.0%」）；None/异常 → 「—」。"""
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_dd(v) -> str:
    """回测 max_drawdown（P1-1 起为净值百分比，∈[-1, 0]）→ 「-12.0%」；异常/越界 → 「—」。

    2026-08-05：legacy 报告 max_drawdown 是累计 rr 口径（如 -412.62），越界即不渲染，
    避免把 rr 当百分比显示成 -41262%（重算在 broadcast.__main__ 数据源侧完成）。
    """
    try:
        v = float(v)
        if v < -1.0 or v > 0.0:
            return "—"
        return f"{v * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"
