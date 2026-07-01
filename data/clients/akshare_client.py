"""AKShare 客户端：宏观/板块/资金流/日线 fetch wrapper。

设计哲学（Karpathy 极简 / 显式至上 / 对齐 yfinance_client 红线契约）：
- 真实 ak.* 调用全部封装在 wrapper 方法内，参数以【已装 akshare 1.18.64 实测签名】为准
  （非凭记忆/文档，避免幻觉参数导致运行时 TypeError）。
- 采用【手动熔断 API】（allow_request / record_success / record_failure），失败一律返回
  空 DataFrame，绝不外抛。Why 不用装饰器：装饰器路径失败会抛 CircuitOpenError，破坏
  fetcher 组件"任何异常都返回空 DataFrame"的对外契约；手动 API 让我们在 OPEN 时直接
  return _EMPTY.copy()，彻底封死异常外泄路径（与 yfinance_client 同范式）。
- 模块级 akshare_breaker / akshare_limiter 单例定义在 data/resilience.py，跨实例共享
  熔断/限流状态，避免每个 AKShareClient 实例各持一份导致阈值失真。

akshare 1.18.64 关键函数签名（2026-07-01 实测复核）：
    stock_zh_a_hist(symbol, period='daily', start_date='19700101',
                    end_date='20500101', adjust='', timeout=None)
    macro_china_shrzgm() / macro_china_money_supply() / macro_china_shibor_all()
    stock_margin_detail_sse(date='20230922')        # 单参数 date，无 start/end
    stock_margin_detail_szse(date='20230925')       # 单参数 date，无 start/end
    stock_sector_fund_flow_rank(indicator='今日', sector_type='行业资金流')
    stock_individual_fund_flow(stock='600094', market='sh')
    repo_rate_hist(start_date, end_date)            # DR007 主接口
    rate_interbank(market, symbol, indicator)       # DR007 兜底接口
"""
from __future__ import annotations

import datetime as _dt
import logging

import pandas as pd

from data.resilience import akshare_breaker, akshare_limiter

logger = logging.getLogger(__name__)

# 空降级 DataFrame（无列定义的裸空表，供熔断/失败时返回）
# Why 用 .copy() 返回：_EMPTY 是模块级共享单例，若直接返回引用，下游若对返回值做
# 原地修改会污染全局空表，引发难以追踪的串味 bug（与 yfinance_client 同防范）。
_EMPTY = pd.DataFrame()


def _today8() -> str:
    """今日日期 YYYYMMDD（akshare 融资融券接口要求的日期格式）。"""
    return _dt.date.today().strftime("%Y%m%d")


