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
# W7（2026-08-28 完成退役）：假 xtquant 注入摘除——唯一消费方 trading.qmt_gateway
# 已随 QMT live 面删除，无 xtquant 依赖面（历史考古：archive/qmt-stack-final）。


# ============ 隔离 config 包 load_dotenv 的 .env 污染（治 eod_plan confirmed=True 误判 + test_trading_api 恒 401）============
# Why 全局 autouse：config/__init__.py:18 模块级 load_dotenv()——任何 import 链触及 config
# 包即把 .env 注入 os.environ。完整 collection tests/trading/ 时某测试间接 import config
# → AUTO_CONFIRM_PLAN=true / AUTO_TRADE_MODE=live 被注入 → eod_plan (engine.py:256) 读到
# auto_confirmed=True → 落盘即 confirm_plan → plan.confirmed=True，而测试期望人审默认
# confirmed=False → 误判失败。本 fixture 强制覆盖为测试默认（人审 + dry_run）。
# 恒 401 同根（2026-08-18 扩面 QUANTER_API_TOKEN，tests/test_trading_api 9 用例全灭）：
#   1. .env 08-16 起配置 QUANTER_API_TOKEN → require_write（presentation/server/http/auth.py，
#      请求时读 env）走「token 已配 + 无 Bearer → 401」分支，恒拒不带鉴权头的 TestClient
#      （6 例 assert 401==200/503 + 3 例 KeyError 皆此 401 detail JSON 之表象分裂）；
#   2. 本 delenv 强制测试态「token 未配置 + dry_run → 放行 + WARNING」（auth.py 开发态语义，
#      DG-G2：fail-closed 仅作用于 live，测试态 AUTO_TRADE_MODE 已被上出行钉死 dry_run）；
#   3. 显式 monkeypatch.setenv("QUANTER_API_TOKEN", ...) 的鉴权用例后序覆盖本 delenv
#      （同一 monkeypatch 实例，后序生效），token 校验语义用例不受影响。
# 安全性：测试函数内显式 monkeypatch.setenv 同名变量会覆盖本默认（同一 monkeypatch 实例，
# 后序生效）；Grep 全 tests/ 无测试依赖 AUTO_CONFIRM_PLAN=true，故 autouse 不破坏既有用例；
# 无 .env 环境（变量本就不存在）delenv 为 no-op（raising=False），行为不变。
@pytest.fixture(autouse=True)
def _isolate_trade_env(monkeypatch):
    monkeypatch.setenv("AUTO_CONFIRM_PLAN", "")
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    # .env 注入的 QUANTER_API_TOKEN 若在场即摘除——防 require_write「token 已配 + 无 Bearer
    # → 401」分支拒无鉴权 TestClient（test_trading_api 9 用例恒 401 根因，Why 见上方注释块）。
    monkeypatch.delenv("QUANTER_API_TOKEN", raising=False)
    # 委员会评审闸默认关（2026-09-06 质证工序）：防 publish 路径测试触发真实 GLM Tier A
    # 评审（review.py 的 load_dotenv 会把 .env 的 GLM_API_KEY 带进测试进程）——烧配额+
    # 拖慢测试。需要开闸的用例在测试内 monkeypatch.setenv("COMMITTEE_GATE", "1") 后序覆盖。
    monkeypatch.setenv("COMMITTEE_GATE", "0")
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


# ============ tushare 注册表隔离（W2 收口 · 2026-08-19：原 5 文件复制块单源化）============
# Why 非全局 autouse：仅数据层测试 mutate 全局注册表，全局 autouse 会给所有用例
# 强加 deepcopy 开销。数据层各文件保留 2 行 autouse 薄壳引用本 fixture（文件内生效）。
@pytest.fixture
def tushare_registry_isolated():
    """深拷贝 TUSHARE_DATASETS + LAKE_CONFIG['lakes']，测试后还原原对象。

    Why 深拷贝：数据层测试就地覆盖全局注册表（重定向 lake 到 tmp_path / 覆盖字段集），
    若不还原会污染后续测试（测试顺序依赖、隔离性破坏）。深拷贝处理嵌套 dict
    （lakes 子键），yield 后还原引用让其它模块看到原始未改动的配置。
    """
    import copy as _copy
    from config import TUSHARE_DATASETS, LAKE_CONFIG
    saved_datasets = _copy.deepcopy(TUSHARE_DATASETS)
    saved_lakes = _copy.deepcopy(LAKE_CONFIG["lakes"])
    yield
    TUSHARE_DATASETS.clear()
    TUSHARE_DATASETS.update(saved_datasets)
    LAKE_CONFIG["lakes"].clear()
    LAKE_CONFIG["lakes"].update(saved_lakes)


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
