# -*- coding: utf-8 -*-
"""组件8 TableSnapshotCollector：每日每表落点快照（spec §8.2）。

物理意图：回放每日 post_close 后读 state_store 6 表 + plan JSON + review md 的当日计数，
存快照 dict 供 ReportBuilder 生成「每张表落了哪些数据」+ 跑 4 类校验。

为什么 sqlite3 直查而非走 state_store ORM：E2E 校验要的是「真相同源真相」——
绕过 ORM 直接读 SQLite 文件，若 ORM 有 bug（漏写/写错表）才能在此暴露（spec §8 真相源）。
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from trading import state_store, trading_plan


class TableSnapshotCollector:
    """每日每表快照（读 state_store + plan/review 文件）。

    Why __init__ 读 _DEFAULT_DB 而非传 db_path：conftest.isolated_state 已 monkeypatch
    state_store._DEFAULT_DB 到 tmp，本类 import 时刻不读，构造时刻才读 → 命中隔离 DB。
    """

    def __init__(self) -> None:
        # 构造期取值（非 import 期），确保 isolated_state 的 monkeypatch 已生效。
        self._db = state_store._DEFAULT_DB

    def _count(self, con: sqlite3.Connection, sql: str, t_date_iso: str | None = None) -> int:
        """容错计数：表未建 / 列不存在 → OperationalError 返 0（首日空 DB 健壮）。

        参数自适应：sql 内 ? 占位数 0（fill/position 全量）或 1（按日期过滤），
        t_date_iso 仅在含 ? 时传。修正 brief 对无占位 SQL 强传单参的 ProgrammingError。
        """
        try:
            params: tuple = (t_date_iso,) if "?" in sql and t_date_iso else ()
            row = con.execute(sql, params).fetchone()
            return row[0] if row else 0
        except sqlite3.OperationalError:
            # 首日 post_close 时若 DB 未 init_store / 表缺失 → 视为 0 落点（不抛，校验阶段暴露）。
            return 0

    def snapshot(self, t_date: date) -> dict:
        """读 t_date 当日每表落点。

        ⚠️ 偏离 brief 的列名修正（CLAUDE.md 事实审查）：
        - trade_event 表无 trade_date 列，事件时间在 timestamp 列（state_store DDL line 137）。
          brief 写的 date(trade_date) 会 OperationalError 被 _count 吞掉返 0 → 改用 date(timestamp)。
          物理含义不变：当日发生的事件计数；timestamp 即事件落库时间（ISO 含日期）。
        - order 表名是 SQL 关键字，需双引号包裹（state_store DDL line 149 已是 "order"）。
        """
        d = t_date.isoformat()
        snap: dict = {}
        con = sqlite3.connect(self._db)
        con.row_factory = sqlite3.Row
        try:
            # 6 表当日计数
            # trade_event：按 timestamp 列的日期部分过滤（修正 brief 的 trade_date 误用）
            snap["trade_event"] = self._count(
                con, 'SELECT COUNT(*) FROM trade_event WHERE date(timestamp)=date(?)', d)
            # order：trade_date 是真实列（DDL line 153），表名关键字需引号
            snap["order_count"] = self._count(
                con, 'SELECT COUNT(*) FROM "order" WHERE trade_date=?', d)
            # fill 无 trade_date 列（仅 traded_time/applied_at），全量计数——
            # 跨日对账由 ReportBuilder._check_consistency 用 order.FILLED vs fill 全量近似（精确对账 V7 实算）
            snap["fill"] = self._count(con, 'SELECT COUNT(*) FROM fill', d)
            # position：当前持仓行（qty>0），非历史归零
            snap["position"] = self._count(con, 'SELECT COUNT(*) FROM position WHERE qty>0', d)
            # account_daily：当日权益快照（PK account_id+date，一行/账户/日）
            snap["account_daily"] = self._count(
                con, 'SELECT COUNT(*) FROM account_daily WHERE date=?', d)
            # trade_event 按动作分组（事件链覆盖：ORDERED/FILLED/CANCELLED/CLOSED...）
            try:
                rows = con.execute(
                    'SELECT action, COUNT(*) c FROM trade_event WHERE date(timestamp)=date(?) '
                    'GROUP BY action', (d,)).fetchall()
                snap["trade_event_by_action"] = {r["action"]: r["c"] for r in rows}
            except sqlite3.OperationalError:
                snap["trade_event_by_action"] = {}
            # order 终态分布（PENDING/FILLED/CANCELLED/REJECTED）
            try:
                rows = con.execute(
                    'SELECT state, COUNT(*) c FROM "order" WHERE trade_date=? GROUP BY state',
                    (d,)).fetchall()
                snap["order_by_state"] = {r["state"]: r["c"] for r in rows}
            except sqlite3.OperationalError:
                snap["order_by_state"] = {}
        finally:
            con.close()
        # plan JSON：trading_plan.load_plan(date_str)，返 None/{"orders":[...], "confirmed":...}
        plan = trading_plan.load_plan(d)
        snap["plan_orders"] = len(plan["orders"]) if plan else 0
        snap["plan_confirmed"] = plan.get("confirmed") if plan else None
        # review md 存在性（review_report.save_review 落盘 logs/trading_reviews/review_<date>.md）。
        # ⚠️ 偏离 brief 的目录修正：review_report._DEFAULT_REVIEW_DIR="logs/trading_reviews"
        # （brief 写 "logs/reviews" 会永久 False，掩盖复盘产出），此处对齐真实默认目录。
        snap["review_md_exists"] = (Path("logs/trading_reviews") / f"review_{d}.md").exists()
        return snap
