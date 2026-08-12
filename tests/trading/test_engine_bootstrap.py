# -*- coding: utf-8 -*-
"""W3：TradingEngine.bootstrap() 收口 7 步 I/O 初始化（C-2 scheduling-orchestration Task 5）。

物理意图：原 ``trading/__main__._run_forever`` 把「连接网关 + 注册回调 + position_book.init_db
+ state_store.init_store + _migrate_env_to_account」7 步内联在入口里。W3 把这段 I/O
初始化提取到 ``TradingEngine.bootstrap()``（async），让独立 ``__main__`` 与 uvicorn
server lifespan 复用同一段初始化代码（构造(零 I/O) → bootstrap(I/O init) → start(调度启动)
三段分离）。

测试覆盖：
  - 主路径：网关非空 → connect/set_order_update_callback/三个 init 全调，``eng._gw`` 被赋值。
  - 降级路径：网关 None → 不抛，``eng._gw`` 保持 None（dry_run 影子模式合法）。

Mock 策略：patch ``trading.engine.get_gateway``（模块级函数）+ ``trading.position_book.init_db``
+ ``trading.state_store.init_store`` + ``trading.state_store._migrate_env_to_account``
（bootstrap 内函数局部 import 这些子模块，故 patch 源模块路径）。

W1-A/T2-Task17 patch 物理路径迁移（bootstrap engine 调用链归属判定）：
    本文件 8 测全调 ``eng.bootstrap()``——TradingEngine 实例方法（engine.py 内
    ``async def bootstrap``）。bootstrap 函数体内对 ``get_gateway`` / ``_alert_critical``
    均作 **engine 模块全局名 bare global 读取**（``gw = get_gateway()`` / 单实例锁被占用
    时调 ``_alert_critical(...)``）→ 经 engine ``__globals__`` 解析。按「符号被读取时的
    ``__globals__`` 归属模块」判 → **全 9 patch 保 trading.engine.X 不迁，0 处迁移**：

    - ``get_gateway`` ×8（C3 · 保 engine · L26/46/59/83/113/140/157/178）：bootstrap 内
      ``gw = get_gateway()`` 读引擎模块全局名 → engine ``__globals__``。engine.py:367
      自定义薄 wrapper（物理真身在 trading.gateway_service · Task 7 切断后 engine 顶部
      ``from trading.gateway_service import get_gateway`` re-export）即物理命中点。
      8 测覆盖主路径（客户端就绪 connect）/ 降级（gw=None）/ 未就绪跳过 connect /
      未就绪带诊断 / live 持锁 / live 拒锁 / dry_run 跳锁 / live+QUANTER_TESTING 跳锁
      全部经 engine.bootstrap → 保 engine.X 正确拦截。迁 phases 反 miss（phases 内
      ``get_gateway`` 本地绑定不经 bootstrap 调用链）。与 Task 10（health_guard）/
      Task 16（_stoploss）的 ``get_gateway`` 同构——engine 实例方法路径统一保 engine。
    - ``_alert_critical`` ×1（C3 engine re-export · 保 engine · L141）：bootstrap 单实例
      守护段在锁被占用时调 ``_alert_critical("检测到另一 TradingEngine 实例持有 QMT
      session=... 锁...")`` 读引擎模块全局名 → engine ``__globals__``。engine.py:93
      ``from trading.critical import _alert_critical`` re-export 整体绑定命中 patch
      （test_bootstrap_live_refuses_when_lock_held 验 alert.assert_called_once）。
      ``_alert_critical`` 物理真身在 trading.critical（T1-Task2 迁出），engine re-export
      即测试物理命中点；phases 内 ``_alert_critical`` 本地绑定不经 bootstrap 调用链 →
      不迁。（与 Task 10/13 _alert_critical 同构——bootstrap 路径统一保 engine。）

    按 ``__globals__`` 归属：bootstrap 是 engine 实例方法（engine.py 定义），其
    ``__globals__`` = ``trading.engine.__dict__``；``get_gateway`` / ``_alert_critical``
    均 re-export 绑定于 engine namespace（``'get_gateway' in vars(engine)`` /
    ``'_alert_critical' in vars(engine)`` 实证 True）→ patch trading.engine.X 即拦截
    bootstrap 内 bare global 读取的物理真身。phases ``__globals__`` 全程不经 →
    无需双口子 patch。

    绿门：8 passed（baseline 8 绿 → 仍 8 绿，零行为变更，patch 字符串零修改）。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from trading.engine import TradingEngine


@pytest.mark.asyncio
async def test_bootstrap_inits_db_and_connects():
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db") as pb, \
         patch("trading.state_store.init_store") as ss, \
         patch("trading.state_store._migrate_env_to_account") as mig:
        gw = MagicMock()
        gw.is_client_ready = lambda: True      # 客户端就绪 → 走连接主路径
        gw.connect = AsyncMock()
        gg.return_value = gw
        await eng.bootstrap()
        gw.connect.assert_awaited_once()
        gw.set_order_update_callback.assert_called_once()
        pb.assert_called_once()
        ss.assert_called_once()
        mig.assert_called_once()
        assert eng._gw is gw


@pytest.mark.asyncio
async def test_bootstrap_no_gateway_degrades():
    eng = TradingEngine()
    with patch("trading.engine.get_gateway", return_value=None), \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        await eng.bootstrap()  # 不抛
        assert eng._gw is None


@pytest.mark.asyncio
async def test_bootstrap_skips_connect_when_client_not_ready():
    """客户端未就绪 → bootstrap 不调 connect（防先于客户端创建会话文件 → -1 中毒，P0-1）。"""
    from unittest.mock import MagicMock
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        gw = MagicMock()
        gw.is_client_ready.return_value = False
        gw.connect = AsyncMock()
        gg.return_value = gw
        await eng.bootstrap()
    gw.connect.assert_not_awaited()                # 未就绪绝不 connect
    gw.set_order_update_callback.assert_called_once()  # 回调仍先接好，health_guard 连接后回报链路就绪
    assert eng._gw is gw


@pytest.mark.asyncio
async def test_bootstrap_not_ready_warning_carries_diag(caplog):
    """W1.2 收口 A：bootstrap 未就绪 WARNING 文案接入 _client_staleness_diag（启动期断线带根因）。

    物理意图：旧 WARNING 只说「未就绪」不带根因，操作员看到日志还要去翻 userdata 找原因。
    W1.2 接入 gw._client_staleness_diag()（T1 四态文案）让启动失败时日志自带诊断，
    与 _health_guard ④ 文案同口径（启动期 + 守护期断线可见性统一）。
    """
    import logging
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        gw = MagicMock()
        gw.is_client_ready.return_value = False
        gw._client_staleness_diag.return_value = "userdata 目录不存在（客户端未安装/路径错）"
        gw.connect = AsyncMock()
        gg.return_value = gw
        caplog.set_level(logging.WARNING, logger="trading.engine")
        await eng.bootstrap()
    gw.connect.assert_not_awaited()
    # 收口 A 核心断言：WARNING 文案含诊断
    diag_warns = [r for r in caplog.records
                  if r.levelno == logging.WARNING
                  and "目录不存在" in r.getMessage()]
    assert len(diag_warns) >= 1, "bootstrap 未就绪 WARNING 必须带 _client_staleness_diag 文案"


# ============================================================================
# QMT session 单实例锁（live 专属）：双引擎抢 session → connect -1 防御
# ============================================================================
@pytest.mark.asyncio
async def test_bootstrap_live_acquires_and_shutdown_releases_lock(monkeypatch, tmp_path):
    """live 模式 bootstrap 持有 session 锁；shutdown 释放（可再 acquire）。"""
    from trading import single_instance
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QMT_SESSION_ID", "999")
    monkeypatch.setenv("TRADING_ENGINE_LOCK_DIR", str(tmp_path))
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        gw = MagicMock()
        gw.is_client_ready = lambda: True
        gw.connect = AsyncMock()
        gg.return_value = gw
        await eng.bootstrap()
    assert (tmp_path / "trading_engine_999.lock").exists()
    assert getattr(eng, "_instance_lock", None) is not None
    eng.shutdown()  # 优雅停机 → 释放锁
    reacquired = single_instance.acquire("999", lock_dir=str(tmp_path))
    assert reacquired is not None
    reacquired.release()


@pytest.mark.asyncio
async def test_bootstrap_live_refuses_when_lock_held(monkeypatch, tmp_path):
    """第二实例拿不到锁 → 拒连网关（raise）+ CRITICAL 告警。"""
    from trading import single_instance
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QMT_SESSION_ID", "999")
    monkeypatch.setenv("TRADING_ENGINE_LOCK_DIR", str(tmp_path))
    held = single_instance.acquire("999", lock_dir=str(tmp_path))
    assert held is not None
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.engine._alert_critical") as alert:
        gg.return_value = AsyncMock()
        with pytest.raises(RuntimeError, match="单实例锁"):
            await eng.bootstrap()
    gg.return_value.connect.assert_not_awaited()
    alert.assert_called_once()
    held.release()


@pytest.mark.asyncio
async def test_bootstrap_dry_run_skips_session_lock(monkeypatch, tmp_path):
    """dry_run 不持锁（无真 session，不干扰开发多开/测试）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    monkeypatch.setenv("QMT_SESSION_ID", "999")
    monkeypatch.setenv("TRADING_ENGINE_LOCK_DIR", str(tmp_path))
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        gw = MagicMock()
        gw.is_client_ready = lambda: True
        gw.connect = AsyncMock()
        gg.return_value = gw
        await eng.bootstrap()
    assert not (tmp_path / "trading_engine_999.lock").exists()
    assert getattr(eng, "_instance_lock", None) is None


@pytest.mark.asyncio
async def test_bootstrap_live_skips_session_lock_when_testing(monkeypatch, tmp_path):
    """B3: live + QUANTER_TESTING=1 → 不 acquire session 锁（测试不抢生产锁）。"""
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    monkeypatch.setenv("QUANTER_TESTING", "1")
    monkeypatch.setenv("QMT_SESSION_ID", "999")
    monkeypatch.setenv("TRADING_ENGINE_LOCK_DIR", str(tmp_path))
    eng = TradingEngine()
    with patch("trading.engine.get_gateway") as gg, \
         patch("trading.position_book.init_db"), \
         patch("trading.state_store.init_store"), \
         patch("trading.state_store._migrate_env_to_account"):
        gw = MagicMock()
        gw.is_client_ready = lambda: True
        gw.connect = AsyncMock()
        gg.return_value = gw
        await eng.bootstrap()
    assert not (tmp_path / "trading_engine_999.lock").exists()
    assert getattr(eng, "_instance_lock", None) is None
