"""宏观信贷同步：Tushare cn_m(M0/M1/M2) + akshare fallback(社融/DR007) → 日频对齐 parquet。

源切换（Plan C Task 2，用户决策：宏观切 Tushare）：
    - M0/M1/M2 → Tushare cn_m（货币供应量同比，月频 month=YYYYMM）
    - 社融(shrzgm)/DR007 → Tushare 无专门接口，fallback akshare 单指标

CreditRegime 不变量（core/macro_regime.py:154）：macro 湖必须含 shrzgm + M1M2_gap
两列，dr007 可选。本脚本保证列名对齐——CreditRegime 代码不改。任何 rename 都会
让 test_credit_regime_unchanged_reads_columns 先红，比改 sync 早暴露。

前视红线（Look-ahead bias 防护，不变）：
    社融/M1M2 是【月频】数据，每月才公布一次。要把它们 join 到日频策略上，
    必须把月度值"展平"到日频——但展平方向只有【向前 ffill】是合法的：
    即用"已公布的最近一期月度值"解释当下的每一天。
    【绝不可】用 bfill 向后回填——否则会把"未来才公布的月度数据"提前泄漏给
    历史交易日，构成前视偏差，回测曲线完美但实盘直接崩盘（典型未来函数陷阱）。
    本脚本 align_to_daily() 严格 ffill-only，无任何 bfill，是整个宏观 CTA
    四级数据湖（宏观月频→板块日频→微观日线→分钟）顶层的合规基石。

⚠️ 事实审查 / 待探测风险（brief §Step 3 注）：
    Tushare cn_m 接口的参数名(start_m/end_m vs start_date/end_date)与字段名
    (m0_yoy/m1_yoy/m2_yoy) 为 brief 假设——开发机无 token，单测用 fake_pro mock
    不验证真实字段。生产首次联调需用真 token 探测：
        python -c "import tushare as ts; pro=ts.pro_api('TOKEN'); \
                    print(pro.cn_m(start_m='202401', end_m='202402').columns.tolist())"
    若字段名不同，调整 _fetch_with_guard("cn_m", ...) 的参数与下方列对齐逻辑。
"""
from __future__ import annotations

import os
import sys

# 加项目根到 sys.path：脚本可从任意 cwd 直接 `python scripts/xxx.py` 运行。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from config import LAKE_CONFIG
from data._tushare_compat import get_pro
from data.clients.akshare_client import AKShareClient
from data.resilience import tushare_breaker, tushare_rate_limiter


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


def _fetch_with_guard(api_name: str, **kwargs) -> pd.DataFrame:
    """限频+熔断包装（与 data.tushare_sync 同范式，反黑盒显式重写）。

    Why 单独实现而非 import tushare_sync._fetch_with_guard：sync_macro_credit 是
    按月拉取的轻量场景（cn_m 单次单月），不涉及 tushare_sync 的分页/续传语义；
    复用其 helper 反而引入不必要的耦合（改一处影响全局）。此处显式重写限频+熔断
    包装，保持宏观脚本独立可演进。

    失败语义：限频 acquire(1.0) → 熔断 allow_request() → getattr(pro, api) →
    任一异常/空返空 DF（不抛，让上游按列缺失降级——单档缺失不崩宏观湖）。
    """
    tushare_rate_limiter.acquire(1.0)
    if not tushare_breaker.allow_request():
        return pd.DataFrame()
    pro = get_pro()
    try:
        df = getattr(pro, api_name)(**kwargs)
    except Exception:
        tushare_breaker.record_failure()
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    tushare_breaker.record_success()
    return df


