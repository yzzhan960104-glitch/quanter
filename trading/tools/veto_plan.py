# -*- coding: utf-8 -*-
"""交易计划否决工具（全自动模式的刹车 · pre_open 前手动调）。

物理意图：
    AUTO_CONFIRM_PLAN=true 全自动模式下，eod_plan 写 DB CONFIRMED 即放行，pre_open 次日
    直挂。研究员若对某单/全部有异议，必须在 pre_open（09:22）前调本工具否决——这是
    全自动模式下唯一的人审刹车（opt-out 红线：默认挂，否决才拦）。

    SSoT Phase C · C3：veto 只写 DB trade_event(VETOED)（spec §6 真相源），不改 JSON。
    pre_open/load_plan 据 ``get_latest_action(trade_id)=VETOED`` 跳过标的，eod_plan 重跑
    据 ``!= VETOED`` 不复活 CONFIRMED。

    - 不传 symbol → 全否（为 plan 里每个 symbol 批量写 DB VETOED）
    - 传 symbol  → 单否（仅写该 symbol 的 DB VETOED，pre_open 拦该 trade_id）

用法：
    .venv310/Scripts/python.exe trading/tools/veto_plan.py              # 否决今日全部
    .venv310/Scripts/python.exe trading/tools/veto_plan.py 2026-07-28   # 否决指定日全部
    .venv310/Scripts/python.exe trading/tools/veto_plan.py 2026-07-28 300654.SZ  # 仅否决该票

幂等：重复否决不报错（DB INSERT OR IGNORE 幂等，提示后返 0）。
"""
from __future__ import annotations

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


# H3/T2 收口（2026-08-12）：单一真相源 trading/account.py，不再本地复制。
from trading.account import resolve_account_id as _resolve_account_id


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
    _pre_open_impl / eod_plan 完全一致，否则
    get_latest_action(trade_id) 查不到防线失效。Fix1 rework：迁移 build_trade_id 单点。
    """
    trade_id = state_store.build_trade_id(account_id, symbol, date)
    state_store.insert_trade_event(account_id, trade_id, symbol, "VETOED")


def veto(date: str, symbol: str | None = None) -> int:
    """否决某日计划：symbol=None 全否，传 symbol 单否（spec §6 C3 改只写 DB VETOED）。

    SSoT Phase C · C3（spec §6）：veto 只写 DB trade_event(VETOED) 真相源，不再改 JSON。
    物理：研究员 pre_open 前人审刹车。pre_open 防线（engine._pre_open_impl
    ``get_latest_action(trade_id)=="VETOED"`` 跳过挂单）据 DB 拦标的，eod_plan 重跑据
    ``get_latest_action != "VETOED"`` 不复活 CONFIRMED。C2c 切 DB 后 pre_open/load_plan
    均读 DB 真相源——JSON 镜像退役（C3 后 JSON 是导出产物，非真相源）。

    全否（symbol=None）：为 plan 里每个 symbol 批量写 DB VETOED（load_plan 据 latest_action
    =VETOED → confirmed=False 整体拦，pre_open per-symbol 也拦每个 trade_id）。
    单否（symbol=X）：仅写该 symbol 的 DB VETOED（pre_open per-symbol 拦该 trade_id）。

    失败语义（红线）：DB 写失败 → **抛错退出**，绝不假成功。
    全自动模式下 pre_open 据放行，veto 假成功 = 人审刹车失效 = 不可逆实盘敞口。

    Args:
        date:   计划日（YYYY-MM-DD）。
        symbol: None 全否；传 ts_code 单否。

    Returns:
        0=否决成功（含幂等已否）；1=无计划（DB 无 SIGNAL 行，load_plan 返 None）。
    """
    plan = trading_plan.load_plan(date)
    if plan is None:
        print(f"[veto] 无计划 {date}（DB 无 SIGNAL 且无 JSON 回退），无需否决")
        return 1

    account_id = _resolve_account_id()
    # DB 真相源先写（失败即抛错，绝不假成功）。
    # _ensure_account 必须在所有 DB 写之前（FK 源），失败=基础设施故障，抛错退出。
    _ensure_account(account_id)

    if symbol is None:
        # 全否：为 plan 里每个 symbol 批量写 DB VETOED（per-trade_id 拦截）。
        # 物理：pre_open 防线是 trade_id 级查询——必须为每个 symbol 写 VETOED，
        # 防线才拦得住每个 trade_id（C3 load_plan 据 latest_action=VETOED → confirmed=False
        # 整体拦 + pre_open per-symbol 再拦一次双保险）。
        n_vetoed = 0
        for o in plan.get("orders", []):
            sym = (o.get("order") or {}).get("symbol")
            if sym:
                _veto_symbol_in_db(account_id, sym, date)
                n_vetoed += 1
        print(f"[veto] 已否决 {date} 全部（{n_vetoed} 个 symbol 写 DB VETOED，pre_open/load_plan 将跳过）")
        return 0

    # 单否：仅写该 symbol 的 DB VETOED（pre_open per-symbol 拦该 trade_id）。
    _veto_symbol_in_db(account_id, symbol, date)
    print(f"[veto] 已否决 {date} {symbol}（DB VETOED 已写，pre_open 拦该 trade_id）")
    return 0


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else clock.today()
    symbol = sys.argv[2] if len(sys.argv) > 2 else None
    sys.exit(veto(date, symbol))
