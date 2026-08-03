# -*- coding: utf-8 -*-
"""M1 网关健康守卫单测：就绪探测 + 重连互斥 + 守护 job。"""
import os, time, pytest
from unittest.mock import AsyncMock

def _gw(userdata):
    from broker.qmt import QmtExecutionGateway
    return QmtExecutionGateway(userdata_path=userdata, account_id="10110356", session_id=777888)

def test_is_client_ready_false_when_dir_missing():
    gw = _gw("/nonexistent/path/xyz")
    assert gw.is_client_ready() is False

def test_is_client_ready_false_when_files_stale(tmp_path):
    """userdata 下文件都 >5min 未动 → 客户端没在跑 → False。"""
    (tmp_path / "down_queue_win_777888").write_bytes(b"x")
    old = time.time() - 9999
    os.utime(tmp_path / "down_queue_win_777888", (old, old))
    gw = _gw(str(tmp_path))
    assert gw.is_client_ready(staleness_sec=300) is False

def test_is_client_ready_true_when_file_fresh(tmp_path):
    """近 5min 内有活跃 shm/queue 文件 → 客户端在跑 → True。"""
    (tmp_path / "miniqmtShmStockListCacheSZO").write_bytes(b"x")  # 刚创建=新
    gw = _gw(str(tmp_path))
    assert gw.is_client_ready(staleness_sec=300) is True


# ============================================================ M1 重连互斥
def test_reconnecting_flag_default_false(tmp_path):
    """构造后 _reconnecting 默认 False（未在重连）——互斥标志的干净初态。"""
    gw = _gw(str(tmp_path))
    assert gw._reconnecting is False


@pytest.mark.asyncio
async def test_reconnect_skips_if_already_reconnecting(monkeypatch, tmp_path):
    """_reconnecting=True 时 _reconnect 立即返回且不调 connect（互斥让出）。

    场景：on_disconnected→_reconnect 与 T8 守护 job 是两条重连路径；
    若无互斥，并发触发会同时 start/connect 同一 sid，QMT 会返回 -1（session 占用）。
    本测试验证第一条路径持有 _reconnecting=True 时，第二条路径直接让出。
    """
    from unittest.mock import AsyncMock
    gw = _gw(str(tmp_path))
    # 互斥标志已被另一条路径持有
    gw._reconnecting = True
    # connect 作 spy：若互斥失效会真调 connect（触发 xtquant import 等副作用）
    monkeypatch.setattr(gw, "connect", AsyncMock())
    await gw._reconnect()  # type: ignore[func-returns-value]
    # 核心断言：互斥生效 → connect 未被调用
    gw.connect.assert_not_awaited()
    # 让出路径不应清他人持有的标志（finally 不能误清他人锁）
    assert gw._reconnecting is True


# ============================================================ T8：守护 job（_health_guard）
# M1 自愈的最后一块：TradingEngine._health_guard（apscheduler interval 60s）。
# 场景：启动 connect 失败 / 盘中断线 → 守护 job 周期探测 is_client_ready→connect 恢复 live。
@pytest.mark.asyncio
async def test_health_guard_noop_when_connected():
    """已连接时守护 job 直接返回（不捣乱活跃连接）。

    物理意图：盘中活跃连接不能被周期 job 重连打断（重连会断开活跃 session 重建，
    导致回报回调丢失）。_connected=True → 清失败计数 + no-op。
    """
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock(); gw._connected = True
    gw.connect = AsyncMock()  # spy：若误调会污染活跃连接
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_not_awaited()  # 已连，绝不重连
    assert eng._guard_fail_count == 0  # 已连清计数


@pytest.mark.asyncio
async def test_health_guard_reconnects_when_ready_and_disconnected():
    """未连接但客户端就绪 → 调 connect 恢复。

    场景：启动 connect 失败 / 盘中断线后 miniQMT 客户端已自行恢复
    （is_client_ready=True 标志 shm 文件 mtime 新鲜）→ 守护 job 调 connect 重建 session。
    """
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False  # #6：风控熔断标志默认 False（网络断线自愈路径）
    gw._connected = False
    gw._reconnecting = False  # 无并发重连路径在跑
    gw.is_client_ready = MagicMock(return_value=True)
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_awaited_once()  # 就绪→重连一次