def fetch_macro_series(start: str, end: str) -> pd.DataFrame:
    """合并 M0/M1/M2(Tushare cn_m) + 社融/DR007(akshare fallback) → 日频对齐。

    M1M2_gap 衍生 = M1同比 - M2同比（CreditRegime core 字段，货币活性剪刀差）。
    含义：M1 增速 > M2 → 资金活化（企业活期存款上行，投资活跃）→ 宽信用预期；
         M1 < M2 → 资金沉淀为定期/储蓄 → 紧信用预期。CreditRegime 据此判状态迁移。

    容错策略（任一档缺失不崩，按列合并）：
        - cn_m 月频(M1/M2 同比) → align_to_daily 仅 ffill 到日频 → 衍生 M1M2_gap；
        - 社融(shrzgm)/DR007 → akshare fallback，月/日频 ffill 到日频；
        - 列合并后再 ffill：弥合各档时间起点不同留下的头部 NaN（仍只向前，无 bfill）。
    """
    series: dict[str, pd.Series] = {}

    # M0/M1/M2 走 Tushare cn_m（月频，month=YYYYMM）。
    # ⚠️ 参数 start_m/end_m 为 brief 假设（6 位 YYYYMM），待真 token 探测确认。
    #    截取 [:6] 容错 YYYY-MM-DD → YYYYMM（cn_m 接口按月查）。
    cn_m = _fetch_with_guard(
        "cn_m",
        start_m=start.replace("-", "")[:6],
        end_m=end.replace("-", "")[:6],
    )
    if not cn_m.empty:
        # _pick 把 cn_m 的 month 列防御性归一（若上游改名为别的，仍能取到日期列）。
        m = align_to_daily(_pick(cn_m, "month"), "month", start, end)
        # cn_m 字段名 m1_yoy/m2_yoy → CreditRegime 消费的标准名 M1同比增长/M2同比增长。
        # 归一映射而非硬编码列名：字段漂移时只改映射表，不改下游算式。
        for col, key in [("m1_yoy", "M1同比增长"), ("m2_yoy", "M2同比增长")]:
            if col in m.columns:
                series[key] = pd.to_numeric(m[col], errors="coerce")

    # 社融 shrzgm + DR007：akshare fallback（Tushare 无专门接口，spec §3.7 风险条款）。
    # Why 保留 akshare：Tushare cn_m 只覆盖货币供应量同比，社融增量(shrzgm)与银行间
    # 7 天回购(DR007) 无对应 Tushare 接口，akshare 的 macro_china_shrzgm /
    # repo_rate_hist 仍是当前最干净的源——三源并存而非强求单一接口。
    ak = AKShareClient()
    shrzgm = ak.fetch_macro_raw("shrzgm")
    if not shrzgm.empty:
        s = align_to_daily(_pick(shrzgm, "月份"), "月份", start, end)
        series["shrzgm"] = s.iloc[:, 0]
    dr007 = ak.fetch_macro_raw("dr007")
    if not dr007.empty:
        d = align_to_daily(_pick(dr007, "日期"), "日期", start, end)
        series["dr007"] = d.iloc[:, 0]

    if not series:
        return pd.DataFrame()
    # 列合并后再 ffill：弥合各档时间起点不同留下的头部 NaN（仍只向前，无 bfill）。
    df = pd.DataFrame(series).ffill().dropna(how="all")
    # M1M2_gap 衍生：M1同比 - M2同比（CreditRegime core 字段，必须在 ffill 之后算，
    # 保证整段时序连续；任一同比缺失则 M1M2_gap 整段 NaN——CreditRegime 走中性兜底）。
    if "M1同比增长" in df and "M2同比增长" in df:
        df["M1M2_gap"] = (
            pd.to_numeric(df["M1同比增长"], errors="coerce")
            - pd.to_numeric(df["M2同比增长"], errors="coerce")
        )
    return df


def sync_macro(start: str, end: str, out: str | None = None) -> None:
    """落 data_lake/macro_credit.parquet（DatetimeIndex，列含 shrzgm + M1M2_gap）。

    参数：
        start/end: 'YYYY-MM-DD' 同步区间。
        out: 自定义输出路径（默认 LAKE_CONFIG['lakes']['macro']）。
    """
    out = out or LAKE_CONFIG["lakes"]["macro"]
    df = fetch_macro_series(start, end)
    if df.empty:
        print("宏观数据为空，跳过")
        return
    os.makedirs(os.path.dirname(out), exist_ok=True)
    df.to_parquet(out)
    print(f"宏观湖写入：{out}，{len(df)} 行，列={list(df.columns)}")


if __name__ == "__main__":
    # 默认同步近 2 年（宏观月频数据 2 年约 24 期，足以判 CreditRegime 状态迁移）。
    import datetime as _dt

    end = _dt.date.today().strftime("%Y-%m-%d")
    start = (_dt.date.today() - _dt.timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    sync_macro(start, end)
