# -*- coding: utf-8 -*-
"""W1-A/T2 Task 8 临时校验脚本：扫 tests/ 全量 patch，按映射表建议物理路径。

⚠️ 临时脚本：Task 19 Step 4 删除（360 patch 迁移收尾后）。

============================================================================
背景（Task 7 启示 · patch 迁移核心红线）
============================================================================
Task 4-7 切断 _eng_mod 反查后，phases（pre_open/stop_loss/post_close/exit）+
order_state 改顶部 ``from trading.X import Y``（或 ``from trading.X import Y as Z``）
直接 import 物理真身。这会在 phases 模块命名空间绑定【本地引用】Y/Z——

  - 旧形态：phases ``import trading.engine as _eng_mod`` → 内部 ``_eng_mod.Y()``
    → patch("trading.engine.Y") 命中（_eng_mod.Y 经 engine 模块属性解析）。
  - 新形态（Task 4-7 后）：phases ``from trading.X import Y`` → 内部 ``Y()``
    → patch("trading.engine.Y") **失效**（engine.Y 与 phases.Y 是两个独立命名空间）。

正确迁移目标 = phases 实际调用方模块（patch 必须注入到调用方模块命名空间才命中）。
Task 7 fix commit aeb02036 已实证此规则（e2e 6 红→12 绿）。

============================================================================
分类规则（优先级从上到下，命中即归类）
============================================================================
A. **TradingEngine 类成员**：``patch("trading.engine.TradingEngine.X")`` /
   ``setattr(TradingEngine, "X")`` / ``patch.object(TradingEngine, "X")`` →
   engine 自身类成员（_plan_data_keys / _pre_open_gate / _broadcast_positions_pnl 等），
   **不迁**（标「TradingEngine 类·不迁」）。

B. **共享模块对象·属性型**：字符串路径 ``trading.engine.<mod>.<attr>``（<mod> 是
   共享模块对象：calendar/clock/qmt_market_data/position_book/reconcile_job/
   trading_plan/dynamic_whitelist/job_ledger/state_store/_state_store/
   position_book/_position_book/critical/data_ctx/compute.stop/compute.breaker/
   io.breaker/alerting/account/order_state 等）→ monkeypatch 在【共享模块对象】上
   设 attr（engine.<mod> IS trading.<mod> IS phases.<mod>，都指向 sys.modules 同一对象）
   → patch 天然全局命中，**无需迁**（标「共享对象·属性型·无需迁」）。
   典型：``monkeypatch.setattr("trading.engine.calendar.is_trading_day", ...)`` /
   ``patch("trading.engine._state_store.list_signals_with_meta_by_plan_date")``。

C. **from…import 本地绑定型**（Task 7 核心红线）：符号经 phases 顶部 ``from X import Y``
   绑定为本地引用（见 SYMBOL_TO_PHASES 表，扫描 trading/phases/*.py + order_state.py 派生）：
   - C1 单一调用方模块 → 自动建议 ``trading.phases.<module>.<symbol>``（标「自动·单目标」）。
   - C2 多调用方模块 → 列候选 phases，标「需人工判断 phases 调用方」（如 _submit 在
     pre_open/stop_loss/exit 三处用 → 须据 test 语义选）。
   - C3 engine 自身另有定义（``_submit`` L378 / ``get_gateway`` L367 / ``_resolve_account_id``
     L350）→ 既可能被 engine 内部路径（_health_guard / bootstrap / _eod wrapper）读，
     也可能被 phases 读 → 标「需人工判断 engine vs phases」（Task 7 fix 实证：engine 路径
     保 engine.X，phases 路径迁 phases.X，或双口子同 patch）。
     **同构扩展（ENGINE_REEXPORT_INTERNAL_READ）**：``_alert_critical``/``_mode``/``_trade_cfg``
     /``_CriticalHalt``/``_state_store``（整体替换型）—— engine re-export 且 engine 内部经
     模块全局名读（如 _halt ``alert=_alert_critical`` / _submit ``dry_run=(_mode()=="dry_run")``
     / bootstrap ``_state_store.upsert_account``）→ 盲迁 phases 会破 engine 路径测试 →
     同归 C3（engine 保 / phases 迁 / 双口子同 patch 三选）。
   - C4 engine 内部 wrapper 符号（不在 phases 本地表，engine 顶部 from…import 或自建）：
     如 ``eod_plan``/``load_plan``/``get_ready``/``get_data_ready``/``sanity_check_date_alignment``
     /``pre_open``/``stop_loss_monitor``/``post_close``/``_pre_open_impl``（engine re-export +
     _eod/_pre_open/_stoploss/_post_close wrapper 内经模块全局名解析命中）→ **不迁**
     (标「engine 内部 wrapper·不迁」)。

D. **共享模块对象·整体替换型**：``setattr(engine, "<mod>", mock)`` 整体替换 engine 命名空间
   的模块绑定（无 .<attr> 访问器）→ 仅命中 engine 内部代码（engine.<mod>.X 经替换后的 mock），
   phases 本地 <mod> 绑定不受影响 → 标「需人工判断（共享对象·整体替换·仅命中 engine 内部）」。

E. **未识别/孤儿符号**：不在上述任一表 → 标「需人工判断（未知符号）」。

============================================================================
用法
============================================================================
    python tests/trading/_patch_audit.py                 # 默认扫 tests/trading/
    python tests/trading/_patch_audit.py --scope all      # 扫 tests/ 全量
    python tests/trading/_patch_audit.py --file <path>    # 只扫指定文件
    python tests/trading/_patch_audit.py --json           # 输出 JSON（机器可读）
    python tests/trading/_patch_audit.py --out report.md  # 同时写报告文件

退出码：0（成功生成报告）；1（扫描异常）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional, Tuple

# ============================================================================
# 权威符号 → phases 调用方模块 映射表
# ============================================================================
# 派生方法（2026-08-12 re-verified）：扫描 trading/phases/{pre_open,stop_loss,
# post_close,exit}.py + trading/order_state.py 的 ``from X import Y [as Z]`` 语句，
# 取【本地绑定名】（alias 优先）→ phases 模块短名（trading.phases.X / trading.order_state）。
# Task 4-7 切断 _eng_mod 后这些是 phases 实际调用方。
# 维护：phases import 变化时重扫（命令见文件末注释）。
SYMBOL_TO_PHASES: Dict[str, List[str]] = {
    # ---- gateway_service 反查（Task 7 切断）----
    "_submit":             ["phases.pre_open", "phases.stop_loss", "phases.exit"],
    "get_gateway":         ["phases.pre_open", "phases.stop_loss", "phases.post_close"],
    # ---- state_store 模块（共享对象，phases 绑别名 _state_store）----
    "_state_store":        ["phases.pre_open", "phases.stop_loss", "phases.post_close", "phases.exit", "order_state"],
    # ---- critical 集群 A（Task 2 re-export 切断）----
    "_mode":               ["phases.pre_open", "phases.stop_loss", "phases.post_close", "order_state"],
    "_alert_critical":     ["phases.pre_open", "phases.stop_loss", "phases.post_close", "order_state"],
    "_CriticalHalt":       ["phases.pre_open", "phases.stop_loss", "order_state"],
    "_trade_cfg":          ["phases.pre_open"],
    # ---- account SSoT（Task 5 切断）----
    "_resolve_account_id": ["phases.pre_open", "phases.stop_loss", "phases.post_close", "phases.exit", "order_state"],
    # ---- compute.stop（Task 4 切断）----
    "_trading_days_between": ["phases.pre_open", "phases.stop_loss"],
    "trading_days_between":  ["phases.pre_open", "phases.stop_loss"],
    "should_trigger_stop":   ["phases.stop_loss"],
    # ---- io.breaker（Task 4 切断）----
    "_cancel_all_open_orders": ["phases.pre_open", "phases.post_close"],
    "cancel_all_open_orders":  ["phases.pre_open", "phases.post_close"],
    # ---- phases 内部互调（Task 6 切断）----
    "_scan_expired_positions":  ["phases.pre_open"],   # pre_open 别名自 stop_loss
    "scan_expired_positions":   ["phases.pre_open"],   # stop_loss 自身定义方（调用方=stop_loss 时另算）
    "_close_expired_positions": ["phases.pre_open"],
    "close_expired_positions":  ["phases.pre_open"],
    "place_take_profit":        ["phases.stop_loss", "order_state"],
    "_seq_for_real_oid":        ["order_state"],
    "seq_for_real_oid":         ["order_state"],
    "_order_state_to_db":       ["order_state"],
    "order_state_to_db":        ["order_state"],
    "_pre_open_impl":           [],  # engine re-export 专有（phases 内同模块直调，无跨模块 import）
    # ---- strategies.neckline.execution（Task 4 切断）----
    "decide_exit":   ["phases.stop_loss"],
    "ExitAction":    ["phases.stop_loss"],
    "ExitReason":    ["phases.stop_loss"],
    # ---- compute.breaker（post_close 内部）----
    "_check_daily_loss_limit": ["phases.post_close"],
    "check_daily_loss_limit":  ["phases.post_close"],
    # ---- 共享模块对象（from trading import X）—— 整体替换型（D 类）----
    "calendar":         ["phases.stop_loss"],
    "clock":            ["phases.pre_open", "phases.stop_loss", "phases.post_close", "phases.exit", "order_state"],
    "qmt_market_data":  ["phases.stop_loss"],
    "_position_book":   ["phases.stop_loss", "phases.post_close"],
    "position_book":    ["phases.stop_loss", "phases.post_close"],
    "trading_plan":     ["phases.exit"],
    "reconcile_job":    ["phases.post_close"],
    "dynamic_whitelist":["phases.pre_open", "phases.post_close"],
    "job_ledger":       ["phases.pre_open"],
}

# ============================================================================
# engine.py 自身定义的符号（L350-393 + wrapper）—— patch engine.X 命中 engine 内部
# ============================================================================
# 当 patch 目标是这些符号时，engine 内部代码（_health_guard/bootstrap/_eod wrapper）
# 仍经 engine 模块全局名解析命中；phases 走自己的本地绑定（不命中 engine.X）。
# → 归 C3（需人工判断 engine vs phases）。
ENGINE_SELF_DEFINED = {
    "_submit",            # engine.py L378（透传 gateway_service.submit_order）
    "get_gateway",        # engine.py L367（透传 gateway_service.get_gateway）
    "_resolve_account_id",# engine.py L350（透传 trading.account.resolve_account_id）
}

# ============================================================================
# engine re-export 且 engine 内部经模块全局名读取的符号 —— 同构 ENGINE_SELF_DEFINED
# ============================================================================
# 这些符号同时具备三态：
#   ① engine 顶部 from…import re-export 入 engine 命名空间（保 patch("trading.engine.X") 命中）；
#   ② engine 内部（L262 后）经【模块全局名】读取——盲迁 phases 会破 engine 路径测试；
#   ③ phases 顶部亦 from…import 绑本地引用（在 SYMBOL_TO_PHASES 表）。
# 即与 _submit/get_gateway/_resolve_account_id（ENGINE_SELF_DEFINED）同构 → 归 C3
# （engine 保 / phases 迁 / 双口子同 patch 三选）。
#
# 实证（reviewer 定位）：
#   - tests/trading/test_critical_guard.py:24 patch("trading.engine._alert_critical")
#     经 eng._halt 触发 → engine.py L1079 ``alert=_alert_critical``（模块全局名解析）
#     → patch 当前命中。盲迁 phases.X._alert_critical 后 engine 全局不变 → patch 失效 → 红。
#   - _state_store（整体替换型 setattr(engine,"_state_store",mock)）：engine 内部 L840
#     ``_state_store.upsert_account``/L1450 ``list_signals_with_meta_by_plan_date`` 等
#     读 engine 全局 _state_store → patch 命中。盲迁 phases 仅改 phases 本地绑定 → 破 engine 路径。
#
# 成员判定（grep trading/engine.py L262 后内部引用确认，2026-08-12 re-verified）：
#   - _alert_critical : L785/942/992/1051/1069(_halt alert=)/1231/1437/1572/1643（9 处内部读）
#   - _mode           : L393/780/941/991/1050/1436/1571/1635/1642/1647（10 处内部读）
#   - _trade_cfg      : L1481 ``cfg_trade = _trade_cfg()``（1 处内部读；当前 0 patch，收录为规则完整）
#   - _CriticalHalt   : L1611 ``raise _CriticalHalt``（1 处内部读；当前 0 patch，收录为规则完整）
#   - _state_store    : L840/1450/1471/1472（4 处内部读；仅【整体替换型】提升 C3，
#                       【属性型 _state_store.X】仍归 B——共享对象属性天然全局命中，不需迁）
#
# 不收录：_cancel_all_open_orders（engine re-export L80 但 L262 后无内部调用 → 仍归 C2）。
ENGINE_REEXPORT_INTERNAL_READ = {
    "_alert_critical",  # critical 集群 A · _halt alert= 口子（保 engine 全局命中）
    "_mode",            # critical 集群 A · _submit/_health_guard 读
    "_trade_cfg",       # critical 集群 A · _stoploss 内读
    "_CriticalHalt",    # critical 集群 A · bootstrap raise 口子
    "_state_store",     # 共享模块对象 · engine 内部 upsert/list_signals/build_trade_id 读
}

# ============================================================================
# engine 内部 wrapper 符号（engine re-export 或自建，phases 无本地绑定）—— C4 不迁
# ============================================================================
# 这些符号经 engine 顶部 from…import 入 engine 命名空间，engine 内 _eod/_pre_open/
# _stoploss/_post_close wrapper 经模块全局名解析命中；phases 未绑（不在 SYMBOL_TO_PHASES）。
# patch("trading.engine.X") 测 engine wrapper 路径 → 仍命中 → 不迁。
ENGINE_INTERNAL_WRAPPERS = {
    # eod 计划入口（engine._eod wrapper 调 eod_plan(...) → engine.eod_plan 命中）
    "eod_plan", "sanity_check_date_alignment",
    # 计划加载（engine._pre_open wrapper 经 ports 调 load_plan · 顶部 from trading_plan）
    "load_plan",
    # 数据就绪闸（engine._pre_open/_stoploss wrapper 读 get_data_ready/get_ready）
    "get_ready", "get_data_ready",
    # phase 入口（engine cron wrapper 调 pre_open()/stop_loss_monitor()/post_close() 经模块全局名）
    "pre_open", "stop_loss_monitor", "post_close", "_pre_open_impl",
    # data_ctx 集群（engine._eod 内 _load_* 经 re-export 别名命中）
    "load_universe", "_load_universe", "load_df_upto", "_load_df_upto",
    "load_recent_plan_symbols", "_load_recent_plan_symbols",
    "resolve_cooldown_days", "_resolve_cooldown_days",
    "load_integrity_ctx", "_load_integrity_ctx",
    "resolve_id_window", "_resolve_id_window",
    "plan_data_keys",
    # order_state 集群（engine 类内 _handle_order_update 薄 wrapper · 模块级 re-export）
    "handle_order_update", "order_direction", "advance_order_state_from_status",
    # compute.plan
    "build_orders_from_signals",
    # blackout（已收口 ports.blackout · 模块级常量已删 · patch 应失效或迁 ports）
    "_last_quote_blackout_alert_ts", "_QUOTE_BLACKOUT_ALERT_INTERVAL_S",
    # engine 内部方法（TradingEngine 实例/类方法 · 须用 TradingEngine.X 形式 patch）
    "_broadcast_positions_pnl", "_eod", "_pre_open", "_stoploss", "_post_close",
    "_health_guard", "bootstrap",
}

# 共享模块对象名（B 类属性型判定用）：engine.<name> 指向 sys.modules 内同一对象
SHARED_MODULE_OBJECTS = {
    "calendar", "clock", "qmt_market_data", "position_book", "_position_book",
    "reconcile_job", "trading_plan", "dynamic_whitelist", "job_ledger",
    "state_store", "_state_store",  # phases 别名 _state_store / engine 别名 _state_store
    "critical", "data_ctx", "alerting", "account", "order_state", "ports",
    "compute", "compute.stop", "compute.breaker", "compute.plan",
    "io", "io.breaker", "io.orders", "types", "types.order_state",
}

# TradingEngine 实例属性/方法（setattr(eng, "X") 形式 · eng = TradingEngine() 实例变量）——
# 注意：测试中 ``eng`` 既可能是 engine 模块别名（from trading import engine as eng），
# 也可能是 TradingEngine 实例变量（eng = TradingEngine()）。本表收录只在实例上有意义的
# 属性名，分类为 class_stay（engine 自身实例·不迁）。
TRADING_ENGINE_INSTANCE_ATTRS = {
    "_pre_open_gate", "_ports", "_scheduler", "_gateway", "_jobs",
}

# ============================================================================
# 分类类别常量（报告列）
# ============================================================================
CAT_AUTO_SINGLE = "auto_single"              # C1 自动·单目标
CAT_AUTO_MULTI = "auto_multi_judge"           # C2 需人工判断 phases 调用方
CAT_ENGINE_VS_PHASES = "engine_vs_phases_judge"  # C3 需人工判断 engine vs phases
CAT_ENGINE_INTERNAL = "engine_internal_stay"  # C4 engine 内部 wrapper·不迁
CAT_SHARED_ATTR = "shared_attr_no_migrate"   # B 共享对象·属性型·无需迁
CAT_CLASS_STAY = "class_stay"                 # A TradingEngine 类·不迁
CAT_SHARED_WHOLE = "shared_whole_review"      # D 共享对象·整体替换·需判断
CAT_UNKNOWN = "unknown_review"                # E 未知符号·需判断


# ============================================================================
# Patch 检测（正则 · 覆盖 5 种形式）
# ============================================================================
# 检测对象：trading.engine.* 命中的 patch（含模块/类/实例/setattr 各形式）。
# 不可用 ast 直接解析（patch 参数常含局部变量 + 字符串字面量混合），故正则。
#
# ⚠️ 正则逐行匹配，假设 patch 单行（第一参数 + 起始引号在同一物理行）。
# 理论漏检：多行形态 ``patch(\n    "trading.engine.X",\n    ...)``
# 实测 tests/trading/ 369 patch 全部单行（2026-08-12 基准），后续新增 patch
# 若跨行需本脚本重检（或加 re.DOTALL / 预处理 join 行）。


def _normalize_engine_alias(alias: str) -> str:
    """测试中 engine 模块的多种别名归一化为 'engine'（判定 setattr 第一参数是否 engine 模块）。"""
    if alias in ("engine", "eng", "eng_mod", "_eng_mod", "engine_mod", "trading_engine", "te"):
        return "engine"
    return alias


def scan_file(path: str) -> List[Dict]:
    """扫单文件，返回 patch 记录列表。

    每条记录：
      {file, line, col, form, raw, target, full_path, category, suggestion, note}
    """
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except (OSError, UnicodeDecodeError):
        return records

    lines = src.splitlines()
    for ln_idx, line in enumerate(lines, start=1):
        # --- 形式 1: patch("trading.engine.X") ---
        for m in re.finditer(r'\bpatch\(\s*["\']trading\.engine\.([^"\']+)["\']', line):
            target_path = m.group(1)
            records.append(_classify(
                path, ln_idx, m.start(), form='patch("trading.engine.X")',
                raw=line.strip(), target_path=target_path,
            ))
        # --- 形式 2: patch.object(mod_or_cls, "X") ---
        for m in re.finditer(r'\bpatch\.object\(\s*(\w+)\s*,\s*["\'](\w+)["\']', line):
            recv, attr = m.group(1), m.group(2)
            recv_n = _normalize_engine_alias(recv)
            if recv_n == "engine":
                records.append(_classify(
                    path, ln_idx, m.start(), form=f'patch.object({recv}, "X")',
                    raw=line.strip(), target_path=attr,
                ))
            elif recv == "TradingEngine":
                records.append(_classify(
                    path, ln_idx, m.start(), form='patch.object(TradingEngine, "X")',
                    raw=line.strip(), target_path=f"TradingEngine.{attr}",
                ))
        # --- 形式 3: setattr("trading.engine.X", ...) 字符串路径 ---
        for m in re.finditer(r'\.setattr\(\s*["\']trading\.engine\.([^"\']+)["\']', line):
            target_path = m.group(1)
            records.append(_classify(
                path, ln_idx, m.start(), form='setattr("trading.engine.X")',
                raw=line.strip(), target_path=target_path,
            ))
        # --- 形式 4: setattr(engine_alias, "X", ...) 模块对象 ---
        for m in re.finditer(r'\.setattr\(\s*(engine|eng|eng_mod|_eng_mod|engine_mod|trading_engine|te)\s*,\s*["\'](\w+)["\']', line):
            recv, attr = m.group(1), m.group(2)
            records.append(_classify(
                path, ln_idx, m.start(), form=f'setattr({recv}, "X")',
                raw=line.strip(), target_path=attr,
            ))
        # --- 形式 5: setattr(TradingEngine, "X", ...) 类对象 ---
        for m in re.finditer(r'\.setattr\(\s*TradingEngine\s*,\s*["\'](\w+)["\']', line):
            attr = m.group(1)
            records.append(_classify(
                path, ln_idx, m.start(), form='setattr(TradingEngine, "X")',
                raw=line.strip(), target_path=f"TradingEngine.{attr}",
            ))
    return records


def _classify(path: str, line: int, col: int, form: str, raw: str, target_path: str) -> Dict:
    """对单条 patch 记录分类，给出建议 + 注释。

    target_path 形如：
      - "X"（裸符号）
      - "X.Y"（共享对象属性 / TradingEngine.X 类方法 / trading_plan.load_plan 等子路径）
    """
    rec = {
        "file": path.replace(os.sep, "/"),
        "line": line, "col": col,
        "form": form, "raw": raw,
        "target": target_path,
    }

    # ---- A. TradingEngine 类成员 / 类构造器 ----
    if target_path == "TradingEngine" or target_path.startswith("TradingEngine."):
        rec["category"] = CAT_CLASS_STAY
        rec["suggestion"] = ""  # 不迁
        if target_path == "TradingEngine":
            rec["note"] = "TradingEngine 类构造器 patch（patch 整个类返 mock 实例）·engine 自身·不迁"
        else:
            rec["note"] = f"TradingEngine 类成员 patch（{target_path}·engine 自身类）·不迁"
        return rec

    # 拆 target_path → 主体符号 + 可能的子属性
    parts = target_path.split(".")
    head = parts[0]
    is_dotted = len(parts) > 1

    # ---- B. 共享模块对象·属性型（trading.engine.<mod>.<attr>）----
    if is_dotted and head in SHARED_MODULE_OBJECTS:
        rec["category"] = CAT_SHARED_ATTR
        rec["suggestion"] = ""  # 无需迁
        rec["note"] = (
            f"共享模块对象属性型（engine.{head} IS trading.{head} IS phases.{head}）·"
            f"patch 在共享对象上设 attr · 天然全局命中 · 无需迁"
        )
        return rec

    # ---- C3a. engine re-export 且 engine 内部经模块全局名读（_alert_critical/_mode/
    #         _state_store 等）—— 与 ENGINE_SELF_DEFINED 同构（C3 三选）----
    # 必须在 D（共享整体替换）/ C1/C2（SYMBOL_TO_PHASES）之前判定，否则被吃掉。
    # 注意：_state_store.X 属性型已在上面 B 分支返回；此处仅处理裸符号整体替换。
    if head in ENGINE_REEXPORT_INTERNAL_READ:
        rec["category"] = CAT_ENGINE_VS_PHASES
        candidates = SYMBOL_TO_PHASES.get(head, [])
        rec["suggestion"] = (
            f"engine 内部路径（engine 全局名读 · 如 _halt alert=/_submit dry_run/_state_store.upsert）"
            f"→ 保 trading.engine.{head};  "
            f"phases 路径 → 迁 phases 调用方（候选 {candidates}）;  "
            f"或双口子同 patch（Task 7 fix aeb02036 实证：engine + phases 双 patch）"
        )
        rec["note"] = (
            f"engine re-export 且 engine 内部经模块全局名读（{head}）·"
            f"盲迁 phases 会破 engine 路径测试 · 需人工判断 engine vs phases"
        )
        return rec

    # ---- D. 共享模块对象·整体替换（setattr(engine, "<mod>", mock) 无属性访问器）----
    if not is_dotted and head in SHARED_MODULE_OBJECTS:
        # 注意：trading_plan/calendar 等整体替换 → 仅命中 engine 内部
        rec["category"] = CAT_SHARED_WHOLE
        rec["suggestion"] = (
            f"若测 engine 内部路径：保 trading.engine.{head}；"
            f"若测 phases 路径：迁 phases 调用方模块（候选见 SYMBOL_TO_PHASES[{head!r}]="
            f"{SYMBOL_TO_PHASES.get(head, [])}）"
        )
        rec["note"] = "共享模块对象整体替换·仅命中 engine 内部·phases 本地绑定不受影响"
        return rec

    # ---- C3. engine 自身定义（_submit/get_gateway/_resolve_account_id）----
    if head in ENGINE_SELF_DEFINED:
        rec["category"] = CAT_ENGINE_VS_PHASES
        candidates = SYMBOL_TO_PHASES.get(head, [])
        rec["suggestion"] = (
            f"engine 内部路径（_health_guard/bootstrap）→ 保 trading.engine.{head};  "
            f"phases 路径 → 迁 phases 调用方（候选 {candidates}）;  "
            f"或双口子同 patch（Task 7 fix aeb02036 实证：_pre_open_mod + engine 双 patch）"
        )
        rec["note"] = "engine 自身有定义（L350/367/378）·phases 亦有本地绑定·需人工判断"
        return rec

    # ---- C1/C2. phases 本地绑定型（在 SYMBOL_TO_PHASES 表）----
    if head in SYMBOL_TO_PHASES:
        candidates = SYMBOL_TO_PHASES[head]
        if len(candidates) == 1:
            mod = candidates[0]
            rec["category"] = CAT_AUTO_SINGLE
            rec["suggestion"] = f"trading.{mod}.{head}"
            rec["note"] = f"phases 单一调用方（{mod} 顶部 from…import {head}）"
        elif len(candidates) > 1:
            rec["category"] = CAT_AUTO_MULTI
            cand_str = " / ".join(f"trading.{m}.{head}" for m in candidates)
            rec["suggestion"] = f"多目标候选：{cand_str}"
            rec["note"] = f"phases 多调用方（{candidates}）·须据 test 语义选"
        else:
            # SYMBOL_TO_PHASES[head] == []（engine re-export 专有，如 _pre_open_impl）
            rec["category"] = CAT_ENGINE_INTERNAL
            rec["suggestion"] = ""
            rec["note"] = "engine re-export 专有·phases 内同模块直调（无跨模块 import）·不迁"
        return rec

    # ---- C4. engine 内部 wrapper（re-export 或自建，不在 phases 表）----
    if head in ENGINE_INTERNAL_WRAPPERS:
        rec["category"] = CAT_ENGINE_INTERNAL
        rec["suggestion"] = ""  # 不迁
        rec["note"] = (
            f"engine 内部 wrapper/re-export 符号（engine._eod/_pre_open/_stoploss/_post_close "
            f"wrapper 经模块全局名解析命中）·不迁"
        )
        return rec

    # ---- A'. TradingEngine 实例属性（setattr(eng, "X")，eng=TradingEngine()）----
    if head in TRADING_ENGINE_INSTANCE_ATTRS:
        rec["category"] = CAT_CLASS_STAY
        rec["suggestion"] = ""  # 不迁
        rec["note"] = (
            f"TradingEngine 实例属性/方法（eng = TradingEngine() 实例变量）·"
            f"setattr(eng, {head!r}) 改实例属性·不迁"
        )
        return rec

    # ---- E. 未知符号 ----
    rec["category"] = CAT_UNKNOWN
    rec["suggestion"] = "（需人工判断）"
    rec["note"] = "未识别符号·不在 SYMBOL_TO_PHASES / ENGINE_INTERNAL_WRAPPERS / SHARED_MODULE_OBJECTS"
    return rec


# ============================================================================
# 报告生成
# ============================================================================
def build_report(records: List[Dict], scope_desc: str) -> Tuple[str, Dict]:
    """构建文本报告 + 统计 dict。"""
    # 统计
    by_cat = defaultdict(int)
    by_symbol = defaultdict(int)
    by_file = defaultdict(int)
    for r in records:
        by_cat[r["category"]] += 1
        by_symbol[r["target"].split(".")[0]] += 1
        by_file[r["file"]] += 1

    stats = {
        "total": len(records),
        "by_category": dict(by_cat),
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda kv: -kv[1])),
        "by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])),
        "scope": scope_desc,
    }

    lines = []
    lines.append("# W1-A/T2 Task 8 · patch 全量映射校验报告")
    lines.append("")
    lines.append(f"**扫描范围：** {scope_desc}")
    lines.append(f"**总 patch 数：** {len(records)}")
    lines.append("")

    # ---- 分类汇总 ----
    lines.append("## 1. 分类汇总")
    lines.append("")
    lines.append("| 类别 | 数量 | 说明 |")
    lines.append("|---|---|---|")
    cat_desc = OrderedDict([
        (CAT_AUTO_SINGLE,    "C1 自动·单目标（可直接迁）"),
        (CAT_AUTO_MULTI,     "C2 需人工判断 phases 调用方"),
        (CAT_ENGINE_VS_PHASES, "C3 需人工判断 engine vs phases"),
        (CAT_ENGINE_INTERNAL,  "C4 engine 内部 wrapper·不迁"),
        (CAT_SHARED_ATTR,    "B 共享对象·属性型·无需迁"),
        (CAT_SHARED_WHOLE,   "D 共享对象·整体替换·需判断"),
        (CAT_CLASS_STAY,     "A TradingEngine 类·不迁"),
        (CAT_UNKNOWN,        "E 未知符号·需判断"),
    ])
    for cat, desc in cat_desc.items():
        lines.append(f"| `{cat}` | {by_cat.get(cat, 0)} | {desc} |")
    lines.append("")

    # ---- 符号分布 ----
    lines.append("## 2. 符号分布（top 20）")
    lines.append("")
    lines.append("| 符号 | patch 数 |")
    lines.append("|---|---|")
    for sym, cnt in list(stats["by_symbol"].items())[:20]:
        lines.append(f"| `{sym}` | {cnt} |")
    lines.append("")

    # ---- 文件分布 ----
    lines.append("## 3. 文件分布（patch 数 ≥ 3）")
    lines.append("")
    lines.append("| 文件 | patch 数 |")
    lines.append("|---|---|")
    for f, cnt in stats["by_file"].items():
        if cnt >= 3:
            lines.append(f"| `{f}` | {cnt} |")
    lines.append("")

    # ---- 需人工判断明细（C2/C3/D/E）----
    needs_review = [r for r in records if r["category"] in
                    (CAT_AUTO_MULTI, CAT_ENGINE_VS_PHASES, CAT_SHARED_WHOLE, CAT_UNKNOWN)]
    lines.append(f"## 4. 需人工判断明细（{len(needs_review)} 条）")
    lines.append("")
    if needs_review:
        lines.append("| 文件:行 | 形式 | 目标 | 类别 | 建议 |")
        lines.append("|---|---|---|---|---|")
        # 按文件聚合，控制行数（前 80 条）
        for r in needs_review[:80]:
            loc = f"{r['file']}:{r['line']}"
            sug = (r.get("suggestion") or "").replace("|", "\\|")
            lines.append(f"| {loc} | `{r['form']}` | `{r['target']}` | `{r['category']}` | {sug} |")
        if len(needs_review) > 80:
            lines.append(f"| ... | （余 {len(needs_review)-80} 条见 JSON 输出） | | | |")
    else:
        lines.append("（无）")
    lines.append("")

    # ---- 可自动迁移明细（C1 抽样）----
    auto_single = [r for r in records if r["category"] == CAT_AUTO_SINGLE]
    lines.append(f"## 5. 可自动迁移抽样（C1 共 {len(auto_single)} 条，前 15 条示例）")
    lines.append("")
    if auto_single:
        lines.append("| 文件:行 | 目标 | 建议 |")
        lines.append("|---|---|---|")
        for r in auto_single[:15]:
            loc = f"{r['file']}:{r['line']}"
            lines.append(f"| {loc} | `{r['target']}` | `{r['suggestion']}` |")
    else:
        lines.append("（无）")
    lines.append("")

    return "\n".join(lines), stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="W1-A/T2 Task 8 · patch 全量映射校验（Task 9-19 迁 patch 前置）",
    )
    parser.add_argument(
        "--scope", choices=["trading", "all"], default="trading",
        help="扫描范围：trading=tests/trading/（默认），all=tests/ 全量",
    )
    parser.add_argument("--file", help="只扫指定文件（绝对或相对路径）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器可读）")
    parser.add_argument("--out", help="同时写报告到指定文件")
    args = parser.parse_args(argv)

    # 定位 repo root（脚本所在路径上推：tests/trading/_patch_audit.py → repo root）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
    tests_root = os.path.join(repo_root, "tests")

    # 本脚本文件名（自扫会产生 docstring/regex 字面量假阳性，须排除）
    SELF_NAME = "_patch_audit.py"

    # 收集目标文件
    if args.file:
        target = args.file if os.path.isabs(args.file) else os.path.join(repo_root, args.file)
        files = [target] if os.path.isfile(target) else []
        scope_desc = f"单文件：{args.file}"
    elif args.scope == "all":
        files = []
        for dirpath, _dirs, fnames in os.walk(tests_root):
            for fn in fnames:
                if fn.endswith(".py") and fn != SELF_NAME:
                    files.append(os.path.join(dirpath, fn))
        scope_desc = "tests/ 全量"
    else:  # trading
        trad_dir = os.path.join(tests_root, "trading")
        files = []
        if os.path.isdir(trad_dir):
            for dirpath, _dirs, fnames in os.walk(trad_dir):
                for fn in fnames:
                    if fn.endswith(".py") and fn != SELF_NAME:
                        files.append(os.path.join(dirpath, fn))
        scope_desc = "tests/trading/"

    files.sort()

    # 扫描
    records: List[Dict] = []
    for f in files:
        records.extend(scan_file(f))

    # 输出
    if args.json:
        print(json.dumps({"records": records, "stats_scope": scope_desc},
                         ensure_ascii=False, indent=2))
    else:
        report, _stats = build_report(records, scope_desc)
        print(report)
        if args.out:
            out_path = args.out if os.path.isabs(args.out) else os.path.join(repo_root, args.out)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(report)
            print(f"\n（报告已写：{out_path}）", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())


# ============================================================================
# 维护备忘：SYMBOL_TO_PHASES 重扫命令（phases import 变化时）
# ============================================================================
# python -c "
# import re
# phases = ['trading/phases/pre_open.py','trading/phases/stop_loss.py',
#           'trading/phases/post_close.py','trading/phases/exit.py',
#           'trading/order_state.py']
# binding = {}
# for f in phases:
#     short = f.replace('trading/','').replace('.py','').replace('/','.')
#     txt = open(f, encoding='utf-8').read()
#     for m in re.finditer(r'^from\s+\S+\s+import\s+(?:\(([^)]+)\)|([^\n]+))', txt, re.M):
#         body = m.group(1) or m.group(2)
#         for piece in re.split(r',', body):
#             mm = re.match(r'\s*([A-Za-z_][\w]*)(?:\s+as\s+([A-Za-z_]\w*))?', piece)
#             if mm:
#                 name, alias = mm.group(1), mm.group(2) or mm.group(1)
#                 binding.setdefault(alias, set()).add(short)
# for k in sorted(binding): print(f'    {k!r}: {sorted(binding[k])},')
# "
