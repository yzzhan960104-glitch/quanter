# -*- coding: utf-8 -*-
"""基准指数管道（首页收益率对比 · 2026-09-01 用户需求④：主图与上证/纳指/标普同图）。

三基准：
  上证综指 000001.SH —— Tushare index_daily（新鲜；失败降级湖 index_daily，滞后一日）
  纳斯达克综合 IXIC / 标普500 SPX —— Tushare index_global（实测代码表无 .INX 后缀）

输出 public/data/benchmarks.json：era 起的 A 股交易日轴 + 三基准前向填充收盘对齐
（美股交易日 ≠ A 股日：A 股开市美股休市日沿用前收——对比图语义=同期累计收益，
前向填充是标准口径）。原始拉取缓存 logs/benchmarks_cache.json，API 抖动降级缓存。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT = ROOT / "presentation" / "web" / "public" / "data" / "benchmarks.json"
CACHE = ROOT / "logs" / "benchmarks_cache.json"
ERA = "2026-08-31"                     # 与 nav_history.ERA_START 同源（双 20 万纪元）

NAMES = {"000001.SH": "上证综指", "IXIC": "纳斯达克", "SPX": "标普500"}


def _tushare():
    import os
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    import tushare as ts
    return ts.pro_api(os.getenv("TUSHARE_TOKEN", ""))


def _fetch(pro, ts_code: str, start: str, end: str) -> dict[str, float]:
    """单指数分段拉取 → {date: close}。5 年一段合并（Tushare 单次行数上限防御）；
    index_global（美股）与 index_daily（A 股）按代码带不带 '.' 双形态。"""
    out: dict[str, float] = {}
    cur = datetime.strptime(start, "%Y%m%d")
    stop = datetime.strptime(end, "%Y%m%d")
    while cur < stop:
        seg_end = min(cur + timedelta(days=365 * 5), stop)
        try:
            if "." in ts_code:
                df = pro.index_daily(ts_code=ts_code,
                                     start_date=f"{cur:%Y%m%d}",
                                     end_date=f"{seg_end:%Y%m%d}")
            else:
                df = pro.index_global(ts_code=ts_code,
                                      start_date=f"{cur:%Y%m%d}",
                                      end_date=f"{seg_end:%Y%m%d}")
            if df is not None and not df.empty:
                out.update(dict(zip(df["trade_date"].astype(str),
                                    df["close"].astype(float))))
        except Exception:
            pass                                   # 单段失败继续（部分历史优于全空）
        cur = seg_end + timedelta(days=1)
    return out


def _lake_sh_series(start: str, end: str) -> dict[str, float]:
    """湖降级：index_daily.parquet 的上证（滞后一日但永不失联）。"""
    import pandas as pd
    df = pd.read_parquet(ROOT / "data_lake" / "index_daily.parquet")
    try:
        s = df.xs("000001.SH", level="symbol")["close"].sort_index()
    except KeyError:
        return {}
    s = s.loc[start: f"{end} 23:59"]
    return {f"{d:%Y-%m-%d}": float(v) for d, v in s.items()}


def _norm(d: str) -> str:
    """trade_date 归一 ISO：Tushare 紧凑 20260831 → 2026-08-31（湖已带破折号直通）。"""
    s = str(d).replace("-", "")
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 else s


def update() -> dict:
    # 2026-09-01 用户需求④：全量 2010→今（图可缩放全景）；展示层默认年初至今窗口
    # （归一锚=当年首个交易日，前端 NavCurve 消费）。
    start = "20100101"
    end = f"{datetime.now():%Y%m%d}"

    cache: dict = {}
    if CACHE.exists():
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cache = {}
    try:
        pro = _tushare()
        for code in ("000001.SH", "IXIC", "SPX"):
            fresh = _fetch(pro, code, start, end)
            if fresh:
                cache[code] = fresh
        # 交易日历（含未来法定节假日精确口径——09-02 超期日推算精度依赖：
        # 周末外推会误把国庆黄金周当交易日，偏早可达一周）
        try:
            cal = pro.trade_cal(exchange="SSE", start_date="20100101",
                                end_date="20271231")
            if cal is not None and not cal.empty:
                cache["TRADE_CAL"] = sorted(
                    r["cal_date"] for _, r in cal.iterrows() if r["is_open"] == 1)
        except Exception:
            pass                                   # 日历失败降级周末外推
    except Exception as e:
        print(f"  ⚠ Tushare 不可用，走缓存/湖降级：{type(e).__name__}")

    # 统一 ISO 键（cache 内 Tushare 紧凑键 / 湖破折号键混居 → 全部规整）；
    # TRADE_CAL 是日期列表（非 {date: close} 行典）——直通不归一。
    cache = {c: (rows if c == "TRADE_CAL"
                 else {_norm(d): v for d, v in rows.items()})
             for c, rows in cache.items()}
    sh = cache.get("000001.SH") or {
        _norm(d): v for d, v in _lake_sh_series(
            ERA, f"{datetime.now():%Y-%m-%d}").items()}
    if not sh:
        raise SystemExit("benchmarks: 上证轴不可得（Tushare+湖双降级皆空）")
    axis = sorted(d for d in sh if d >= "2010-01-01")
    # 年初锚（展示层 YTD 归一用；当年首个 A 股交易日）
    year = datetime.now().year
    ytd_anchor = next((d for d in axis if d >= f"{year}-01-01"), axis[0])

    def ff(code: str) -> list[float | None]:
        raw = cache.get(code) or {}
        out: list[float | None] = []
        last: float | None = None
        for d in axis:
            v = raw.get(d)
            if v is not None:
                last = v
            out.append(last)
        return out

    doc = {"era_start": ERA, "ytd_anchor": ytd_anchor, "axis": axis,
           "series": [{"code": c, "name": NAMES[c], "points": ff(c)}
                      for c in ("000001.SH", "IXIC", "SPX")],
           "updated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return doc


if __name__ == "__main__":
    d = update()
    print(f"[benchmarks] 轴 {len(d['axis'])} 天 @ {d['updated_at']}")
    for s in d["series"]:
        first = next((p for p in s["points"] if p is not None), None)
        print(f"  {s['name']}: {sum(1 for p in s['points'] if p is not None)} 点"
              f"（首值 {first}）")