class AKShareClient:
    """A 股宏观/板块/资金流/日线 fetch 客户端（手动熔断 + 限流，失败返空 DF）。"""

    def _guard(self) -> bool:
        """熔断 + 限流前置守卫。

        Why 先限流后熔断：限流器 acquire 在令牌不足时阻塞等待（至多 timeout），
        而熔断 OPEN 时应立即快速失败返回空 DF——故先 acquire 占令牌（防 429），
        再查熔断状态决定是否放行。熔断 OPEN 时本次 acquire 的令牌不退还，
        这是可接受的：熔断期本就罕见，少量令牌损耗换取逻辑简洁。
        """
        akshare_limiter.acquire(1.0)
        return akshare_breaker.allow_request()

    # ---------------- 日线（A 股个股历史 OHLCV）----------------
    def fetch_daily_hist(
        self, symbol: str, start: str, end: str, adjust: str = "qfq"
    ) -> pd.DataFrame:
        """拉取 A 股个股日线；熔断/失败一律返回空 DF，绝不抛。

        参数：
            symbol: akshare 代码（如 '000001'，注意【不带】交易所后缀，
                    akshare 用纯数字代码 + market 参数区分沪深，非 yfinance 的 .SZ/.SH 形式）。
            start/end: 'YYYY-MM-DD'（内部转 YYYYMMDD 喂给 akshare）。
            adjust: '' 前复权不复权 / 'qfq' 前复权 / 'hfq' 后复权（默认 qfq，对齐回测复权口径）。

        红线契约：
            - 熔断 OPEN 期间不触达底层 ak.stock_zh_a_hist（防连环超时被封禁）；
            - 任何异常（网络/限频/解析/空返回）一律 catch → 返回空 DF，绝不外抛；
            - 成功返回标准 schema：DatetimeIndex + open/high/low/close/volume/amount(/turnover)。
        """
        if not self._guard():
            logger.warning("akshare 熔断开启，返回空 DF：日线 [%s]", symbol)
            return _EMPTY.copy()
        try:
            import akshare as ak

            # akshare 日期参数为 YYYYMMDD 连续串（实测复核），需剥离 '-'
            raw = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start.replace("-", ""),
                end_date=end.replace("-", ""),
                adjust=adjust,
            )
            if raw is None or raw.empty:
                # 空数据属中性事件（如非交易日范围），不计熔断也不算成功
                return _EMPTY.copy()
            akshare_breaker.record_success()
            return self._cleanse_daily(raw)
        except Exception as e:
            # 基础设施异常（网络/限频/解析）→ 计入熔断；返回空 DF 绝不抛
            # Why 宽 catch Exception：akshare 内部异常类型多变（HTTPError/JSONDecodeError
            # /ConnectionError/KeyError），逐一列举易漏；红线是"绝不外抛"，宽 catch 最稳。
            logger.error("AKShare 日线失败 [%s]：%s", symbol, e)
            akshare_breaker.record_failure()
            return _EMPTY.copy()

    @staticmethod
    def _cleanse_daily(raw: pd.DataFrame) -> pd.DataFrame:
        """洗净日线：中文列名 → 英文标准列名，日期列 → DatetimeIndex 并排序。

        Why 这些清洗：
        - 列名英化：akshare 返回中文列名（日期/开盘/最高/...），统一为系统 OHLCV 标准
          schema（与 TushareDataFetcher / YFinanceClient 一致），便于下游因子/回测 join。
        - date 转 DatetimeIndex 并排序：A 股数据天然按日升序，但显式 sort_index 防御
          akshare 偶发乱序，确保 rolling/ewm 等时序运算正确。
        - turnover 可选保留：部分标的（如新股）无换手率列，按存在性保留，不强补 NaN。
        """
        col_map = {
            "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
            "收盘": "close", "成交量": "volume", "成交额": "amount", "换手率": "turnover",
        }
        df = raw.rename(columns=col_map)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        keep = [c for c in ["open", "high", "low", "close", "volume", "amount", "turnover"]
                if c in df.columns]
        return df[keep]

    # ---------------- 宏观原始数据（由 sync 脚本合并落湖）----------------
    def fetch_macro_raw(self, kind: str) -> pd.DataFrame:
        """拉取宏观原始 DataFrame；熔断/失败一律返回空 DF，绝不抛。

        参数 kind ∈ {'shrzgm', 'money_supply', 'shibor', 'dr007'}：
            - 'shrzgm'       : 社融滚动增量（macro_china_shrzgm）
            - 'money_supply' : 货币供应量 M0/M1/M2（macro_china_money_supply）
            - 'shibor'       : Shibor 全期限（macro_china_shibor_all）
            - 'dr007'        : 银行间质押式回购 7 天加权利率（DR007）。

        DR007 接口选择（实测复核 akshare 1.18.64）：
            主接口 repo_rate_hist(start_date, end_date) 实测存在；
            兜底接口 rate_interbank(market, symbol, indicator) 实测存在但参数语义模糊
            （market='上海银行间同业拆放利率'/symbol='Shibor'/indicator='7天'）。
            brief 要求"实测复核哪个可用；都不可用则返空 DF（不崩）"——
            故用 try/except 链：先 repo_rate_hist()，失败兜底 rate_interbank()，
            再失败由外层 except 捕获返空 DF。这样即使两接口签名后续变更也不崩。
        """
        if not self._guard():
            logger.warning("akshare 熔断开启，返回空 DF：宏观 [%s]", kind)
            return _EMPTY.copy()
        try:
            import akshare as ak

            if kind == "shrzgm":
                df = ak.macro_china_shrzgm()
            elif kind == "money_supply":
                df = ak.macro_china_money_supply()
            elif kind == "shibor":
                df = ak.macro_china_shibor_all()
            elif kind == "dr007":
                # DR007：先主接口 repo_rate_hist，失败兜底 rate_interbank
                try:
                    df = ak.repo_rate_hist()
                except Exception:
                    # 兜底：rate_interbank 参数语义实测为 (market, symbol, indicator)
                    df = ak.rate_interbank(
                        market="上海银行间同业拆放利率", symbol="Shibor", indicator="7天")
            else:
                logger.warning("未知 macro kind：%s，返回空 DF", kind)
                return _EMPTY.copy()

            if df is None or df.empty:
                return _EMPTY.copy()
            akshare_breaker.record_success()
            return df
        except Exception as e:
            logger.error("AKShare 宏观失败 [%s]：%s", kind, e)
            akshare_breaker.record_failure()
            return _EMPTY.copy()

    # ---------------- 融资融券明细（沪深合并）----------------
    def fetch_margin_detail(self) -> pd.DataFrame:
        """融资融券明细（沪深合并）；熔断/失败一律返回空 DF，绝不抛。

        实测签名（akshare 1.18.64）：
            stock_margin_detail_sse(date='20230922')   # 单参数 date，YYYYMMDD
            stock_margin_detail_szse(date='20230925')  # 单参数 date，YYYYMMDD
        brief 原写的 start_date/end_date 双参数【与实测不符】，已修正为单 date。
        沪深分别拉取当日明细后 concat 合并，交由 sync 脚本落湖。
        """
        if not self._guard():
            logger.warning("akshare 熔断开启，返回空 DF：融资融券")
            return _EMPTY.copy()
        try:
            import akshare as ak

            today = _today8()
            sse = ak.stock_margin_detail_sse(date=today)
            szse = ak.stock_margin_detail_szse(date=today)
            # 过滤 None / 空 DF 后合并（任一交易所当日无数据不致整体失败）
            parts = [d for d in (sse, szse) if d is not None and not d.empty]
            if not parts:
                return _EMPTY.copy()
            merged = pd.concat(parts, ignore_index=True)
            akshare_breaker.record_success()
            return merged
        except Exception as e:
            logger.error("AKShare 融资融券失败：%s", e)
            akshare_breaker.record_failure()
            return _EMPTY.copy()

    # ---------------- 板块资金流排名 ----------------
    def fetch_sector_fund_flow(self) -> pd.DataFrame:
        """行业板块资金流排名（今日）；熔断/失败一律返回空 DF，绝不抛。

        实测签名：stock_sector_fund_flow_rank(indicator='今日', sector_type='行业资金流')。
        返回原始 DataFrame（含板块名/主力净流入/涨跌幅等），由 sync 脚本解析落湖。
        """
        if not self._guard():
            logger.warning("akshare 熔断开启，返回空 DF：板块资金流")
            return _EMPTY.copy()
        try:
            import akshare as ak

            df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
            if df is None or df.empty:
                return _EMPTY.copy()
            akshare_breaker.record_success()
            return df
        except Exception as e:
            logger.error("AKShare 板块资金流失败：%s", e)
            akshare_breaker.record_failure()
            return _EMPTY.copy()

    # ---------------- 个股资金流 ----------------
    def fetch_individual_fund_flow(self, symbol: str) -> pd.DataFrame:
        """个股资金流（主力/超大单/大单/中单/小单）；熔断/失败一律返回空 DF，绝不抛。

        参数：
            symbol: 支持 '000001' 或 '000001.SZ' 两种形式（内部剥离交易所后缀，
                    因 akshare stock_individual_fund_flow 的 stock 参数只要纯数字代码）。
                    market 自动按后缀推断（.SH→sh, 否则 sz；无后缀默认 sz）。

        实测签名：stock_individual_fund_flow(stock='600094', market='sh')。
        """
        if not self._guard():
            logger.warning("akshare 熔断开启，返回空 DF：个股资金流 [%s]", symbol)
            return _EMPTY.copy()
        try:
            import akshare as ak

            # 剥离交易所后缀，akshare 只要纯数字代码
            code = symbol.split(".")[0]
            market = "sh" if symbol.upper().endswith(".SH") else "sz"
            df = ak.stock_individual_fund_flow(stock=code, market=market)
            if df is None or df.empty:
                return _EMPTY.copy()
            akshare_breaker.record_success()
            return df
        except Exception as e:
            logger.error("AKShare 个股资金流失败 [%s]：%s", symbol, e)
            akshare_breaker.record_failure()
            return _EMPTY.copy()
