"""Task 6：板块两融 + 活跃股初筛 —— 漏斗中层（板块日频 → 50 只活跃股日线）。

漏斗物理意图（Why）：
    宏观信贷扩张（Task 5 社融/M1M2/DR007）先在【板块融资余额】端显形——机构与杠杆
    资金率先涌入景气板块，融资余额环比增速领跑；随后才传导到板块内【活跃个股】
    （换手率/动量放大）。本漏斗用「融资增速 → top 板块 → 个股活跃度」两级过滤，
    把全市场 5000+ 只 A 股压缩到 ≤50 只活跃池，避免对全市场拉取日线（IO 爆炸），
    也为下游分钟级因子（Task 8/12）收敛候选域。

行业映射现实拷问（降级路径）：
    akshare 的 fetch_margin_detail()（stock_margin_detail_sse/szse）返回的是
    【个股融资余额明细】，列名随版本漂移，且【未必含"行业"列】。若 margin 无行业
    列，个股→申万行业映射需额外调用（akshare 个股信息接口），耦合复杂且不稳定。
    本模块采用【显式降级链】（详见 select_active_pool 注释）：
        主路径：margin 含"行业"列 → groupby 行业算融资增速 → top 板块 → 板块内个股
        降级 A：margin 无行业列，但有板块资金流（fetch_sector_fund_flow）→ 直接用
                板块资金流排名 top_n 的板块名（仅落盘用，无法反查个股成分）→ 此时
                个股池用 fetch_individual_fund_flow 主力净流入兜底选 top
        降级 B：margin 与板块资金流都失效 → 返回空池（绝不抛，落盘跳过）
    测试用小池（top_n=1, pool_size=2）验证漏斗筛选逻辑，真实 pool_size=50 由 config。
"""
import pandas as pd


class _FakeClient:
    """mock AKShareClient：融资融券/板块资金流/个股日线假数据。

    daily 返回对齐 Task 4 wrapper _cleanse_daily 后的 schema（英文列名 + DatetimeIndex），
    这样测试与生产代码用同一份 schema，避免「测试 mock 中文名、生产用英文名」错位陷阱。
    """

    def fetch_margin_detail(self) -> pd.DataFrame:
        """3 只个股 + 2 个行业的融资余额（明日细算环比增速用）。

        银行：000001=110 / 600000=90 → 合计 200；地产：000002=105 → 合计 105。
        （测试 prev={"银行":100,"地产":100}：银行增速 (200-100)/100=+100%，
         地产增速 (105-100)/100=+5% → top1 应为「银行」。）
        """
        return pd.DataFrame({
            "标的代码": ["000001.SZ", "000002.SZ", "600000.SH"],
            "行业": ["银行", "地产", "银行"],
            "融资余额": [110.0, 105.0, 90.0],
        })

    def fetch_sector_fund_flow(self) -> pd.DataFrame:
        """板块资金流（板块名 + 主力净流入），仅作降级/落盘用。"""
        return pd.DataFrame({
            "名称": ["银行", "地产", "钢铁"],
            "今日主力净流入-净额": [1e8, 5e7, 1e7],
        })

    def fetch_individual_fund_flow(self, symbol: str) -> pd.DataFrame:
        """个股资金流（降级兜底用，本组测试主路径不触达）。"""
        return pd.DataFrame({"代码": [symbol], "今日主力净流入-净额": [1e7]})

    def fetch_daily_hist(self, symbol: str, start: str, end: str, adjust: str = "qfq") -> pd.DataFrame:
        """近 20 日假日线（对齐 _cleanse_daily 后 schema：英文列名 + DatetimeIndex）。

        给每只票一个【确定性递增的收盘序列】，使 pct_change().sum()（动量）
        随 symbol 末位代码递增——便于断言 top1=银行 后池内排序稳定。
        """
        base = int(symbol.split(".")[0][-1])  # 取代码末位 → 1/2/0
        idx = pd.bdate_range("2024-01-02", periods=20)
        close = pd.Series([10 + base + i * 0.1 for i in range(20)], index=idx, name="close")
        return pd.DataFrame({
            "open": close - 0.05, "high": close + 0.05, "low": close - 0.1,
            "close": close, "volume": [1000 + base * 100 + i for i in range(20)],
            "amount": close * 1000,
        }, index=idx)


def test_compute_margin_growth_top_sectors():
    """融资余额环比增速：按行业 groupby → 取前 N 板块（增速降序）。

    断言：银行合计 200 vs 昨日 100 → 增速 +1.0；地产合计 105 vs 昨日 100 → +0.05。
    故 top1 必为「银行」，且银行增速严格 > 地产增速。
    """
    from scripts.sync_sector_daily import compute_margin_growth

    margin = pd.DataFrame({
        "标的代码": ["000001.SZ", "000002.SZ", "600000.SH"],
        "行业": ["银行", "地产", "银行"],
        "融资余额": [110.0, 105.0, 90.0],   # 假昨日 100/100/100
    })
    growth = compute_margin_growth(margin, prev={"银行": 100.0, "地产": 100.0})
    top3 = growth.sort_values("growth", ascending=False).head(3)
    assert "银行" in top3["行业"].tolist()  # 银行 110+90=200 vs 100 → 增速正且最高
    # 严格断言排序：银行增速 +1.0 必 > 地产增速 +0.05
    assert top3.iloc[0]["行业"] == "银行"
    assert top3.iloc[0]["growth"] > top3.iloc[1]["growth"]


def test_select_active_pool_size_and_source():
    """活跃池：来自 top 板块内、按动量/换手排序，定 pool_size 只（测试用小池 top_n=1/pool_size=2）。

    断言三件套：
        1. 返回 list[str]；
        2. 长度 ≤ pool_size（2）；
        3. 池内 symbol 必来自 top1 板块（银行 = 000001.SZ + 600000.SH），
           绝不应混入地产（000002.SZ）——验证漏斗「板块内筛」物理意图。
    """
    from scripts.sync_sector_daily import select_active_pool

    pool = select_active_pool(
        _FakeClient(), top_n=1, pool_size=2,
        # 注入昨日行业余额，使增速有区分度（银行+100% vs 地产+5%）→ top1 严格=银行。
        # 实盘此参数从昨日 sector.parquet 读取；测试显式注入避免冷启增速全 0 的不稳定排序。
        prev={"银行": 100.0, "地产": 100.0},
    )
    assert isinstance(pool, list) and len(pool) <= 2
    # 漏斗红线：池内只允许 top1（银行）的成分股，地产必须被排除
    assert set(pool).issubset({"000001.SZ", "600000.SH"})
    assert "000002.SZ" not in pool   # 地产不在 top1，绝不应入池
