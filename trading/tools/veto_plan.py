# -*- coding: utf-8 -*-
"""交易计划否决工具（全自动模式的刹车 · pre_open 前手动调）。

物理意图：
    AUTO_CONFIRM_PLAN=true 全自动模式下，eod_plan 落盘即 confirmed=True，pre_open 次日
    直挂。研究员若对某单/全部有异议，必须在 pre_open（09:22）前调本工具否决——这是
    全自动模式下唯一的人审刹车（opt-out 红线：默认挂，否决才拦）。

    - 不传 symbol → confirmed=False（pre_open 跳过整个 plan，全不挂）
    - 传 symbol  → 从 orders 删该 symbol（pre_open 挂其余，仅拦这一单）

用法：
    .venv310/Scripts/python.exe trading/tools/veto_plan.py              # 否决今日全部
    .venv310/Scripts/python.exe trading/tools/veto_plan.py 2026-07-28   # 否决指定日全部
    .venv310/Scripts/python.exe trading/tools/veto_plan.py 2026-07-28 300654.SZ  # 仅否决该票

幂等：重复否决不报错（已 confirmed=False / 已无该 symbol → 提示后返 0）。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

# 三层 dirname：本脚本 → tools → trading → 项目根（与 trigger_eod_once.py 同范式）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_PROJECT_ROOT)  # TRADE_PLAN_DIR 默认 logs/trading_plans 相对路径，须 cwd=项目根
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from trading import state_store, trading_plan
# C-6 V3：单一时间源（CLI 今日默认走 clock.today，与 engine.py V2 同款）。
from trading import clock


def _resolve_account_id() -> str:
    """解析当前账户 ID（与 engine._resolve_account_id 同口径，避免循环 import 复制一份）。

    物理意图：veto 写 trade_event 需要归属账户。优先读 QMT_ACCOUNT_ID（与 engine / eod_plan
    口径一致），缺失（dry_run）时退 state_store 默认账户。**必须与 engine.py 一致**——
    否则 veto 写 account_A，pre_open 读 account_B，VETOED 防线对不上 trade_id 而失效。
    """
    aid = os.getenv("QMT_ACCOUNT_ID")
    return aid if aid else state_store._DEFAULT_ACCOUNT_ID


def _ensure_account(account_id: str) -> None:
    """确保 account 行存在（trade_event FK 引用，与 eod_plan/_pre_open_impl 同款兜底）。

    init_store 只建表不插行；缺 account 行直接 insert_trade_event 会 IntegrityError。
    """
    state_store.init_store()
    if state_store.get_account(account_id) is None:
        state_store.upsert_account(account_id, broker="qmt")


def _veto_symbol_in_db(account_id: str, symbol: str, date: str) -> None:
    """DB trade_event(VETOED) 真相源写入（幂等 UNIQUE，重复不报错）。

    trade_id 口径 ``state_store.build_trade_id(account_id, symbol, date)`` —— 与
    _pre_open_impl / eod_plan / trading_plan.confirm_plan 完全一致，否则
    get_latest_action(trade_id) 查不到防线失效。Fix1 rework：迁移 build_trade_id 单点。
    """
    trade_id = state_store.build_trade_id(account_id, symbol, date)
    state_store.insert_trade_event(account_id, trade_id, symbol, "VETOED")


def veto(date: str, symbol: str | None = None) -> int:
    """否决某日计划：symbol=None 全否（confirmed=False），传 symbol 单否（删该 order）。

    W2 · DB+JSON 双写（DB 真相源先写，JSON 镜像后写）：
        物理：研究员 pre_open 前人审刹车。DB trade_event(VETOED) 是真相源——pre_open 既有
        防线（engine._pre_open_impl ``get_latest_action(trade_id)=="VETOED"`` 跳过挂单）
        据此拦标的，eod_plan 重跑据 ``get_latest_action != "VETOED"`` 不复活 CONFIRMED。
        JSON 镜像保留供 CLI/钉钉展示。

        全否（symbol=None）：plan 级 confirmed=False 拦整批，**同时** 为 plan 里每个 symbol
        批量写 DB VETOED（标的级镜像）——双保险，即使 confirmed 标志被误改回 True，DB
        VETOED 仍拦每个标的（真相源优先于展示镜像）。
        单否（symbol=X）：DB 写该 symbol 的 VETOED + JSON 从 orders 删该 symbol。

    失败语义（红线）：DB 写失败 → **抛错退出**，绝不「JSON 改了 DB 没记」。
    全自动模式下 pre_open 据放行，veto 假成功 = 人审刹车失效 = 不可逆实盘敞口。
    故 DB 先写、JSON 后写，DB 失败时 JSON 保持原状。

    Returns:
        0=否决成功（含幂等已否）；1=无计划。
    """
    plan = trading_plan.load_plan(date)
    if plan is None:
        print(f"[veto] 无计划 {date}（未落盘或损坏），无需否决")
        return 1

    account_id = _resolve_account_id()
    # ① DB 真相源先写（失败即抛错，不碰 JSON）。
    # _ensure_account 必须在所有 DB 写之前（FK 源），失败=基础设施故障，抛错退出。
    _ensure_account(account_id)

    if symbol is None:
        # 全否：为 plan 里每个 symbol 批量写 DB VETOED（标的级镜像）。
        # 物理：confirmed=False 是 plan 级拦截，但 pre_open 防线是 trade_id 级查询——
        # 必须为每个 symbol 写 VETOED，防线才拦得住每个 trade_id（不只靠 confirmed=False）。
        for o in plan.get("orders", []):
            sym = (o.get("order") or {}).get("symbol")
            if sym:
                _veto_symbol_in_db(account_id, sym, date)
        # ② JSON 镜像后写（DB 已落，JSON 失败不影响真相源）。
        # 幂等：confirmed 已 False 视作已否，DB 仍写一遍（INSERT OR IGNORE 幂等）。
        plan["confirmed"] = False
        trading_plan._plan_path(date).write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[veto] 已否决 {date} 全部（confirmed=False，DB+JSON 双写，pre_open 将跳过）")
        return 0

    # 单否：DB 写该 symbol 的 VETOED（真相源），再从 JSON orders 删该 symbol（镜像）。
    _veto_symbol_in_db(account_id, symbol, date)
    # ② JSON 镜像后写：从 orders 删该 symbol（pre_open 挂其余）。
    before = len(plan.get("orders", []))
    plan["orders"] = [o for o in plan.get("orders", [])
                      if o.get("order", {}).get("symbol") != symbol]
    after = len(plan["orders"])
    trading_plan._plan_path(date).write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    if before == after:
        # 幂等：JSON 已无该 symbol（DB 已写 VETOED 幂等）。视作成功，不报错。
        print(f"[veto] 已否决 {date} {symbol}（DB VETOED 已写；JSON 已无该单，幂等）")
    else:
        print(f"[veto] 已否决 {date} {symbol}（{before}→{after} 单，DB+JSON 双写，pre_open 挂其余）")
    return 0


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else clock.today()
    symbol = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(veto(date, symbol))
