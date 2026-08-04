# -*- coding: utf-8 -*-
"""数据快照扩容字段探测（Plan Task 11 dry-run）。

物理意图：全量回填前用最小配额（每接口 1-2 请求）验证新数据集字段真实性，
防幻觉列（注册表写了但 API 不返 → 落湖全 NaN）。沿用项目 probe_tushare_fields 习惯。
每接口独立 try-except，输出真实列名/行数/错误，单接口失败不影响其他。
"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from data._tushare_compat import get_pro

pro = get_pro()


def _probe(name: str, fn, label: str = ""):
    """探测一个接口：打印 [OK rows cols] 或 [ERR]。"""
    try:
        df = fn()
        if df is None or df.empty:
            print(f"[{name}] EMPTY（参数可能无效/无数据/积分不足）{label}")
            return None
        print(f"[{name}] OK rows={len(df)} cols={list(df.columns)}{label}")
        return df
    except Exception as e:
        msg = str(e)[:160]
        print(f"[{name}] ERR {type(e).__name__}: {msg}{label}")
        return None


print("=" * 70)
print("数据快照扩容字段探测（每接口最小请求，验证字段真实性）")
print("=" * 70)

# 1. stock_basic（标的池源头，已知可用，验证落湖字段）
_probe("stock_basic", lambda: pro.stock_basic(list_status="L", fields="ts_code,symbol,name,area,industry,market,list_date"))

# 2. hs_const 沪/深股通成分
_probe("hs_const_sh", lambda: pro.hs_const(hs_type="SH"))
_probe("hs_const_sz", lambda: pro.hs_const(hs_type="SZ"))

# 3. concept（概念字典，恢复同步后验证）
concept_df = _probe("concept", lambda: pro.concept())

# 4. concept_detail（按概念 id 分页，先从 concept 拿首个 id）
if concept_df is not None and not concept_df.empty:
    first_id = str(concept_df.iloc[0]["code"]) if "code" in concept_df.columns else str(concept_df.iloc[0, 0])
    _probe("concept_detail", lambda: pro.concept_detail(id=first_id), f"（id={first_id}）")
else:
    print("[concept_detail] SKIP（concept 拉取失败，无 id 可测）")

# 5. daily_basic（每日基本面因子，by=date 单日全市场）
_probe("daily_basic", lambda: pro.daily_basic(trade_date="20260106", limit=3), "（trade_date=20260106，若空试更早）")

# 6. stk_factor_pro（技术因子，by=date）
_probe("stk_factor_pro", lambda: pro.stk_factor_pro(trade_date="20260106", limit=3), "（trade_date=20260106）")

# 7. cyq_chips（逐价位筹码分布，by=date）
_probe("cyq_chips", lambda: pro.cyq_chips(trade_date="20260106", limit=3), "（trade_date=20260106）")

# 8. cyq_perf（已有，验证对比）
_probe("cyq_perf", lambda: pro.cyq_perf(ts_code="000001.SZ", limit=2), "（ts_code=000001.SZ）")

# 9. weekly / monthly（OHLCV 前复权源数据，by=symbol 拉单标的区间）
_probe("weekly", lambda: pro.weekly(ts_code="000001.SZ", start_date="20260101", end_date="20260131", limit=3))
_probe("monthly", lambda: pro.monthly(ts_code="000001.SZ", start_date="20260101", end_date="20260131", limit=3))

# 10. daily + adj_factor（前复权管道源，验证字段）
_probe("daily", lambda: pro.daily(ts_code="000001.SZ", start_date="20260106", end_date="20260110", limit=3))
_probe("adj_factor", lambda: pro.adj_factor(ts_code="000001.SZ", start_date="20260106", end_date="20260110", limit=3))

print("=" * 70)
print("探测完成。对照 config/registry.py 的 fields 串，订正幻觉列。")
