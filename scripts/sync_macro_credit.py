"""宏观信贷同步：AKShare 社融/M1M2/DR007/SHIBOR → 日频对齐 parquet。

前视红线（Look-ahead bias 防护）：
    社融/M1M2 是【月频】数据，每月才公布一次。要把它们 join 到日频策略上，
    必须把月度值"展平"到日频——但展平方向只有【向前 ffill】是合法的：
    即用"已公布的最近一期月度值"解释当下的每一天。
    【绝不可】用 bfill 向后回填——否则会把"未来才公布的月度数据"提前泄漏给
    历史交易日，构成前视偏差，回测曲线完美但实盘直接崩盘（典型未来函数陷阱）。
    本脚本 align_to_daily() 严格 ffill-only，无任何 bfill，是整个宏观 CTA
    四级数据湖（宏观月频→板块日频→微观日线→分钟）顶层的合规基石。

数据流（宏观月频湖）：
    AKShareClient.fetch_macro_raw(kind)
        kind ∈ {shrzgm: 社融增量, money_supply: M0/M1/M2, dr007: 银行间7天回购, shibor: 同业拆放}
    → 月频 reindex 到工作日 + 仅 ffill
    → 衍生 M1M2_gap = M1同比 - M2同比（货币活性剪刀差，宽/紧信用判据）
    → 落 data_lake/macro_credit.parquet（DatetimeIndex，列式 parquet）
"""
from __future__ import annotations

import os
import sys

# 加项目根到 sys.path：脚本可从任意 cwd 直接 `python scripts/xxx.py` 运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from config import LAKE_CONFIG
from data.clients.akshare_client import AKShareClient


def align_to_daily(
    df: pd.DataFrame,
    date_col: str,
    start: str,
    end: str,
    *,
    value_cols: list[str] | None = None,
) -> pd.DataFrame:
    """月频/日频 DataFrame → reindex 到工作日日历 + 仅向前 ffill。

    前视红线核心实现：
        - pd.bdate_range(start, end) 生成工作日日历（周一~周五，对齐 A 股交易日口径）；
        - reindex 后，月度数据仅在"对应那一天"有值，其余工作日为 NaN；
        - out[cols].ffill() 仅【向前填充】（用过去最近一期已知值解释当下），
          NaN 都被过去的月度值填上——这正是"用过去解释现在"的合法语义；
        - 全程【无 bfill】，绝不把未来月度值回填到历史日，杜绝前视偏差。

    参数：
        df:        原始 DataFrame（含日期列与若干指标列）。
        date_col:  日期列名（如 "月份"/"日期"）。
        start/end: 'YYYY-MM-DD' 日历范围。
        value_cols: 仅对这些列 ffill（默认全部列）；显式传入可避免误填非数值列。

    返回：
        DatetimeIndex（工作日）的 DataFrame，值列已向前填充，index.name='date'。
    """
    d = df.copy()
    # 日期列解析为 datetime 并设为索引，sort_index 防御上游乱序（时序运算前提）。
    # 容错：akshare 宏观接口（如 macro_china_shrzgm）的月份列返回 "201501" 形式
    #   （6 位 YYYYMM），pd.to_datetime 默认解析会失败（month out of range）；
    #   mock 测试用 "2024-01" 漏了此格式，实数据触发——先默认解析，失败再试 %Y%m。
    _vals = d[date_col].astype(str).str.strip()
    # 多格式容错：akshare 宏观月份列有三种形态——
    #   "202604"(6位 YYYYMM) / "2008年02月份"(中文) / "2024-01"(标准)。
    # pd.to_datetime 默认对前两种会返 NaT（不抛），须显式多格式回退。
    parsed = pd.to_datetime(_vals, errors="coerce")
    if parsed.isna().mean() > 0.5:  # 多数解析失败 → 试中文 "YYYY年MM月份" → "YYYY-MM"
        cleaned = _vals.str.replace("月份", "", regex=False).str.replace("年", "-", regex=False)
        parsed = pd.to_datetime(cleaned, format="%Y-%m", errors="coerce")
    if parsed.isna().mean() > 0.5:  # 仍失败 → 试 6 位 YYYYMM
        parsed = pd.to_datetime(_vals, format="%Y%m", errors="coerce")
    d[date_col] = parsed
    d = d.dropna(subset=[date_col])  # 丢弃最终仍解析失败的脏行
    d = d.set_index(date_col).sort_index()
    # 工作日日历：对齐 A 股交易日（周末无行情，reindex 进来也无意义，徒增 NaN）。
    cal = pd.bdate_range(start, end)
    out = d.reindex(cal)
    # ⚠️ 仅向前 ffill（用过去值解释现在）；绝无 bfill（不回填未来月度值）。
    cols = value_cols or out.columns.tolist()
    out[cols] = out[cols].ffill()
    out.index.name = "date"
    return out


