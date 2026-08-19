"""Task 5：宏观信贷同步脚本 —— 月频宏观 → 日频对齐，前视红线守护。

拷问红线（量化风控极度拷问）：
- 社融/M1M2 是【月频】数据，reindex 到日频后【仅向前 ffill】（用过去值解释现在），
  绝不可 bfill 回填未来月度值——否则会把"未来才知道的月度数据"提前泄漏给历史日，
  构成前视偏差（look-ahead bias），回测看起来完美、实盘直接崩盘。
- DR007 走 Task 4 新鲜度守卫（过期/错列→返空）；本测试 mock client，不依赖真实接口，
  且对【空 DR007】容错（缺了就少一列，不崩整个 fetch_macro_series）。

⚠️ Plan C Task 2 源切换后签名变更：fetch_macro_series 不再接收 client 参数——
    M0/M1/M2 已切到 Tushare cn_m（_fetch_with_guard 内部 get_pro），社融/DR007
    内部自建 AKShareClient()。本测试同步更新为 monkeypatch AKShareClient + cn_m
    fake_pro 风格（与 test_tushare_datasets_macro.py 一致）。
"""
import pandas as pd
import pytest



@pytest.fixture
def fake_pro(monkeypatch):
    """薄壳（原 ~60 行块收口 tests/_tushare_stub.py，2026-08-19 W2）。"""
    from tests._tushare_stub import FakePro, install_fake_pro
    fake = FakePro(data=None)
    install_fake_pro(monkeypatch, fake, module_targets=("data.tools.sync_macro_credit",))
    return fake


def test_align_to_daily_forward_fill_only():
    """月频宏观 → 日频，仅向前 ffill（无未来值回填）。

    构造 1 月值=1.0、2 月值=2.0 的月频序列，reindex 到 1 月日历日后，
    1 月内所有工作日的值必须恒为 1.0（用过去值解释现在）；
    若出现 2.0，说明发生了 bfill 回填未来月度值 → 前视偏差，红线被破。
    """
    from data.tools.sync_macro_credit import align_to_daily

    m = pd.DataFrame({"月份": ["2024-01-01", "2024-02-01"], "x": [1.0, 2.0]})
    m["月份"] = pd.to_datetime(m["月份"])
    daily = align_to_daily(m, date_col="月份", start="2024-01-01", end="2024-01-31")
    # 1 月内所有工作日都应为 1.0（1 月值向前填），绝不应出现 2.0（2 月未来值）
    assert (daily["x"] == 1.0).all()


def test_fetch_macro_series_derives_m1m2_gap(fake_pro, monkeypatch):
    """fetch_macro_series 须合并 cn_m(M1/M2) + 社融/DR007 并衍生 M1M2_gap 剪刀差列。

    M1M2_gap = M1同比 - M2同比，是货币活性剪刀差，正向扩张代表资金活化（M1 增速
    快于 M2），CreditRegime 据此判断宽信用/紧信用状态。

    源切换后（Plan C Task 2）：M1/M2 走 Tushare cn_m（fake_pro mock），社融/DR007
    走 akshare fallback（monkeypatch AKShareClient.fetch_macro_raw）。本测试验证
    双源合并后 M1M2_gap 衍生列存在、且缺 SHIBOR/部分档不崩。
    """
    # Tushare cn_m：M0/M1/M2 同比（brief 字段名假设，待真 token 探测）
    fake_pro.set("cn_m", pd.DataFrame({
        "month": ["202401"],
        "m0_yoy": [8.0], "m1_yoy": [5.0], "m2_yoy": [9.0]}))
    # akshare 社融/DR007 fallback（monkeypatch，避开真实网络）
    import data.clients.akshare_client as akc
    monkeypatch.setattr(akc.AKShareClient, "fetch_macro_raw",
                        lambda self, kind: {
                            "shrzgm": pd.DataFrame({"月份": ["2024-01"],
                                                    "社会融资规模增量": [100]}),
                            "dr007": pd.DataFrame({"日期": ["2024-01-02"], "利率": [2.1]}),
                        }.get(kind, pd.DataFrame()))

    from data.tools.sync_macro_credit import fetch_macro_series
    s = fetch_macro_series("2024-01-01", "2024-01-31")
    assert "M1M2_gap" in s.columns   # 剪刀差衍生列


