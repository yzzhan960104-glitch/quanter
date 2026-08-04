# -*- coding: utf-8 -*-
"""研究摘要生成器（research_digest · 2026-08-03 Phase B 原型）。

物理意图（观察环）：每天盘后给 Agent/人一个"一眼可决策"的摘要：
    1. 实盘成交清洗（state_store.fill 真相源，只保留 strategy 非空；A3 切 DB）；
    2. 回测期望（replay_tasks.db 最近 SUCCESS 报告统计）；
    3. 漂移对比（胜率/均 rr 与期望的差距 → OK / WARN / CRITICAL / 样本不足）；
    4. 数据与实验状态（snapshot data_hash / ACTIVE 实验版本）。

漂移阈值是原型值（模块常量，后续用历史数据标定）：
    MIN_SAMPLE=5           实盘样本不足不判定（防噪声误报）；
    WIN_RATE_WARN=0.15     胜率低于期望 >15pp → WARN；
    WIN_RATE_CRITICAL=0.30 胜率低于期望 >30pp → CRITICAL（fail-closed 信号）；
    RR_WARN=0.30           均 rr 低于期望 >0.3 → WARN。

诚实性：fill 表只存成交回报（无平仓盈亏），win_rate/avg_rr 需 state_store
平仓归因（TODO Phase B 后续接 trading/state_store 的 fill→exit 归因）；字段缺失时
渲染「—」且不做漂移判定，绝不编造实盘表现。
"""
from __future__ import annotations

import csv
import json
import os
import argparse
import asyncio
import logging
import sqlite3
from datetime import date
from pathlib import Path

# 通知适配层（横切：钉钉/企微/Telegram 通道，模块级 import 便于测试 monkeypatch）。
from infra.notifier import build_default_manager, fire_and_forget

logger = logging.getLogger(__name__)

# Phase C 提案工作流（模块级 import：main() 内生成提案，测试可 monkeypatch）。
from research import proposals
from research import discovery_bridge
from experiment.resolver import resolve_active

# 漂移判定阈值（原型值，可标定）
MIN_SAMPLE = 5
WIN_RATE_WARN = 0.15
WIN_RATE_CRITICAL = 0.30
RR_WARN = 0.30


