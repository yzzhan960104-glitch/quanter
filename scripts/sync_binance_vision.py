"""Binance Vision 离线下载：aiohttp 并发拉 1m ZIP → 解压 CSV → 统一列 → 增量 parquet。

数据流（加密分钟沙盒湖，7x24 极端市场测试）：
    Binance Vision 公开静态文件（无鉴权、无风控限频，纯 CDN）：
      https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/{symbol}-1m-{date}.zip
    → aiohttp + Semaphore(8) 并发下载过去 N 天（避开 API 限频，纯静态走 CDN 带宽）
    → stdlib zipfile 内存解压（不落临时文件，避免磁盘 IO 与清理遗漏）
    → 12 列无表头 CSV 赋标准名 + open_time(ms)→UTC datetime 索引
    → 合并 MultiIndex(date, symbol) → 增量 data_lake/crypto_btc_1m.parquet

为何用 Binance Vision 作"可选加密沙盒"（而非直接对接交易 API）：
    加密是 7x24、无涨跌停、流动性极端（牛市日波 ±30%、崩盘时秒级 -50%）的市场，
    是宏观 CTA 信号、止损逻辑、敞口管理的【极限压力测试场】。Vision 静态文件
    无鉴权/无限频，可作为离线沙盒反复回放历史极端行情（如 2022 Luna 崩盘、
    2024 ETF 获批瞬拉），验证策略在"传统市场遇不到的尾部"是否还活着。

容错红线：
    - 404 跳过：某日 daily 文件可能缺失（新币种补档、服务器归档滞后），返 None，
      绝不让 asyncio.gather 连锁炸掉整组下载。
    - 任意 IO/解压异常返 None：网络抖动、ZIP 损坏、CSV 半截，统一降级为跳过。
"""
from __future__ import annotations

import asyncio
import csv as _csv
import datetime as _dt
import io
import os
import zipfile

import aiohttp
import pandas as pd

# Binance Klines CSV 12 列无表头的官方字段顺序（逐字对齐 Binance 文档，绝不可错位）：
#   open_time(ms), open, high, low, close, volume, close_time(ms),
#   quote_asset_volume, number_of_trades, taker_buy_base, taker_buy_quote, ignore
# 其中 quote_asset_volume 才是【USDT 计价的成交额】——跨币种量级统一口径。
_RAW_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_asset_volume", "number_of_trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
# 对外标准 6 列（宏观 CTA 分钟湖统一 schema，与 jq 1m 对齐）：
#   amount=quote_asset_volume（成交额，非成交量 base 数量），见模块 docstring。
_STD_COLS = ["open", "high", "low", "close", "volume", "amount"]


