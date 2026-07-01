"""Task 9：Binance Vision 离线挖掘测试 —— 12 列赋名、UTC 索引、404 跳过。

测试隔离红线（不依赖真实 Binance 接口）：
    - parse_klines_csv 单测：构造一段【12 列无表头】的 CSV 字节流，验证赋名、
      close 取值、amount=quote_asset_volume、open_time(ms)→UTC datetime 索引。
      全程内存，零网络。
    - fetch_one 404 单测：mock 一个 raise_for_status 抛 404 的假响应，验证
      【404 → 返回 None】，调用方可安全跳过而不抛异常炸掉整个 gather。
"""
import asyncio
import io
import zipfile

import aiohttp
import pandas as pd


def test_parse_klines_csv_assigns_columns():
    """12 列无表头 CSV → 标准 6 列 + open_time(ms)→UTC datetime 索引。

    构造一行（open_time,OHLCV,close_time,quote_vol,trades,...），断言：
      - 标准 6 列顺序固定为 open/high/low/close/volume/amount；
      - close 取值正确（1.5，第 4 列）；
      - amount 映射到【第 8 列 quote_asset_volume=150】（成交额，非成交量的 base 数量），
        这是宏观 CTA 量价归一化的统一口径（不同币种价格量级差万倍，唯有
        quote_asset_volume 即 USDT 计价成交额才跨币种可比）；
      - 索引为 UTC datetime（1700000000000ms → 2023-11-14 UTC，时区统一防数据拼接错位）。
    """
    from scripts.sync_binance_vision import parse_klines_csv

    csv = b"1700000000000,1.0,2.0,0.5,1.5,100,1700000060000,150,50,60,90,ignore\n"
    df = parse_klines_csv(csv)
    assert list(df.columns)[:6] == ["open", "high", "low", "close", "volume", "amount"]
    assert df["close"].iloc[0] == 1.5
    assert df["amount"].iloc[0] == 150  # amount=quote_asset_volume（USDT 计价成交额）
    assert str(df.index[0])[:4] == "2023"  # ms→UTC datetime


def test_404_skipped():
    """404 → fetch_one 返回 None，调用方跳过，绝不抛异常炸掉并发 gather。

    某日（如周末/节假日补档缺失）Binance Vision 可能无 daily klines 文件，
    此时静态服务器返 404。若不显式吞掉 404，asyncio.gather 会把整个下载
    任务组连锁炸掉——这是离线挖掘最常见的"偶发缺口"，必须降级为跳过。
    """
    from scripts.sync_binance_vision import fetch_one

    class _Resp:
        # 模拟 raise_for_status 在 404 时抛 ClientResponseError
        def raise_for_status(self):
            raise aiohttp.ClientResponseError(None, None, status=404, message="NF")

        @property
        def status(self):
            return 404

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    res = asyncio.run(fetch_one(_Sess(), "BTCUSDT", "2024-01-02"))
    assert res is None


# ============ I-3: 落盘索引须保时分秒且 tz-naive（与 jqdata 对称） ============


def test_parse_klines_csv_index_keeps_time_and_is_tz_naive():
    """parse_klines_csv 返回的索引须含时分秒（非午夜）且 tz-naive。

    Why（修复背景）：
    - 修复前 parse_klines_csv 返回 DatetimeIndex(tz=UTC)，sync_binance_vision 落盘后
      parquet 索引也是 tz=UTC。lake_reader.load 走【单索引分支】时会调 normalize()，
      该方法对 tz-aware 索引会【截掉时分秒】把同日 1440 根 1m K 线压成同日 →
      加密分钟湖整张表退化失效。
    - 修复方案（与 jqdata _cleanse 的 tz_localize(None) 口径对称）：parse_klines_csv
      在返回前 tz_localize(None) 去掉 tz 标签但【保留时分秒】，落盘后 lake_reader
      走 tz-naive 分支不会调 normalize()，时分秒得以保留。

    断言：
    - 索引第 0 项的 hour/min 不全为 0（即非午夜截断）；
    - 索引 tz 为 None（tz-naive）。
    """
    from scripts.sync_binance_vision import parse_klines_csv

    # open_time=1700000000000ms → 2023-11-14 22:13:20 UTC（非午夜，时分秒显著）
    csv = b"1700000000000,1.0,2.0,0.5,1.5,100,1700000060000,150,50,60,90,ignore\n"
    df = parse_klines_csv(csv)
    ts0 = df.index[0]
    # ★ 时分秒保留：hour/min/sec 不应全为 0（修复前若被 normalize 截断会是 00:00:00）
    assert not (ts0.hour == 0 and ts0.minute == 0 and ts0.second == 0), (
        f"索引时分秒被截断为午夜：{ts0}——lake_reader.normalize() 会把同日 1440 根压成同日"
    )
    # 明确断言具体时分秒（1700000000000ms = 2023-11-14 22:13:20 UTC）
    assert ts0.hour == 22 and ts0.minute == 13, f"时分秒应=22:13:20，实际 {ts0}"
    # ★ tz-naive（修复前是 tz=UTC）
    assert df.index.tz is None, f"索引应为 tz-naive，实际 tz={df.index.tz}"