def fetch_macro_series(client: AKShareClient, start: str, end: str) -> pd.DataFrame:
    """合并社融/M1M2/DR007/SHIBOR → 日频对齐 DataFrame（衍生 M1M2_gap）。

    容错策略（任一档缺失不崩，按列合并）：
        - 社融/货币月频 → align_to_daily 仅 ffill 到日频；
        - DR007 日频直接 reindex（若 Task 4 新鲜度守卫返空，则缺了少一列不崩）；
        - SHIBOR 当前未参与合并（结构复杂，留待后续），返空亦不影响。

    M1M2_gap 衍生：M1同比 - M2同比。
        含义：货币活性剪刀差。M1 增速 > M2 → 资金活化（企业活期存款上行，
        投资活跃）→ 宽信用预期；M1 < M2 → 资金沉淀为定期/储蓄 → 紧信用预期。
        CreditRegime（Task 11）据此判状态机迁移。

    参数：
        client: AKShareClient 实例（或任何具 fetch_macro_raw(kind) 协议的对象）。
        start/end: 'YYYY-MM-DD' 日历范围。
    """
    shrzgm = client.fetch_macro_raw("shrzgm")
    money = client.fetch_macro_raw("money_supply")
    dr007 = client.fetch_macro_raw("dr007")

    series: dict[str, pd.Series] = {}
    # 社融月频 → 日频仅 ffill；列名以 akshare 实测为准，_pick 做日期列防御性 rename。
    if not shrzgm.empty:
        s = align_to_daily(_pick(shrzgm, "月份"), "月份", start, end)
        series["shrzgm"] = s.iloc[:, 0]
    # 货币供应量月频 → 日频仅 ffill；衍生 M1M2_gap 剪刀差。
    if not money.empty:
        m = align_to_daily(_pick(money, "月份"), "月份", start, end)
        # 归一 AKShare 原始列名 → CreditRegime 消费的标准名
        # 实测列名：货币(M1)-同比增长 / 货币和准货币(M2)-同比增长 / 流通中的现金(M0)-同比增长
        _ren = {}
        for c in m.columns:
            if "(M1)" in c and "同比增长" in c:
                _ren[c] = "M1同比增长"
            elif "(M2)" in c and "同比增长" in c:
                _ren[c] = "M2同比增长"
            elif "(M0)" in c and "同比增长" in c:
                _ren[c] = "M0同比增长"
        if _ren:
            m = m.rename(columns=_ren)
        if "M1同比增长" in m and "M2同比增长" in m:
            # 衍生列在 ffill 之后计算：用已对齐的日频同比相减，保证整段时序连续。
            m["M1M2_gap"] = pd.to_numeric(m["M1同比增长"], errors="coerce") - pd.to_numeric(
                m["M2同比增长"], errors="coerce"
            )
        series.update({c: m[c] for c in m.columns})
    # DR007 日频 → 直接 reindex（Task 4 新鲜度守卫已防过期；空则少一列不崩）。
    if not dr007.empty:
        d = align_to_daily(_pick(dr007, "日期"), "日期", start, end)
        series["dr007"] = d.iloc[:, 0]

    if not series:
        return pd.DataFrame()
    # 列合并后再 ffill：弥合各档时间起点不同留下的头部 NaN（仍只向前，无 bfill）。
    df = pd.DataFrame(series).ffill().dropna(how="all")
    return df


def _pick(df: pd.DataFrame, prefer: str) -> pd.DataFrame:
    """容错取日期列（akshare 版本间列名漂移，需防御性归一）。

    背景：akshare 上游接口列名在不同小版本间存在漂移（如 "月份" vs "报告日"
    vs "date"），若硬编码列名会导致 KeyError。本函数：
        - 若 prefer 列已存在 → 原样返回；
        - 否则在列名里搜中文"日期/月份"或英文"date"→ rename 为 prefer 后返回；
        - 都找不到 → 原样返回（让上游 align_to_daily 自然抛错暴露问题，而非静默）。
    """
    if prefer in df.columns:
        return df
    cand = [c for c in df.columns
            if "日期" in str(c) or "月份" in str(c) or "date" in str(c).lower()]
    return df.rename(columns={cand[0]: prefer}) if cand else df


def sync_macro(start: str, end: str, out: str | None = None) -> None:
    """落 data_lake/macro_credit.parquet（DatetimeIndex，列式 parquet）。

    参数：
        start/end: 'YYYY-MM-DD' 同步区间。
        out: 自定义输出路径（默认 LAKE_CONFIG['lakes']['macro']）。
    """
    out = out or LAKE_CONFIG["lakes"]["macro"]
    df = fetch_macro_series(AKShareClient(), start, end)
    if df.empty:
        print("宏观数据为空，跳过")
        return
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out)
    print(f"宏观湖写入：{out}，{len(df)} 行")


if __name__ == "__main__":
    # 默认同步近 2 年（宏观月频数据 2 年约 24 期，足以判 CreditRegime 状态迁移）。
    import datetime as _dt

    end = _dt.date.today().strftime("%Y-%m-%d")
    start = (_dt.date.today() - _dt.timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    sync_macro(start, end)