def parse_klines_csv(raw: bytes) -> pd.DataFrame:
    """12 列无表头 CSV 字节流 → 标准 6 列 DataFrame + open_time(ms)→datetime 索引。

    为何 tz-naive（I-3 修复：与 jqdata _cleanse 的 tz_localize(None) 口径对称）：
        Binance 全市场以 UTC 计时（00:00 UTC = 日切）。早期实现把索引标为 tz=UTC
        作为「绝对时间锚点」，但这与下游 lake_reader.load 的【单索引分支】冲突——
        该分支会对 tz-aware 索引调 normalize()，把【时分秒截掉】，导致同日 1440 根
        1m K 线被压成同日，整张加密分钟湖退化失效。
        修复方案：parse 阶段仍用 utc=True 解析（保证跨夏令时/时区拼接不错位），
        但在返回前 tz_localize(None) 去掉 tz 标签【保留时分秒】。落盘后 lake_reader
        走 tz-naive 分支不会 normalize()，时分秒得以保留；与 jqdata _cleanse 的
        tz_localize(None) 完全对称，跨资产合并口径一致。
    为何 amount=quote_asset_volume（而非 volume）：
        volume 是【base 资产数量】（BTC 的个数），不同币种价格量级差万倍
        （BTC 6 万 vs SHIB 0.00001），量级不可比；quote_asset_volume 是
        【USDT 计价的成交额】，跨币种、跨时点都量纲一致，是宏观 CTA 量价
        归一化的唯一合法口径。

    Args:
        raw: ZIP 解压后的 CSV 原始字节流（12 列无表头）。

    Returns:
        标准 6 列 DataFrame（open/high/low/close/volume/amount，数值已 coerce），
        DatetimeIndex(tz-naive, name="date"，保留时分秒)。空输入返空 DataFrame。
    """
    rows = list(_csv.reader(io.StringIO(raw.decode().strip())))
    if not rows:
        return pd.DataFrame()
    # 赋 12 列名（与 Binance 官方字段逐字对齐）；容忍个别行多/少尾列的极端脏数据。
    n_cols = len(_RAW_COLS)
    sample_len = len(rows[0])
    if sample_len > n_cols:
        # 极端脏数据：尾部多余字段 → 用 extra_0/extra_1... 兜底，避免 DataFrame 赋名炸。
        cols = _RAW_COLS + [f"extra_{i}" for i in range(sample_len - n_cols)]
    elif sample_len < n_cols:
        # 字段缺失：截断到实际长度（极少见，通常是下载半截）。
        cols = _RAW_COLS[:sample_len]
    else:
        cols = _RAW_COLS
    df = pd.DataFrame(rows, columns=cols)
    # amount 列先映射 = quote_asset_volume（USDT 计价成交额），再统一转数值。
    if "quote_asset_volume" in df.columns:
        df["amount"] = df["quote_asset_volume"]
    # 数值列强制 to_numeric + coerce：脏字符（空串/None）→ NaN，下游 concat 自动对齐。
    # 注意 amount 必须在【映射之后】转数值，否则引用到字符串列。
    for c in ["open", "high", "low", "close", "volume", "amount", "number_of_trades"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # open_time(ms) → UTC datetime（先用 utc=True 解析防跨时区拼接错位），
    # 再 tz_convert(None) 去 tz 标签但【保留时分秒】（I-3：防 lake_reader.normalize 截掉时分秒）。
    # Why 走 .dt.tz_convert(None)：to_datetime(Series, utc=True) 返回的是 tz-aware
    # Series（不是 DatetimeIndex），其 tz 操作须通过 .dt 访问器；tz_convert(None)
    # 把 UTC 时刻转成 naive 时间戳（保留时分秒），与 jqdata _cleanse 的
    # tz_localize(None) 落盘口径完全对称。
    naive_idx = (
        pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
        .dt.tz_convert(None)
    )
    df.index = naive_idx
    df.index.name = "date"
    return df[_STD_COLS]


async def fetch_one(
    session: aiohttp.ClientSession,
    symbol: str,
    date: str,
    sem: asyncio.Semaphore | None = None,
) -> pd.DataFrame | None:
    """下载单个 (symbol, date) 的 1m klines ZIP → 解压 → 返标准 DataFrame。

    404 / 任意异常 → 返 None（调用方跳过）。为何"返 None 而非抛异常"：
        asyncio.gather 默认 fast-fail，一旦某个 future 抛异常，整组立刻取消。
        但离线挖掘面对的是"偶发缺口"（某日文件缺失/网络抖动），缺一天数据
        不该让 30 天全挂——降级为 None 让调用方过滤即可。

    Args:
        session: aiohttp ClientSession（调用方负责生命周期）。
        symbol: 如 "BTCUSDT"。
        date: "YYYY-MM-DD"。
        sem: 并发限流信号量（避免瞬间打满 CDN 连接）；None 时即时创建单次使用（测试便利）。

    Returns:
        标准 DataFrame 或 None（404/异常）。
    """
    url = (
        f"https://data.binance.vision/data/spot/daily/klines/"
        f"{symbol}/1m/{symbol}-1m-{date}.zip"
    )
    async with (sem or asyncio.Semaphore(1)):
        try:
            async with session.get(url) as resp:
                # 404 优先短路：某日无数据（如新币种补档前），跳过而非当错误。
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                data = await resp.read()
        except aiohttp.ClientResponseError as e:
            # 5xx/其他 HTTP 错误：降级跳过（明日可重试，今天不阻塞 gather）。
            if e.status == 404:
                return None
            return None
        except Exception:
            # 连接重置/超时/DNS 失败等网络层异常：统一跳过。
            return None
    # ZIP 内存解压（不落临时文件，避免磁盘清理遗漏与 Windows 文件锁）。
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = z.namelist()[0]
            raw = z.read(name)
        return parse_klines_csv(raw)
    except Exception:
        # ZIP 损坏 / CSV 半截 / 解码失败：跳过（数据完整性由下游 concat 兜底）。
        return None


async def sync_binance_vision(
    symbol: str = "BTCUSDT",
    days: int = 30,
    out: str = "data_lake/crypto_btc_1m.parquet",
) -> None:
    """并发下载过去 N 天 1m klines → 合并 MultiIndex(date, symbol) → 增量落 parquet。

    Args:
        symbol: 交易对，默认 BTCUSDT。
        days: 回溯天数（含今天，今日文件可能尚未归档 → 自然 404 跳过）。
        out: parquet 输出路径，目录不存在则自动创建。
    """
    sem = asyncio.Semaphore(8)  # 8 并发：Binance Vision 是静态 CDN，8 路够吃满带宽又不被限。
    today = _dt.date.today()
    dates = [(today - _dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[fetch_one(session, symbol, d, sem) for d in dates]
        )
    frames = [df for df in results if df is not None and not df.empty]
    if not frames:
        print("Binance Vision：指定区间无可用数据，已跳过。")
        return
    # 纵向拼接 + 时间排序（多日拼接后边界必须单调，分钟级回测强依赖）。
    big = pd.concat(frames).sort_index()
    # MultiIndex(date, symbol)：与 jq 分钟湖一致的双键，方便 DataLakeReader 跨资产合并。
    big["symbol"] = symbol
    big = big.reset_index().set_index(["date", "symbol"]).sort_index()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    big.to_parquet(out)
    print(f"crypto 分钟湖已写入：{out}，共 {len(big)} 行（{symbol}，{days} 天）")


if __name__ == "__main__":
    # CLI 直跑：默认 BTCUSDT 过去 30 天 → data_lake/crypto_btc_1m.parquet。
    asyncio.run(sync_binance_vision())