def _pct(v) -> str:
    """浮点比例 → 百分比字符串；None/异常 → 「—」。"""
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_rr(v) -> str:
    """盈亏比渲染：数值保留 2 位小数；None/异常 → 「—」。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_int(v) -> str:
    """整数渲染：None → 「—」（占位字段不显示 None 噪声）。"""
    return "—" if v is None else str(v)


def _fmt_num(v, ndigits: int = 2) -> str:
    """数值渲染（保留 ndigits 位小数）；None/异常 → 「—」。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):.{ndigits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(v) -> str:
    """金额渲染：None → 「—」；数值带正负号与两位小数。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):+,.2f}"
    except (TypeError, ValueError):
        return "—"


def _drift_status(live: dict, exp: dict) -> str:
    """实盘 vs 期望的粗粒度漂移判定（OK/WARN/CRITICAL/样本不足）。"""
    n = live.get("n_hits") or 0
    if n < MIN_SAMPLE:
        return "样本不足"
    flags = []
    wr = live.get("win_rate")
    exp_wr = exp.get("win_rate")
    if isinstance(wr, (int, float)) and isinstance(exp_wr, (int, float)):
        diff = exp_wr - wr
        if diff > WIN_RATE_CRITICAL:
            flags.append("CRITICAL")
        elif diff > WIN_RATE_WARN:
            flags.append("WARN")
    rr = live.get("avg_rr")
    exp_rr = exp.get("avg_rr")
    if isinstance(rr, (int, float)) and isinstance(exp_rr, (int, float)):
        if exp_rr - rr > RR_WARN:
            flags.append("WARN")
    return "OK" if not flags else " / ".join(dict.fromkeys(flags))


def build_digest(digest_date, live: dict, expectation: dict | None,
                 data_hash: str = "", active_experiment: str = "",
                 discovery_status: dict | None = None) -> str:
    """组装研究摘要 Markdown（纯函数，零 IO）。

    参数：
        digest_date: 摘要日（YYYY-MM-DD）。
        live: 实盘摘要 dict（n_hits/win_rate/avg_rr/avg_holding_bars/n_skipped/
              same_day_both/stop_gap；win_rate/avg_rr 可为 None=未接入平仓归因）。
        expectation: 回测期望 dict（task_id/window/n_hits/win_rate/avg_rr/
                     max_drawdown/annualized_return）或 None（无 SUCCESS 回测）。
        data_hash: 快照数据内容指纹（P1-3，审计数据版本）。
        active_experiment: 当前 ACTIVE 实验 id（播报/审计同源）。
        discovery_status: discovery 进展 dict（load_discovery_status 产物）；
            None → 探索段渲染「—」。
    """
    status = _drift_status(live, expectation or {}) if expectation else "无回测期望"
    exp_block = "—"
    if expectation:
        src = expectation.get("params_source")
        if src == "ACTIVE":
            src_note = "（ACTIVE 参数 ✓）"
        elif src == "OTHER":
            src_note = "（⚠️ 非 ACTIVE 参数，不代表实盘期望）"
        else:
            src_note = ""
        exp_block = (
            f"- 回测期望（task {expectation.get('task_id', '?')[:8]}，"
            f"窗口 {expectation.get('window', '?')}）{src_note}：\n"
            f"- 期望：成交 {expectation.get('n_hits', '—')} 笔 / "
            f"胜率 {_pct(expectation.get('win_rate'))} / "
            f"均 rr {_fmt_rr(expectation.get('avg_rr'))}"
        )
    lines = [
        f"# 研究摘要 {digest_date}",
        "",
        "## 实盘表现（fill 去重归因）",
        f"- 实盘成交：{live.get('n_hits', '—')} 笔",
        f"- 胜率：{_pct(live.get('win_rate'))}",
        f"- 均 rr：{_fmt_rr(live.get('avg_rr'))}",
        f"- 已实现盈亏（TP 平仓，金额）：{_fmt_money(live.get('pnl_sum'))}"
        f"（{_fmt_int(live.get('pnl_events'))} 笔）",
        f"- 平仓事件：{_fmt_int(live.get('n_closed'))} 笔",
        f"- 事件计数：跳过 {_fmt_int(live.get('n_skipped'))} / "
        f"同日竞争 {_fmt_int(live.get('same_day_both'))} / "
        f"跳空止损 {_fmt_int(live.get('stop_gap'))}",
        "",
        "## 回测期望",
        exp_block,
        "",
        "## 漂移对比",
        f"- 状态：**{status}**",
        "",
        "## 数据与实验状态",
        f"- 数据版本（data_hash）：{data_hash or '—'}",
        f"- 当前生效实验：{(active_experiment or '—')[-8:]}",
        "",
        "## 参数探索（discovery 低功率）",
        _discovery_block(discovery_status),
    ]
    return "\n".join(lines)


def _discovery_block(st: dict | None) -> str:
    """discovery 进展段渲染（trial 数/最新 run/k 进度/新冠军）。"""
    if not st or not st.get("latest_run"):
        return "- 暂无探索记录（低功率 daemon 尚未完成首轮）"
    run = st["latest_run"]
    ch = st.get("champion") or {}
    inner = ch.get("inner") or {}
    outer = ch.get("outer") or {}
    lines = [
        f"- 已评估 trial：{st.get('n_trials', '—')}",
        f"- 最新 run：{str(run.get('run_id', '?'))[:8]}，本轮 {run.get('n_trials', '—')} 组，"
        f"frontier={run.get('frontier_size_prev', '—')}，k={run.get('k_rounds_no_expansion', '—')}",
        f"- 新冠军：calmar={_fmt_num(inner.get('calmar'))} "
        f"outer ann={_pct(outer.get('ann'))}",
    ]
    return "\n".join(lines)


def load_live_fills(db_path: str | None = None) -> list[dict]:
    """读 state_store.fill 并清洗（A3 切 DB · 保 strategy 非空过滤，新断点-4）。

    签名变更：csv_path → db_path（spec §A3 已同步修订）。fill 表 A1 加 strategy 列，
    本函数保留原 CSV 时代的 strategy 非空过滤（只保留有归因的成交，丢弃补录空行），
    digest 实盘样本口径不变。DB 异常 → []（不抛、不阻断摘要生成）。

    物理意图（2026-08-03 实盘流水实证 + 2026-08-05 A3 切真相源）：原 CSV 时代同笔
    成交回报会重复落盘（08-02/08-03 出现同 timestamp/symbol/shares/price 多行），
    且有无 strategy 归因的补录行——观察环输入必须先按 (timestamp, symbol, shares, price)
    去重，否则 n_hits 虚高、漂移检测输入是脏的。A3 切 state_store.fill 后，
    UNIQUE(order_id, traded_time) 已天然去重（spec §2.4 真相源），本函数只需做
    strategy 过滤 + 时间戳规范化（YYYYMMDDHHMMSS → YYYY-MM-DD HH:MM:SS）。
    """
    try:
        from trading import state_store
        state_store.init_store(db_path)
        rows = state_store.query_fills("2000-01-01", "2099-12-31", db_path=db_path)
    except Exception:
        logger.exception("load_live_fills 读 state_store 失败，返空")
        return []
    fills = []
    for r in rows:
        strategy = (r.get("strategy") or "").strip()  # A1 fill.strategy 列
        if not strategy:  # 保口径：补录空 strategy 行丢弃（新断点-4）
            continue
        tt = str(r.get("traded_time") or "")
        # fill.traded_time 存 YYYYMMDDHHMMSS 整数串，规范化为 CSV 时代同款
        # YYYY-MM-DD HH:MM:SS（消费端 build_digest 时间序展示用，下游已适配）
        ts = (f"{tt[0:4]}-{tt[4:6]}-{tt[6:8]} {tt[8:10]}:{tt[10:12]}:{tt[12:14]}"
              if len(tt) >= 14 else tt)
        fills.append({
            "timestamp": ts,
            "symbol": r.get("symbol", ""),
            "direction": (r.get("direction") or "").upper(),
            "shares": r.get("shares"),
            "price": r.get("price"),
            "strategy": strategy,
            "rationale": "",
            "kind": "fill",
        })
    return fills


def load_backtest_expectation(db_path: str | None = None) -> dict | None:
    """读 replay_tasks.db 回测报告 → 期望 dict（含 task_id/window/params_source）。

    2026-08-05 修复（窗口/参数误导实证）：原实现取「最近 SUCCESS」——08-03 手动
    提交的默认参数验证任务（7146fdce，窗口 07-01~08-03）覆盖了 ACTIVE 参数周度
    任务，digest 期望显示 26%/-0.45 与实盘 ACTIVE（25c602）不符。现改为：
        1. 优先选 cfg_override 与当前 resolve_active() params 一致的最近 SUCCESS
           → params_source="ACTIVE"（可信）；
        2. 无匹配 → 取最近 SUCCESS 并标注 params_source="OTHER"（⚠️ 不代表实盘）；
        3. 无 SUCCESS / DB 缺失 → None（期望段降级）。
    """
    try:
        _active = resolve_active()
        _active_params = dict(_active[0].params) if _active else None
    except Exception:
        _active_params = None
    _norm = lambda p: json.dumps(p or {}, sort_keys=True, ensure_ascii=False)
    _target = _norm(_active_params) if _active_params is not None else None
    try:
        from backtest import tasks_db as replay_tasks_db
        replay_tasks_db.init_db(db_path)
        tasks = replay_tasks_db.list_tasks(limit=50, path=db_path) or []
        active_match = None
        other = None
        for t in tasks:
            if t.get("status") != "SUCCESS" or not t.get("report_json"):
                continue
            # tasks_db._row_to_dict 已把 cfg_override 反序列化为 dict（空 → {}）；
            # 兼容字符串形态（老库/直查），容错失败置 None（不参与 ACTIVE 匹配）。
            cfg = t.get("cfg_override") or {}
            if isinstance(cfg, str):
                try:
                    cfg = json.loads(cfg)
                except (TypeError, ValueError):
                    cfg = None
            if _target is not None and cfg is not None and _norm(cfg) == _target:
                if active_match is None:
                    active_match = t
            elif other is None:
                other = t
        picked = active_match if active_match is not None else other
        if picked is None:
            return None
        r = json.loads(picked["report_json"])
        return {
            "task_id": picked["task_id"],
            "window": f"{picked.get('start', '?')}~{picked.get('end', '?')}",
            "params_source": "ACTIVE" if picked is active_match else "OTHER",
            "n_hits": r.get("n_hits"),
            "win_rate": r.get("win_rate"),
            "avg_rr": r.get("avg_rr"),
            "max_drawdown": r.get("max_drawdown"),
            "annualized_return": r.get("annualized_return"),
        }
    except Exception:
        return None

def load_live_perf_from_state_store(db_path: str = "logs/trading_state.db") -> dict:
    """从 state_store 读已实现盈亏与平仓事件（诚实口径，2026-08-03）。

    物理意图：fill 表只有成交回报（无盈亏），而 state_store.trade_event
    的 TP1_FILLED/TP2_FILLED 行带 realized_pnl（金额 = filled_qty×(卖价−开仓均价)），
    CLOSED 行目前只标事件（realized_pnl=None，post_close 先标、pnl 后续从 fill 算）。
    本 loader：
        - pnl_events / pnl_sum：realized_pnl 非空的平仓事件数与金额合计；
        - n_closed：CLOSED 事件数；
        - win_rate / avg_rr：恒 None——金额口径无风险基准（无每笔风险金额），
          且 CLOSED 行无 pnl，绝不猜测胜率（等 post_close 补 realized_pnl 后再算）。
    """
    out = {"n_closed": 0, "pnl_events": 0, "pnl_sum": None,
           "win_rate": None, "avg_rr": None}
    try:
        from trading.state_store import init_store
        init_store(db_path)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT COUNT(*) c,"
                " SUM(CASE WHEN action='CLOSED' THEN 1 ELSE 0 END) closed,"
                " COUNT(realized_pnl) pnl_events, SUM(realized_pnl) pnl_sum"
                " FROM trade_event WHERE action IN"
                " ('CLOSED','TP1_FILLED','TP2_FILLED')").fetchone()
            if row is not None:
                out["n_closed"] = int(row["closed"] or 0)
                out["pnl_events"] = int(row["pnl_events"] or 0)
                if row["pnl_sum"] is not None:
                    out["pnl_sum"] = float(row["pnl_sum"])
        finally:
            con.close()
    except Exception:
        pass   # 库缺失/表未建 → 默认零值（digest 渲染「—」，不抛）
    return out


def summarize_fills(fills: list[dict]) -> dict:
    """fill 列表 → 实盘摘要 dict（成交笔数 + 事件计数占位）。

    诚实性：CSV 只有成交回报，win_rate/avg_rr 返回 None（待 state_store 平仓归因，
    TODO Phase B）。n_skipped/same_day_both/stop_gap 来自回测 metadata 的事件口径，
    实盘侧对应统计留 Phase B（引擎事件链落库后接入）。
    """
    return {
        "n_hits": len(fills),
        "win_rate": None,
        "avg_rr": None,
        "avg_holding_bars": None,
        "pnl_sum": None,
        "pnl_events": 0,
        "n_closed": 0,
        "n_skipped": None,
        "same_day_both": None,
        "stop_gap": None,
    }


def push_digest(md: str, sink: list | None = None) -> None:
    """钉钉推送研究摘要（build_default_manager 装配通道 + 同步等待发送完成）。

    ⚠️ 独立子进程上下文（cron 拉起）无通道装配：必须显式 build_default_manager()
    读 .env 装钉钉通道（daemon 同款坑），否则 get_default() 懒构造 _channels=[] 静默
    丢消息。Why asyncio.run 同步等待而非 fire_and_forget：digest 是短命子进程，
    daemon 线程可能在进程退出时被掐断导致消息丢失；同步等待保证"推送完成才退出"。
    sink 仅供测试收集（生产 None）。
    """
    # ⚠️ 独立子进程（cron 拉起）不经过 python -m trading 的 .env 加载——
    # 必须先 load_dotenv 再装配通道，否则 DINGTALK_WEBHOOK/SECRET 读不到，
    # build_default_manager 会静默装 0 通道（本次开发实测 0/0）。
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)   # 已存在的进程环境变量优先，不覆盖
    except Exception:
        pass   # 无 dotenv 环境 → 跳过（通道装配会因凭证缺失而空，日志可见）
    mgr = build_default_manager()
    results = asyncio.run(mgr.notify_risk_event(md, "INFO"))
    ok = sum(1 for r in results if r is None)
    print(f"[push] 通道投递结果：成功 {ok}/{len(results)}")
    if not results:
        logger.warning("research digest 推送无可用通道（钉钉凭证未装配？）")
    if sink is not None:
        sink.append(md)


def main(argv=None) -> str:
    """入口：读 live CSV + state_store 盈亏 + 回测期望 → 写 digest + 可选钉钉推送。

    --push：cron 用（DETACHED 子进程拉起），推送钉钉后落盘 docs/research_digest.md
    （幂等：直接覆盖当日文件）。
    """
    ap = argparse.ArgumentParser(description="research_digest 研究摘要生成器（观察环）")
    ap.add_argument("--push", action="store_true",
                    help="生成后推钉钉（cron 周期同步用）")
    ap.add_argument("--proposals", action="store_true",
                    help="生成 0-2 条研究提案并追加到摘要（Phase C Agent 交互）")
    ap.add_argument("--out", default="docs/research_digest.md", help="输出路径")
    args = ap.parse_args(argv)

    fills = load_live_fills()
    live = summarize_fills(fills)
    live.update(load_live_perf_from_state_store())
    expectation = load_backtest_expectation()
    # ACTIVE 实验 id（experiment 单一真相源；失败返回空串，摘要降级「—」）
    active_exp = ""
    try:
        from experiment.resolver import resolve_active
        active_list = resolve_active()
        if active_list:
            active_exp = active_list[0].experiment_id
    except Exception:
        pass
    md = build_digest(str(date.today()), live, expectation, active_experiment=active_exp,
                      discovery_status=discovery_bridge.load_discovery_status())
    if args.proposals:
        pid = proposals.generate_proposal(
            proposals._DEFAULT_DB, md, proposals.list_proposals(proposals._DEFAULT_DB))
        if pid:
            p = proposals.get_proposal(proposals._DEFAULT_DB, pid)
            md += (
                "\n\n## 今日提案\n"
                f"- {pid}（A 档）：{p['hypothesis']}\n"
                f"- 参数：{p['params_json']}\n"
                f"- 预期：{p['expected_effect'] or '—'} / 风险：{p['risk'] or '—'}\n"
                f"- 回复「通过 {pid}」或「否决 {pid}」")
    if args.push:
        push_digest(md)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(md)
    return md


if __name__ == "__main__":
    main()
