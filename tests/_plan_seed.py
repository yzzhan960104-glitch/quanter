# -*- coding: utf-8 -*-
"""共享 DB 计划种子 helper（N3 · 三处手写同构的单一归宿）。

物理意图（新债 #6 · 死种子项，2026-08-16 清偿）：DG-5（2026-08-12，11616220）后生产
``load_plan`` 关闭 JSON 读侧 fallback，只读 DB trade_event(SIGNAL).meta——
``tests/_legacy_plan_io.save_plan_legacy`` 的 JSON 镜像对 ``load_plan`` 读回恒不可见
（死种子：写盘 ≠ 生效）。存量红根因（T15 钉死）：只写 JSON 时 ``load_plan`` 返
None → ``_tp_purpose`` 拿不到 tp1 → SELL@tp1 被误判为市价卖单立即 FILLED，resting
限价语义整体失效。故测试若要 pre_open/_stoploss/review 读到计划参数，必须落
DB SIGNAL（meta 携带 tp1/take_profit 等 ``_tp_purpose`` 契约输入）+ CONFIRMED（确认闸）。

此前该 DB 双写逻辑在三处手写同构（~90% 重复）：
    - tests/e2e_long_cycle/test_probabilistic_broker.py::_seed_plan_truth
    - tests/e2e_long_cycle/test_e2e_long_cycle.py::_fake_run_eod_phase
    - tests/e2e_long_cycle/test_table_snapshot.py（测试体内联）
meta 组装公式（``{**o, plan_date, strategy_name, rationale}``）散在三处，改公式漏改
任意一处即互相脱节（种子形状漂移 → 断言口径漂移）。本 helper 收口为单一真相源，
meta 形状与三处原手写逐字保形（同键同序同 json.dumps 参数）。

``tests/_legacy_plan_io.py`` 保留不动：纯 JSON 流测试（attribution/snapshot 老断言直接
读 plan_*.json）仍用 save_plan_legacy；本 helper 的 json_mirror 只是「顺手补一份展示
镜像」，不是 JSON 读回路径的复活——DB 是唯一有效种子。

账户口径（原三处分歧的收口）：默认 ``engine._resolve_account_id()``（与生产 eod_plan
同口径：QMT_ACCOUNT_ID env 优先，缺失走 state_store 默认账户）；table_snapshot 等
需要固定账户的调用方用 ``account_id=`` 显式覆盖，不依赖 env 解析结果。
"""
from __future__ import annotations


def seed_plan(date_iso: str, orders: list[dict], *, confirmed: bool = True,
              json_mirror: bool = False, account_id: str | None = None) -> int:
    """落 C2c 生产同构的 DB 计划种子：逐单 insert_trade_event(SIGNAL, meta) [+ CONFIRMED]。

    与生产 eod_plan 同构的种子形状（Why：pre_open/_stoploss 的 C2c 真相源读路径按
    trade_id 后缀 + meta 键名取数，形状不一致即「种子写了但读不到」）：
        - meta = json.dumps({**o, "plan_date": date_iso, "strategy_name": "neckline",
          "rationale": ""}, ensure_ascii=False)——order_dict 全量展开（tp1/take_profit/
          stop_price 等是 ``_tp_purpose`` 限价单判定的契约输入，**不能简化**）+ C1 补的
          三个归因字段；
        - 逐单先 SIGNAL 再 CONFIRMED：保证 latest_action=CONFIRMED（确认闸通过；
          ``confirmed=False`` 时不写 CONFIRMED，latest 停在 SIGNAL——table_snapshot
          老断言的精确复刻）。

    Why DB 而非 save_plan_legacy（死种子钉死）：DG-5 后 JSON 对 ``load_plan`` 不可见
    ——DB 是唯一有效种子；本 helper 是三处手写同构的单一归宿。

    Args:
        date_iso: 计划日（YYYY-MM-DD = T+1 生效日，与 trade_id 后缀同口径）。
        orders: 计划单列表（每项即原 order_dict：{"order": {...}, "stop_price": ...}）。
        confirmed: True 逐单补写 CONFIRMED（过确认闸）；False 只写 SIGNAL。
        json_mirror: True 额外经 ``save_plan_legacy`` 落 JSON 镜像（confirmed 同参）——
            仅供直接读 plan_*.json 的老断言路径（attribution/snapshot）；对 ``load_plan``
            恒不可见。默认 False（DB-only，与生产 C3 后行为一致）。
        account_id: 显式账户覆盖；默认 ``engine._resolve_account_id()``（跟生产口径）。

    Returns:
        种子订单数（len(orders)，供 fake eod 返回 n_orders 等复用）。
    """
    import json

    from trading import engine as engine_mod, state_store
    from tests._legacy_plan_io import save_plan_legacy

    # JSON 镜像先落（与原三处手写时序一致：save_plan_legacy 在 DB 双写之前）——
    # 消费方互不感知（load_plan 只读 DB，JSON 读方不读 DB），时序仅为保形。
    if json_mirror:
        save_plan_legacy(date_iso, orders, confirmed=confirmed)

    # 账户口径：显式覆盖 > 生产同构解析（env QMT_ACCOUNT_ID 优先 > state_store 默认）。
    # 账户行不存在则补建（guarded upsert，幂等）——trade_event UNIQUE 键含 account_id，
    # 无账户行直接写事件会外键悬空（生产 eod_plan 同款前置）。
    aid = account_id or engine_mod._resolve_account_id()
    if state_store.get_account(aid) is None:
        state_store.upsert_account(aid, broker="qmt")
    for o in orders:
        sym = o["order"]["symbol"]
        # trade_id 单点：build_trade_id（{account_id}_{symbol}_{date}）——与
        # eod_plan/pre_open/veto 完全一致口径，否则 get_latest_action 查的 trade_id
        # 与写 SIGNAL 的 trade_id 对不上，确认闸/veto 防线双双失效。
        tid = state_store.build_trade_id(aid, sym, date_iso)
        # meta 逐字保形（三处原手写的同一公式）：order_dict 全量展开 + C1 三字段。
        meta_obj = {**o, "plan_date": date_iso, "strategy_name": "neckline",
                    "rationale": ""}
        state_store.insert_trade_event(
            aid, tid, sym, "SIGNAL", meta=json.dumps(meta_obj, ensure_ascii=False))
        if confirmed:
            # 先 SIGNAL 后 CONFIRMED：保 latest_action=CONFIRMED（pre_open 确认闸放行）。
            state_store.insert_trade_event(aid, tid, sym, "CONFIRMED")
    return len(orders)