@pytest.mark.asyncio
async def test_health_guard_skips_when_client_not_ready():
    """客户端未就绪 → 不调 connect（防刷柜台）。

    物理意图：miniQMT 客户端进程未起 / userdata 共享内存文件 mtime 过期（>5min）时，
    connect 必然失败（start/connect 拿不到通信通道）→ 每 60s 空跑 connect 只会刷柜台
    日志/触发限流。is_client_ready=False 时跳过，等客户端真就绪再连。
    """
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False  # #6：风控熔断标志默认 False（网络断线自愈路径）
    gw._connected = False; gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=False)
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_not_awaited()  # 未就绪，不空跑


@pytest.mark.asyncio
async def test_health_guard_yields_when_reconnecting():
    """_reconnecting=True 时守护 job 让出（不与 on_disconnected 路径并发抢连）。

    场景：on_disconnected 已触发 _reconnect 并持有 _reconnecting=True 互斥标志，
    同一时刻守护 job 也想重连 → 必须让出（否则两条路径同时 start/connect 同一 sid，
    QMT 返回 -1 session 占用）。
    """
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False  # #6：风控熔断标志默认 False（网络断线自愈路径）
    gw._connected = False
    gw._reconnecting = True  # on_disconnected 路径正在重连
    gw.is_client_ready = MagicMock(return_value=True)
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_not_awaited()  # 让出，不抢


@pytest.mark.asyncio
async def test_health_guard_backoff_after_connect_failure():
    """connect 连续失败 → 退避跳过若干轮（等效指数退避，不刷柜台）。

    物理意图：connect 连续失败（柜台持续不可用）时空跑无意义，按失败次数退避
    （0→0,1→0,2→1,3→3,≥4→7 轮，60s/轮 ≈ 60→120→240→480s）。
    """
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False  # #6：风控熔断标志默认 False（网络断线自愈路径）
    gw._connected = False; gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=True)
    gw.connect = AsyncMock(side_effect=RuntimeError("柜台拒绝"))
    with patch("trading.engine.get_gateway", return_value=gw):
        # 第 1 轮：fail_count=0 → skip=0 → 调 connect → 失败 fail_count=1
        await eng._health_guard()
        assert gw.connect.await_count == 1
        assert eng._guard_fail_count == 1
        # 第 2 轮：fail_count=1 → skip=0 → 调 connect → 失败 fail_count=2
        await eng._health_guard()
        assert gw.connect.await_count == 2
        assert eng._guard_fail_count == 2
        # 第 3 轮：fail_count=2 → skip=1 → rounds_since_fail=0<1 → 跳过
        await eng._health_guard()
        assert gw.connect.await_count == 2  # 未调
        # 第 4 轮：fail_count=2 → skip=1 → rounds_since_fail=1≥1 → 调 connect → 失败 fail_count=3
        await eng._health_guard()
        assert gw.connect.await_count == 3
        assert eng._guard_fail_count == 3


@pytest.mark.asyncio
async def test_health_guard_reconnect_triggers_pre_open_catchup():
    """重连成功后 → 调 pre_open 补挂（R1：窗口内补挂，ledger 幂等由 catchup 守）。"""
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False
    gw._connected = False
    gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=True)
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.catchup._catchup_pre_open",
               new=AsyncMock(return_value=(True, "已补跑"))) as cp:
        await eng._health_guard()
    gw.connect.assert_awaited_once()
    cp.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_health_guard_no_catchup_when_connect_fails():
    """重连失败 → 不调 pre_open 补挂（等下次重连成功）。"""
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False
    gw._connected = False
    gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=True)
    gw.connect = AsyncMock(side_effect=RuntimeError("柜台拒绝"))
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.catchup._catchup_pre_open", new=AsyncMock()) as cp:
        await eng._health_guard()
    gw.connect.assert_awaited_once()
    cp.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_guard_no_catchup_when_already_connected():
    """已连接 → 不重连也不补挂（活跃连接不捣乱）。"""
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw), \
         patch("trading.catchup._catchup_pre_open", new=AsyncMock()) as cp:
        await eng._health_guard()
    gw.connect.assert_not_awaited()
    cp.assert_not_awaited()


def test_guard_skip_rounds_mapping():
    """_guard_skip_rounds 退避映射：0→0,1→0,2→1,3→3,≥4→7。"""
    from trading.engine import TradingEngine
    assert TradingEngine._guard_skip_rounds(0) == 0
    assert TradingEngine._guard_skip_rounds(1) == 0
    assert TradingEngine._guard_skip_rounds(2) == 1
    assert TradingEngine._guard_skip_rounds(3) == 3
    assert TradingEngine._guard_skip_rounds(4) == 7
    assert TradingEngine._guard_skip_rounds(99) == 7  # 上限≈8min