def test_sync_macro_merges_narrow_window_keeps_history(tmp_path, monkeypatch):
    """窄窗口重采不丢历史：existing 有 [d0..d100]，new 只覆盖 [d50..d60]，
    合并后仍保留 d0..d100（new 同期值更新），不因窄窗口被守卫拒写或抹除历史。

    P1 治理（2026-08-12 W0 收尾）：sync_macro 改合并语义——旧整体覆盖语义下，
    窄窗口增量重采（11 行 << 现有 100 行）接入 safe_overwrite 会触发骤降守卫拒写，
    宏观湖永久断更。合并语义（concat 去重 keep='last'）下行数单调≥现有，不误拒、
    不抹历史。前视红线：合并衔接处仅 ffill（无 bfill）——用过去解释现在，绝不
    回填未来月度值。
    """
    from data.tools.sync_macro_credit import sync_macro

    out = tmp_path / "macro.parquet"
    # 现有湖：100 个工作日，shrzgm + M1M2_gap 列（CreditRegime 不变量所需列）。
    idx_old = pd.bdate_range("2026-01-01", periods=100)
    existing = pd.DataFrame({"shrzgm": range(100), "M1M2_gap": [0.1] * 100}, index=idx_old)
    existing.index.name = "date"
    existing.to_parquet(out)

    # new：窄窗口 [d50..d60]，行数 11 << 100（旧覆盖语义会触发骤降守卫或抹除历史）。
    idx_new = idx_old[50:61]  # 取现有湖 d50..d60 共 11 个工作日为窄窗口
    new_df = pd.DataFrame({"shrzgm": range(1000, 1011), "M1M2_gap": [0.5] * 11}, index=idx_new)
    new_df.index.name = "date"
    monkeypatch.setattr("data.tools.sync_macro_credit.fetch_macro_series", lambda s, e: new_df)
    monkeypatch.setattr("data.tools.sync_macro_credit.LAKE_CONFIG", {"lakes": {"macro": str(out)}})

    sync_macro("2026-03-01", "2026-03-15")  # 窗口参数（fetch 已 mock，不影响 new_df）

    got = pd.read_parquet(out)
    assert len(got) >= 100, "窄窗口合并不应丢历史"
    assert "shrzgm" in got.columns
    # new 同期值已更新（d50..d60 的 shrzgm = 1000+，旧 0..100 已被同期覆盖）。
    seg = got.loc[idx_old[50]:idx_old[60], "shrzgm"]
    assert (seg == pd.Series(range(1000, 1011), index=idx_old[50:61])).all()


def test_sync_macro_complete_interval_merge_equals_overwrite(tmp_path, monkeypatch):
    """完整区间输入下，合并结果与「直接用 new 覆盖」逐字一致（行为等价红线守护）。

    Task 3 Self-Review 主张「完整区间输入下合并与旧覆盖逐字一致」——本测试补直接证据。
    场景：existing 已有湖（N 行），new 为完整区间（⊇ existing 范围，相同列 + 同期值
    更新或一致）。跑 sync_macro 合并后，结果应与「直接用 new 覆盖」（旧整体覆盖语义）
    逐字一致。此为「完整区间下合并 == 旧覆盖」的红线守护——若合并实现误改（如保留
    existing 区间外的历史行、误对同期值取 existing 而非 new、bfill 衔接处等），此测 RED。

    Why 守护：合并语义是为治「窄窗口增量重采触发骤降守卫」引入的（见
    sync_macro 文档串），但语义扩展必须保证「完整区间」这一旧覆盖的常见场景行为不变，
    否则等于静默改了生产语义。逐字等价是最强证据——任何发散即回归。
    """
    from data.tools.sync_macro_credit import sync_macro

    out = tmp_path / "macro.parquet"
    # 现有湖：3 个工作日（2026-01-01..01-05），shrzgm + M1M2_gap 列（CreditRegime 不变量）。
    idx_old = pd.bdate_range("2026-01-01", periods=3)  # 1-1, 1-2, 1-5
    existing = pd.DataFrame(
        {"shrzgm": [1, 2, 3], "M1M2_gap": [0.1, 0.2, 0.3]}, index=idx_old
    )
    existing.index.name = "date"
    existing.to_parquet(out)

    # new：完整区间 ⊇ existing 范围——5 个工作日（2025-12-31..2026-01-06）覆盖 existing
    # 的 3 个工作日，相同列，同期值已被上游修订/更新（区别于 existing 旧值）。
    idx_new = pd.bdate_range("2025-12-31", periods=5)  # 12-31, 1-1, 1-2, 1-5, 1-6
    new_df = pd.DataFrame(
        {"shrzgm": [10, 20, 30, 40, 50], "M1M2_gap": [1.1, 1.2, 1.3, 1.4, 1.5]},
        index=idx_new,
    )
    new_df.index.name = "date"
    monkeypatch.setattr(
        "data.tools.sync_macro_credit.fetch_macro_series", lambda s, e: new_df
    )
    monkeypatch.setattr(
        "data.tools.sync_macro_credit.LAKE_CONFIG", {"lakes": {"macro": str(out)}}
    )

    sync_macro("2025-12-31", "2026-01-06")  # fetch 已 mock，窗口参数不影响 new_df

    got = pd.read_parquet(out)
    # 期望：合并后与「直接用 new 覆盖」逐字一致——完整区间下 new 已覆盖 existing 全部
    # 索引，combined 去重 keep='last' 后等于 new 本身，ffill 无 NaN 可填，结果 == new_df。
    # check_freq=False：parquet 落盘不保留 DatetimeIndex.freq 元数据（got.freq 必为 None），
    # 而 new_df 由 bdate_range 构造带 freq="B"——这是 IO 元数据差异，非语义差异，不参与比对。
    pd.testing.assert_frame_equal(got, new_df, check_freq=False)


