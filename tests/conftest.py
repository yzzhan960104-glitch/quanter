"""Pytest 配置与共享 Fixtures"""
import sys
import types
from pathlib import Path

import pytest

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.fixture(autouse=True)
def _no_production_log_leak():
    """测试隔离兜底：每个用例结束后清掉指向 logs/quanter.log 的根 logger FileHandler。

    Why：lifespan 测试会给 root logger 挂生产文件 handler（LOG_CONFIG file），用例
    中途失败时 shutdown 段不执行 → handler 泄漏；后续任何真实 scheduler 日志都会
    写进生产 quanter.log（08-06 实证 pytest traceback 出现在生产日志）。本 fixture
    兜底清理，与 presentation/conftest 的 QUANTER_TESTING=1 双保险。
    """
    yield
    import logging
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler) and str(getattr(h, "baseFilename", "")).endswith("quanter.log"):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


# ============ Phase 1 Task 4：全局注入假 xtquant（collection 前生效）============
# Why 全局注入：qmt_gateway 顶部 `from xtquant.xttrader import XtQuantTrader` 在
# 真实 xtquant 可用时会绑定真实 C++ 类（实例化即连真实柜台，测试不可控）。conftest
# 是 pytest 收集时第一个被 import 的模块，在任何 trading.* 之前执行——此处把假
# xtquant 塞进 sys.modules（优先于文件系统查找），使后续所有 import trading.* 拿到
# 假模块，QmtExecutionGateway 可在无真实柜台环境被实例化与单测。CI 无 xtquant 同样生效。
def _install_fake_xtquant() -> None:
    if getattr(sys.modules.get("xtquant"), "_FAKE", False):
        return  # 已注入，避免重复覆盖

    # 假 xtconstant：枚举值与 qmt_gateway._QMT_ORDER_* 字面量契约一致
    fake_xtconstant = types.ModuleType("xtquant.xtconstant")
    fake_xtconstant.STOCK_BUY = 23
    fake_xtconstant.STOCK_SELL = 24
    fake_xtconstant.LATEST_PRICE = 5
    fake_xtconstant.FIX_PRICE = 11
    for _name, _val in [
        ("ORDER_UNREPORTED", 48), ("ORDER_REPORTED", 50), ("ORDER_REPORTED_CANCEL", 51),
        ("ORDER_CANCELED", 54), ("ORDER_PART_SUCC", 55), ("ORDER_SUCCEEDED", 56),
        ("ORDER_JUNK", 57),
    ]:
        setattr(fake_xtconstant, _name, _val)

    # 假 xttype.StockAccount
    fake_xttype = types.ModuleType("xtquant.xttype")

    class _FakeStockAccount:
        def __init__(self, acc_id, acc_type="STOCK"):
            self.account_id = acc_id
            self.account_type = 2  # 柜台内部类型编码，测试不关心具体值
    fake_xttype.StockAccount = _FakeStockAccount

    # 假 xtdata：get_full_tick 默认返空 dict（测试可 monkeypatch md.xtdata 覆盖）
    fake_xtdata = types.ModuleType("xtquant.xtdata")
    fake_xtdata.get_full_tick = lambda codes: {}

    # 假 xttrader：回调基类 + 可配置的 FakeXtQuantTrader
    fake_xttrader = types.ModuleType("xtquant.xttrader")

    class _FakeCallbackBase:
        pass

    class FakeXtQuantTrader:
        """可配置假 Trader：类属性 rc/seq/positions 作默认，实例记录所有调用。

        测试通过 monkeypatch.setattr(FakeXtQuantTrader, 'connect_rc', 1) 配置类级默认，
        或 monkeypatch 实例属性配置单例行为。
        """
        connect_rc = 0
        subscribe_rc = 0
        cancel_rc = 0
        order_seq = 100
        positions = None

        def __init__(self, path, sid):
            self.path, self.sid = path, sid
            self.cb = None
            self.calls = []

        def register_callback(self, cb):
            self.cb = cb
            self.calls.append("register_callback")

        def start(self):
            self.calls.append("start")

        def connect(self):
            self.calls.append("connect")
            return self.connect_rc

        def subscribe(self, acc):
            self.calls.append("subscribe")
            return self.subscribe_rc

        def stop(self):
            self.calls.append("stop")

        def order_stock_async(self, *args):
            self.calls.append(("order_stock_async", args))
            seq = self.order_seq
            self.order_seq += 1
            return seq

        def cancel_order_stock(self, acc, oid):
            self.calls.append(("cancel_order_stock", oid))
            return self.cancel_rc

        def query_stock_positions(self, acc):
            return self.positions

        def query_stock_asset(self, acc):
            return None

    fake_xttrader.XtQuantTrader = FakeXtQuantTrader
    fake_xttrader.XtQuantTraderCallback = _FakeCallbackBase

    # 假 xtquant 包
    fake_xt = types.ModuleType("xtquant")
    fake_xt._FAKE = True
    fake_xt.xtconstant = fake_xtconstant
    fake_xt.xtdata = fake_xtdata

    sys.modules["xtquant"] = fake_xt
    sys.modules["xtquant.xtconstant"] = fake_xtconstant
    sys.modules["xtquant.xttype"] = fake_xttype
    sys.modules["xtquant.xtdata"] = fake_xtdata
    sys.modules["xtquant.xttrader"] = fake_xttrader


