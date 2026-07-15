# -*- coding: utf-8 -*-
"""真 token 字段事实探测（全量下载前置质量门）。

物理意图（Why）：
- config.TUSHARE_DATASETS 的 fields 是「我们以为 Tushare API 返回的列」，全部源自
  Tushare 文档常识 + mock 测试，未经真 token 验证。CLAUDE.md「事实审查/防幻觉」红线
  要求：调用的 API 参数与返回结构必须绝对准确。
- 本脚本对每个数据集真调一次小样本，把 API 真实返回的 columns 与 config fields
  逐字对比，暴露两类字段幻觉：
    ① 缺字段：config 配了 API 不返回的列（落湖后该列全 NaN，下游前视/统计静默失真）；
    ② 多字段：API 返回了 config 没配的列（信息丢失，但非致命）。
- 这是全量下载前的前置门——避免花积分拉回错字段数据落湖后再返工。

设计（极简 + 自适应）：
- PARAMS 覆盖表给特殊 API 的最小可行参数；未覆盖的按 by 模式 + 数据集前缀推断默认。
- 仅探测 columns 一致性，不落盘、不取大样本（省积分）。
- 三态：OK（字段全匹配）/ FIELD_MISMATCH（缺或多字段）/ CALL_FAIL 或 EMPTY（参数错/限频/无数据）。

用法：python scripts/probe_tushare_fields.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # 触发 .env 加载（python-dotenv）
from config import TUSHARE_DATASETS
from data._tushare_compat import get_pro, source_name

# 固定 token[0] 探测，避免多 token 轮询导致的权限漂移（不同 token 权限不一，
# 轮询会时而调通时而报无权限，结果不可复现）。全量下载前需先确定单 token 的稳定权限边界。
import tnskhdata as _ts
_toks = [t.strip() for t in os.getenv("TNSKHDATA_TOKEN", "").split(",") if t.strip()]
pro = _ts.pro_api(_toks[0]) if _toks else get_pro()
print(f"[probe] 固定 token[0]（共 {len(_toks)} 个 token），source={source_name()}")

# 特殊 API 的最小参数（按 Tushare 文档常识，未覆盖的按 by+前缀推断）。
# 探测取「单标的/单日/单期」最小样本，行数由 API 自然限定，省积分。
PARAMS = {
    # 财务类（需 period 报告期，个股 000001.SZ）
    "fina_income": {"ts_code": "000001.SZ", "period": "20231231"},
    "fina_balance": {"ts_code": "000001.SZ", "period": "20231231"},
    "fina_cashflow": {"ts_code": "000001.SZ", "period": "20231231"},
    "forecast": {"ts_code": "000001.SZ", "period": "20231231"},
    "express": {"ts_code": "000001.SZ", "period": "20231231"},
    "dividend": {"ts_code": "000001.SZ"},
    # 资金流/龙虎榜/融资融券/北向（按日或按标的）
    "moneyflow": {"ts_code": "000001.SZ"},
    "top_list": {"trade_date": "20240105"},
    "top_inst": {"trade_date": "20240105"},
    "margin": {"trade_date": "20240105"},
    "margin_detail": {"trade_date": "20240105"},
    "margin_secs": {"trade_date": "20240105"},
    "hsgt_top10": {"trade_date": "20240105"},
    "moneyflow_hsgt": {"trade_date": "20240105"},
    # 板块/概念/指数
    "concept": {"src": "ts"},
    "ths_daily": {"ts_code": "885538.TI"},
    "index_daily": {"ts_code": "000300.SH", "trade_date": "20240105"},
    "index_weight": {"index_code": "000300.SH", "start_date": "20240101", "end_date": "20240131"},
    "index_member": {"index_code": "000300.SH", "start_date": "20240101", "end_date": "20240131"},
    # 股东/解禁/停牌/筹码
    "top10_holders": {"ts_code": "000001.SZ"},
    "top10_floatholders": {"ts_code": "000001.SZ"},
    "share_float": {"trade_date": "20240105"},
    "suspend_d": {"trade_date": "20240105"},
    "cyq_perf": {"ts_code": "000001.SZ", "trade_date": "20240105"},
    # ETF（标的 510300.SH 沪深300ETF）
    "fund_basic": {"market": "EFT"},
    "fund_daily": {"ts_code": "510300.SH"},
    "fund_nav": {"ts_code": "510300.SH"},
    "fund_portfolio": {"ts_code": "510300.SH", "period": "20231231"},
    "fund_share": {"ts_code": "510300.SH"},
    # 宏观指标（月份/季度，参数名待验）
    "cn_m": {"months": "202401"},
    "cn_cpi": {"months": "202401"},
    "cn_ppi": {"months": "202401"},
    "cn_gdp": {"quarter": "20231Q"},
    "cn_pmi": {"month": "202401"},
    # 银行间/交易所统计（B 类合并后：mkt_daily=daily_info，沪深合一）
    "shibor": {"date": "20240105"},
    "shibor_quote": {"date": "20240105"},
    "mkt_daily": {"trade_date": "20240105"},
}


def probe(key: str, cfg: dict):
    """探测单个数据集：真调 API → 对比 columns vs config fields。"""
    if cfg.get("_unavailable"):
        return ("UNAVAILABLE", cfg["_unavailable"][:90])
    api = cfg["api"]
    expected = [f.strip() for f in cfg["fields"].split(",") if f.strip()]
    params = PARAMS.get(key, {})
    try:
        df = getattr(pro, api)(**params)
    except Exception as e:
        m = str(e)
        # 细分失败原因：权限 vs 方法名错 vs 其他（全量下载可行性判断依赖此分类）
        if "权限" in m or "没有接口" in m:
            return ("NO_PERM", f"无权限: {m[:80]}")
        if "正确的接口名" in m or "No such" in m:
            return ("BAD_METHOD", f"方法名错: {m[:80]}")
        return ("CALL_FAIL", f"{type(e).__name__}: {m[:100]} | 参数 {params}")
    if df is None or len(df) == 0:
        return ("EMPTY", f"参数 {params} 返回空（参数名错或该日/期无数据）")
    returned = list(df.columns)
    missing = [f for f in expected if f not in returned]
    extra = [c for c in returned if c not in expected]
    if not missing and not extra:
        return ("OK", f"{len(df)}行 {len(returned)}列")
    return ("FIELD_MISMATCH", f"缺{missing or '[]'} 多{extra[:8]} | 返回cols: {returned[:14]}")


def main():
    print(f"=== Tushare 字段事实探测（source={source_name()}，{len(TUSHARE_DATASETS)} 数据集）===")
    results = {}
    for key in TUSHARE_DATASETS:
        cfg = TUSHARE_DATASETS[key]
        status, detail = probe(key, cfg)
        results[key] = (status, detail)
        print(f"[{status:13s}] {key:20s} {detail}")

    from collections import Counter
    cnt = Counter(s for s, _ in results.values())
    print(f"\n=== 汇总: {dict(cnt)} ===")
    fails = [k for k, (s, _) in results.items() if s != "OK"]
    if fails:
        print(f"需处理({len(fails)}): {fails}")
    else:
        print("✅ 全部数据集字段与 API 真实返回一致，可放心全量下载。")


if __name__ == "__main__":
    main()