def test_health_guard_job_registered_in_init():
    """守护 job 在 __init__ 内随 sched.add_job 注册（interval 60s，id=_health_guard）。

    物理意图：与 _stoploss 同机制在 __init__ 装配（apscheduler AsyncIOScheduler.add_job），
    start() 仅 sched.start()，job 注册不应在 start 内（既有 4 个 cron 同在 __init__）。
    """
    from trading.engine import TradingEngine
    eng = TradingEngine()
    job_ids = [j.id for j in eng.sched.get_jobs()]
    assert "_health_guard" in job_ids
    # 不破坏既有 4 个 cron（C-2 Task 9：eod_plan → pipeline_then_eod 事件链）
    assert {"pipeline_then_eod", "pre_open", "stop_loss", "post_close"} <= set(job_ids)
    # interval 触发器，60s
    job = eng.sched.get_job("_health_guard")
    assert job.trigger.__class__.__name__ == "IntervalTrigger"
    assert int(job.trigger.interval.total_seconds()) == 60


# ============================================================ T7 补：_reconnect happy-path 复位
# （T7 reviewer M-1 建议：重连完成后 _reconnecting 回 False 的复位测试）
@pytest.mark.asyncio
async def test_reconnect_resets_flag_on_success(tmp_path):
    """_reconnect 成功后 _reconnecting 回 False（互斥标志 happy-path 复位）。

    场景：on_disconnected→_reconnect 置 _reconnecting=True 重连 → connect 成功 →
    finally/成功路径必须复位 _reconnecting=False，否则守护 job 永远让出无法自愈。
    从 _reconnecting=False 初态进入（让 _reconnect 自己置位再在 finally 复位），
    避开开头互斥让出分支（那条路径本就不该清他人锁）。
    """
    from unittest.mock import AsyncMock
    gw = _gw(str(tmp_path))
    gw._reconnecting = False  # 初态：让 _reconnect 自己置位
    monkeypatch_connect = AsyncMock(return_value=True)
    # 直接 patch 对象方法（避免触发 xtquant import）
    import broker.qmt as qmt_mod
    orig = qmt_mod.QmtExecutionGateway.connect
    qmt_mod.QmtExecutionGateway.connect = monkeypatch_connect  # type: ignore
    try:
        await gw._reconnect()  # type: ignore[func-returns-value]
    finally:
        qmt_mod.QmtExecutionGateway.connect = orig  # type: ignore
    # 核心断言：成功后标志复位（守护 job 下轮才能进入重连分支）
    assert gw._reconnecting is False


# ============================================================ #6：风控熔断粘滞（_risk_halted）
def test_risk_halt_not_cleared_by_health_guard_reconnect(monkeypatch, tmp_path):
    """risk_halt 置位后，health_guard 重连成功也不解锁（风控粘滞，#6）。"""
    from unittest.mock import patch
    from trading.engine import TradingEngine

    gw = _gw(str(tmp_path))
    gw.set_risk_halt(True)
    assert gw._risk_halted is True and gw._lock_down is True
    gw._connected = False
    monkeypatch.setattr(gw, "is_client_ready", lambda **kw: True)
    monkeypatch.setattr(gw, "connect", AsyncMock())
    eng = TradingEngine()
    with patch("trading.engine.get_gateway", return_value=gw):
        import asyncio
        asyncio.run(eng._health_guard())
    gw.connect.assert_not_awaited()  # risk_halt 期间不得自动重连
    assert gw._risk_halted is True, "risk_halt 必须粘滞，health_guard 不得自动解除"
    assert gw._lock_down is True, "risk_halt 期间 lock_down 不得被重连清掉"


def test_account_status_ok_does_not_clear_risk_halt(tmp_path):
    """账号状态 OK 推送不得清 risk_halt 的锁（#6 补强：_on_account_status_change 同闸）。"""
    gw = _gw(str(tmp_path))
    gw.set_risk_halt(True)
    gw._on_account_status_change(0)  # ACCOUNT_STATUS_OK
    assert gw._lock_down is True, "risk_halt 期间账号 OK 不得清 lock_down"
    assert gw._risk_halted is True
    # 非 risk_halt 时账号 OK 仍可清锁（网络断线自愈路径不受影响）
    gw.clear_risk_halt()
    gw._on_account_status_change(0)
    assert gw._lock_down is False