_install_fake_xtquant()


# ============ 隔离 config 包 load_dotenv 的 .env 污染（治 eod_plan confirmed=True 误判）============
# Why 全局 autouse：config/__init__.py:18 模块级 load_dotenv()——任何 import 链触及 config
# 包即把 .env 注入 os.environ。完整 collection tests/trading/ 时某测试间接 import config
# → AUTO_CONFIRM_PLAN=true / AUTO_TRADE_MODE=live 被注入 → eod_plan (engine.py:256) 读到
# auto_confirmed=True → 落盘即 confirm_plan → plan.confirmed=True，而测试期望人审默认
# confirmed=False → 误判失败。本 fixture 强制覆盖为测试默认（人审 + dry_run）。
# 安全性：测试函数内显式 monkeypatch.setenv 同名变量会覆盖本默认（同一 monkeypatch 实例，
# 后序生效）；Grep 全 tests/ 无测试依赖 AUTO_CONFIRM_PLAN=true，故 autouse 不破坏既有用例。
@pytest.fixture(autouse=True)
def _isolate_trade_env(monkeypatch):
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "")
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
# ============ C-8 V1：隔离 job 台账 DB（防测试写真实 logs/trading_job_run.db）============
# Why autouse：pipeline_then_eod / pre_open 改造后会写台账；若不隔离，任何调用这些
# 函数的既有测试都会把「测试日」写成 done，污染真实启动补跑判定（漏跑被误判为已跑）。
# tmp_path 每测试唯一，天然互不干扰。
@pytest.fixture(autouse=True)
def _isolate_job_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_JOB_LEDGER_DB", str(tmp_path / "job_run.db"))


# ============ M4：resilience 单例跨用例污染根治（autouse reset）============
# Why autouse：data.resilience 的 breaker/limiter 是模块级共享单例。test_fetcher_resilience
# / test_akshare_client 等测试置 OPEN 后若无 finally 还原，后续依赖单例的测试读到 OPEN
# 误判（allow_request=False → 快速返空）。本 fixture 每用例 setup 前 reset 全部单例运行态
# （不改配置），治本——任何测试忘记还原也不污染。reset() 见 CircuitBreaker/RateLimiter。
@pytest.fixture(autouse=True)
def _reset_resilience_singletons():
    from data import resilience
    for _singleton in (
        resilience.tushare_rate_limiter_basic,
        resilience.tushare_rate_limiter_special,
        resilience.fred_rate_limiter,
        resilience.akshare_limiter,
        resilience.tushare_breaker,
        resilience.fred_breaker,
        resilience.akshare_breaker,
    ):
        _singleton.reset()
    # CR-3（2026-08-15）盘中组合级熔断节流（trading.alerting.PortfolioBreakerThrottle）
    # **刻意不入本清单**：它与 data.resilience 单例不同，按 W1-A「模块级可变状态收口」
    # 红线经 EnginePorts.breaker_throttle 注入（与 QuoteBlackoutThrottle 同范式）——
    # 无模块级单例可 reset，每用例自建 ports/engine 即自新（last_check_ts/miss_streak
    # 生命周期绑定 engine 实例）。若未来引入模块级默认实例，须即刻加入上方清单
    # （reset() 已就绪）；测试内复用同一实例做多轮 streak 场景时显式调 .reset()。
    yield


# ============ SSoT Phase A：tmp_db fixture（共享隔离 state_store DB）============
# Why 非 autouse：state_store 的 trade_event/order/fill/account 读写需显式 tmp DB 隔离，
# 但许多既有测试不触及 state_store（直接走 .venv310/logs/ 默认 DB 亦无副作用）。autouse 会
# 给每条用例强加 init_store + upsert_account 开销，且要求 trading 包可 import（部分纯算法
# 测试不需）。故采用显式注入：`def test_x(tmp_db, ...)`。物理意图：tmp_path 每测试唯一，
# monkeypatch state_store._DEFAULT_DB 让所有未显式传 db_path 的 insert_* 写入落到 tmp，
# account 行预置（trade_event.account_id 是 FK 引用，缺失会破坏 UNIQUE 约束语义）。
@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """tmp state_store DB + 默认 account 行（SSoT plan 共享 fixture）。

    返回 db_path str；测试用 `def test_x(tmp_db, ...)` 注入。
    """
    from trading import state_store
    db = tmp_path / "state.db"
    monkeypatch.setattr(state_store, "_DEFAULT_DB", str(db))
    state_store.init_store(str(db))
    state_store.upsert_account("ACC_TEST", broker="qmt")
    return str(db)
