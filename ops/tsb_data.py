# -*- coding: utf-8 -*-
"""TSB 机会观察数据管道（2026-09-02：紫金×纽约金、美元指数×US10Y×纽约金）。

四源合一（缓存 logs/tsb_cache.json；单源失败降级缓存，不炸整页）：
  纽约金 COMEX 主力 —— 新浪全球期货 GC（~10 年日 K；东财 push2his 同源数据
                       实测会被反爬断连，新浪稳）
  美元指数 DXY      —— Tushare fx_daily 六成分货币对自算（官方加权：
                       50.14348112 × EUR^-0.576 × JPY^0.136 × GBP^-0.119 ×
                       CAD^0.091 × SEK^0.042 × CHF^0.036；实测与东财 99.71 差
                       0.04 = 汇差级。直接源东财拉黑/新浪腾讯无此代码后的硬解）
  美债 10Y          —— Tushare us_tycr 收益率曲线 y10 列（2018 起）
  紫金矿业 601899.SH —— 湖 a_shares_daily（2008 起全量，前复权）

产物 public/data/tsb.json：四序列各自 {dates, points}（互不对齐——前端按窗口
锚归一时各自取「≤窗口首日的最近一根」为锚，缺数段 null 断点）。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "presentation" / "web" / "public" / "data" / "tsb.json"
CACHE = ROOT / "logs" / "tsb_cache.json"

# DXY 官方权重（1973 基期，ICE 公式）
_DXY_W = {"EURUSD.FXCM": -0.576, "USDJPY.FXCM": 0.136, "GBPUSD.FXCM": -0.119,
          "USDCAD.FXCM": 0.091, "USDSEK.FXCM": 0.042, "USDCHF.FXCM": 0.036}
_DXY_BASE = 50.14348112


def _norm(d) -> str:
    s = str(d).replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _sina_gc() -> dict[str, float]:
    """新浪全球期货 COMEX 黄金主力日 K → {iso: close}。"""
    import urllib.request
    u = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20_t=/"
         "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=GC")
    req = urllib.request.Request(u, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
    with urllib.request.urlopen(req, timeout=25) as r:
        body = r.read().decode("gbk", "replace")
    m = re.search(r"\((\[.*\])\)", body, re.S)
    rows = json.loads(m.group(1)) if m else []
    if not rows:
        raise RuntimeError("sina GC 空返回")
    return {r["date"]: float(r["close"]) for r in rows if r.get("close")}


def _dxy_from_pairs() -> dict[str, float]:
    """六成分对子 → DXY（公共交易日交集上逐日计算）。"""
    import os
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import tushare as ts
    pro = ts.pro_api(os.getenv("TUSHARE_TOKEN", ""))
    series: dict[str, dict[str, float]] = {}
    for tc in _DXY_W:
        df = pro.fx_daily(ts_code=tc)
        if df is None or df.empty:
            raise RuntimeError(f"fx_daily {tc} 空返回")
        series[tc] = {_norm(r["trade_date"]): float(r["bid_close"])
                      for _, r in df.iterrows()}
    common = set.intersection(*(set(s) for s in series.values()))
    out = {}
    for d in common:
        v = _DXY_BASE
        for tc, w in _DXY_W.items():
            v *= series[tc][d] ** w
        out[d] = round(v, 3)
    if not out:
        raise RuntimeError("DXY 公共交易日交集为空")
    return out


def _us10y() -> dict[str, float]:
    import os
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import tushare as ts
    pro = ts.pro_api(os.getenv("TUSHARE_TOKEN", ""))
    df = pro.us_tycr()
    out = {}
    for _, r in df.iterrows():
        if r.get("y10") is not None:
            out[_norm(r["date"])] = float(r["y10"])
    if not out:
        raise RuntimeError("us_tycr y10 空返回")
    return out


def _zijin() -> dict[str, float]:
    import pandas as pd
    df = pd.read_parquet(ROOT / "data_lake" / "a_shares_daily.parquet")
    sub = df.xs("601899.SH", level="symbol")["close"].sort_index()
    return {f"{d:%Y-%m-%d}": float(v) for d, v in sub.items()}


def update() -> dict:
    cache: dict = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}

    for key, fn in {"gold": _sina_gc, "dxy": _dxy_from_pairs,
                    "us10y": _us10y, "zijin": _zijin}.items():
        try:
            fresh = fn()
            if fresh:
                cache[key] = fresh
        except Exception as e:
            print(f"  ⚠ tsb.{key} 拉取失败走缓存：{type(e).__name__}: {e}")

    def ser(key: str):
        m = cache.get(key) or {}
        dates = sorted(m)
        return {"dates": dates, "points": [m[d] for d in dates]}

    doc = {
        "series": {"zijin": ser("zijin"), "gold": ser("gold"),
                   "dxy": ser("dxy"), "us10y": ser("us10y")},
        "meta": {
            "gold": "纽约金 = COMEX 黄金主力连续（新浪 GC）",
            "dxy": "美元指数（六成分对子按 ICE 官方权重自算，汇差级精度）",
            "us10y": "美债 10Y 到期收益率 %（Tushare us_tycr）",
            "zijin": "紫金矿业 601899.SH（数据湖，前复权）",
        },
        "updated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return doc


if __name__ == "__main__":
    d = update()
    for k, s in d["series"].items():
        tail = f"末值 {s['points'][-1]}" if s["points"] else "空"
        print(f"  {k}: {len(s['dates'])} 根（{s['dates'][0] if s['dates'] else '—'} → "
              f"{s['dates'][-1] if s['dates'] else '—'}，{tail}）")
