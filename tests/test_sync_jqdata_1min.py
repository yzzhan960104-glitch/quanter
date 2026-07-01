"""Task 8：JQData 分钟数据同步 —— 断点续传 + 配额耗尽优雅停（分钟层落 shard）。

物理意图（Why）：
    宏观 CTA 四级数据湖的【分钟层】：对 Task 6 选出的 ≤50 只活跃股，拉近 3 月
    1m/5m 分钟 K。聚宽试用账号【单连接 + 日 100 万条配额】，分钟数据量极大
    （50 只 × 90 天 × 240 根/日 × 5m=6 根/日 ≈ 数十万~百万级条数），极易一日拉不完。
    故必须支持：
        1. 断点续传：每标的落独立 shard `{symbol}_{freq}.parquet`，已存在即跳过，
           明日重跑时自动从断点续拉（已拉的不再重拉，省配额）；
        2. 优雅停：单只拉取触发 QuotaExceeded 时，【停止后续标的】（绝不再发请求
           越界扣费/封号），打印"明日重跑续传"，不崩；
        3. 合并：全量成功后把 shards 合并为 MultiIndex(date,symbol) → a_shares_1min.parquet。

测试策略（FakeClient）：
    用 FakeClient 在第 N 只（默认 fail_at）抛 QuotaExceeded，验证：
        - 第 1 只成功落 shard（A_5m.parquet 存在）；
        - 第 2 只触发 QuotaExceeded → 优雅停，第 3 只 C 不再拉（C_5m.parquet 不存在）。
    build_multiindex 被 mock 掉（避免依赖真实 shard 合并副作用，专注断点续传/优雅停语义）。
"""
import os

import pandas as pd


class _FakeClient:
    """mock JQDataClient：计数到 fail_at 时抛 QuotaExceeded（模拟配额临限）。

    Why n>=fail_at：聚宽配额耗尽是【运行期】事件（拉到第 N 只时才触红线），故用
    计数器在 fetch_minute_bars 内动态抛出，贴近真实时序（前 N-1 只成功，第 N 只抛）。
    """

    def __init__(self, fail_at: int = 99) -> None:
        self.n = 0
        self.fail_at = fail_at

    def fetch_minute_bars(self, s, a, b, frequency="5m"):
        self.n += 1
        if self.n >= self.fail_at:
            # 触临限即抛：让上层 sync 优雅停，绝不越界继续拉
            from data.clients.jqdata_client import QuotaExceeded
            raise QuotaExceeded("limit")
        # 返回 1 行有效分钟 K（schema 对齐 _cleanse：open/high/low/close/volume/amount）
        return pd.DataFrame(
            {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1], "amount": [1]},
            index=pd.to_datetime(["2024-01-02"]),
        )


def test_sync_resumable_and_graceful_stop(tmp_path, monkeypatch):
    """断点续传 + 优雅停：A 成功落 shard；B 触 QuotaExceeded → 停，C 不再拉。

    断言三件套：
        1. A_5m.parquet 存在 —— 第 1 只成功落 shard（断点续传的"已拉即跳过"前提）；
        2. C_5m.parquet 不存在 —— 第 2 只触临限后优雅停，第 3 只绝不再拉（不越界）；
        3. 不抛异常（优雅停不崩，仅打印）。
    """
    from scripts.sync_jqdata_1min import sync_jqdata_1min

    # FakeClient 在第 2 只抛 QuotaExceeded → A 成功、B 抛、C 不拉
    monkeypatch.setattr(
        "scripts.sync_jqdata_1min.JQDataClient.get_instance",
        lambda: _FakeClient(fail_at=2),
    )
    # build_multiindex 被 mock：优雅停分支本就不合并，但即便走合并也隔离真实 IO
    monkeypatch.setattr("scripts.sync_jqdata_1min.build_multiindex", lambda d, o: None)

    shard_dir = str(tmp_path / "shards")
    out = str(tmp_path / "m.parquet")

    # 优雅停：函数应正常返回（不抛 QuotaExceeded 到调用方）
    sync_jqdata_1min(["A", "B", "C"], months=3, freq="5m", shard_dir=shard_dir, out=out)

    done = [f for f in os.listdir(shard_dir) if f.endswith(".parquet")]
    assert "A_5m.parquet" in done      # 第 1 只成功落 shard
    # 第 2 只触发 QuotaExceeded → 优雅停，C 不再拉
    assert "C_5m.parquet" not in done


def test_sync_resumable_skip_existing(tmp_path, monkeypatch):
    """断点续传：已存在的 shard 直接跳过（不重复拉，省配额）。

    物理意图：聚宽按条计费，重跑必须从断点续传——已拉的 shard 不再发请求。
    预置 A_5m.parquet，FakeClient 计数应【不增加】（A 被跳过未调用 fetch）。
    """
    from scripts.sync_jqdata_1min import sync_jqdata_1min

    shard_dir = str(tmp_path / "shards")
    out = str(tmp_path / "m.parquet")
    os.makedirs(shard_dir, exist_ok=True)
    # 预置 A 的 shard（模拟上次已拉成功）
    pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1], "amount": [1]},
        index=pd.to_datetime(["2024-01-02"]),
    ).to_parquet(os.path.join(shard_dir, "A_5m.parquet"))

    fake = _FakeClient(fail_at=99)  # 不会触临限
    monkeypatch.setattr(
        "scripts.sync_jqdata_1min.JQDataClient.get_instance", lambda: fake
    )
    monkeypatch.setattr("scripts.sync_jqdata_1min.build_multiindex", lambda d, o: None)

    sync_jqdata_1min(["A", "B"], months=3, freq="5m", shard_dir=shard_dir, out=out)

    # A 已存在 → 跳过，FakeClient 只被调 1 次（拉 B）；A 的 shard 不被覆盖
    assert fake.n == 1