def test_sync_macro_merge_seam_no_bfill_no_future_leak(tmp_path, monkeypatch):
    """合并衔接处仅 ffill 无 bfill：未来值不泄漏到历史（前视红线守护）。

    前视偏差（look-ahead bias）红线：社融/M1M2 是【月频】数据，合并衔接处若误用 bfill
    回填，会把「未来才公布的月度值」提前泄漏给历史交易日——回测曲线完美但实盘直接崩盘。
    本测试直接构造「未来已知 / 历史 NaN 缺口」场景验证不泄漏，是 Task 3 Self-Review
    「仅 ffill 无 bfill」主张的直接测试证据（此前仅靠实现审查，无运行时守护）。

    场景：existing 有较晚日期（"未来"）的已知值 100/200/300，new 区间较早期含已知 50.0
    + 后续 NaN 缺口。合并 + ffill 后：
      - 较早期 NaN 必须被「更早的 50.0 ffill」（用过去解释现在）；
      - 绝不可被「较晚的 100.0 bfill」回填（未来值泄漏）。
    若实现误改为 .bfill() 或 .ffill().bfill()，较早期会变成 100.0，此测 RED。
    """
    from data.tools.sync_macro_credit import sync_macro

    out = tmp_path / "macro.parquet"
    nan = float("nan")
    # existing：较晚日期（2026-02-02..02-04，"未来"段）已知 shrzgm = 100/200/300。
    idx_old = pd.bdate_range("2026-02-02", periods=3)  # 2-2, 2-3, 2-4
    existing = pd.DataFrame({"shrzgm": [100.0, 200.0, 300.0]}, index=idx_old)
    existing.index.name = "date"
    existing.to_parquet(out)

    # new：较早期区间（2026-01-01..01-05），首日已知 50.0，后两日 NaN（缺口）。
    idx_new = pd.bdate_range("2026-01-01", periods=3)  # 1-1, 1-2, 1-5
    new_df = pd.DataFrame({"shrzgm": [50.0, nan, nan]}, index=idx_new)
    new_df.index.name = "date"
    monkeypatch.setattr(
        "data.tools.sync_macro_credit.fetch_macro_series", lambda s, e: new_df
    )
    monkeypatch.setattr(
        "data.tools.sync_macro_credit.LAKE_CONFIG", {"lakes": {"macro": str(out)}}
    )

    sync_macro("2026-01-01", "2026-01-05")  # fetch 已 mock，窗口参数不影响 new_df

    got = pd.read_parquet(out)
    # 较早期（1-1..1-5）的 shrzgm 必须全 == 50.0（ffill 自 1-1 已知值，用过去解释现在）。
    # 若实现误用 bfill，1-2/1-5 会被 2-2 的 100.0 回填 → 红线被破、断言失败。
    early = got.loc[idx_new, "shrzgm"]
    assert (early == 50.0).all(), (
        f"前视红线被破：较早期 shrzgm 应被 50.0 ffill，却出现未来值 bfill 回填\n{early}"
    )
    # 同时验证未来段保持原值未被污染（合并不应反向影响 existing 已知值）。
    late = got.loc[idx_old, "shrzgm"]
    assert (late == pd.Series([100.0, 200.0, 300.0], index=idx_old)).all()
