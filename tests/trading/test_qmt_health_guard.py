# -*- coding: utf-8 -*-
"""M1 网关健康守卫单测：就绪探测 + 重连互斥 + 守护 job。"""
import os, time, pytest
from unittest.mock import AsyncMock

def _gw(userdata):
    from broker.qmt import QmtExecutionGateway
    return QmtExecutionGateway(userdata_path=userdata, account_id="10110356", session_id=777888)

# ============================================================ W1.1（2026-08-04 根治）
# is_client_ready 重定义：connect 返回码 = 客户端可用性唯一权威。
#   userdata 目录在 → True（放行让上层 connect，由返回码定权威结论）；
#   目录缺失/空 → False（connect 必失败的唯一该挡场景）。
#   mtime 判据降级为 _client_staleness_diag 的日志分类素材，绝不硬前置。
# 详见 broker/qmt.py is_client_ready docstring（08-04 事故根因）。
def test_is_client_ready_false_when_dir_missing():
    """W1.1：userdata 目录不存在 = 客户端必然未起 → False（connect 必失败的唯一该挡场景）。"""
    gw = _gw("/nonexistent/path/xyz")
    assert gw.is_client_ready() is False
    # 诊断函数同步描述「不存在/缺失」供 health_guard WARNING 文案用
    diag = gw._client_staleness_diag()
    assert "不存在" in diag or "缺失" in diag


def test_is_client_ready_true_when_userdata_dir_exists_even_if_stale(tmp_path):
    """W1.1 核心：userdata 目录存在即视客户端进程在 → ready，mtime 陈旧不再硬前置。

    物理：connect 返回码才是权威；文件 mtime 只做日志分类，防 08-04 静默跳过复发。
    故意只放一个老旧缓存文件（旧逻辑会判 stale=False，新逻辑判 True）。"""
    userdata = tmp_path / "userdata_mini"
    userdata.mkdir()
    (userdata / "miniqmtShmCache_old").write_text("x")
    old = time.time() - 3600  # 1 小时前
    os.utime(userdata / "miniqmtShmCache_old", (old, old))

    gw = _gw(str(userdata))
    assert gw.is_client_ready() is True  # 目录存在 → 进程可能在 → 放行 connect
    # 诊断函数能描述「陈旧」供告警用（文案稳定可断言，是 T2 WARNING 的来源）
    diag = gw._client_staleness_diag()
    assert "陈旧" in diag or "正常" in diag


def test_is_client_ready_false_when_userdata_dir_empty(tmp_path):
    """W1.1：目录存在但完全空（刚创建未登录）→ False。

    物理：空目录意味着客户端安装路径被建出但从未登录过，connect 必失败（无会话上下文），
    与「目录缺失」等价挡掉，避免放行后空跑 connect 撞柜台。"""
    userdata = tmp_path / "userdata_empty"
    userdata.mkdir()
    gw = _gw(str(userdata))
    assert gw.is_client_ready() is False
    assert "目录空" in gw._client_staleness_diag()


def test_is_client_ready_true_when_only_engine_down_queue_present(tmp_path):
    """W1.1 语义反转：只有引擎自建 down_queue_win_{sid}（无客户端文件）→ 目录非空 → True。

    Why 反转：旧逻辑把 down_queue 当「自证自」隐患排除（P0-2），但 W1.1 后 connect 返回码
    才是权威，文件 mtime/类型不再做硬前置——只要目录非空就放行让 connect 自己说话。
    引擎自建 down_queue 的「自证自」风险由 connect 的 stop-before-recreate + -1 自愈兜底。"""
    (tmp_path / "down_queue_win_777888").write_bytes(b"x")  # 仅引擎文件，无客户端文件
    gw = _gw(str(tmp_path))
    assert gw.is_client_ready(staleness_sec=300) is True  # 目录非空 → 放行


# ============================================================ _client_staleness_diag 四态覆盖
# 诊断函数是 T2 _health_guard WARNING 文案的来源，文案需稳定可断言。
# 四态：目录缺失 / 目录空 / 无活跃文件（仅目录存在） / 陈旧 N 分钟 / 正常。
def test_staleness_diag_dir_missing(tmp_path):
    """诊断态①：userdata 目录不存在 → 文案含「不存在/缺失」。"""
    gw = _gw(str(tmp_path / "no_such_dir"))
    diag = gw._client_staleness_diag()
    assert "不存在" in diag or "缺失" in diag


def test_staleness_diag_dir_empty(tmp_path):
    """诊断态②：目录存在但空 → 文案含「目录空」。"""
    userdata = tmp_path / "empty_mini"
    userdata.mkdir()
    gw = _gw(str(userdata))
    assert "目录空" in gw._client_staleness_diag()


def test_staleness_diag_no_active_files(tmp_path):
    """诊断态③：目录非空但无 miniqmtShm*/up_queue*/quoter 活跃文件
    （只有 down_queue 等引擎文件）→ 文案含「无活跃文件」。

    物理：客户端可能未登录或仅引擎跑过，目录在但缺客户端心跳文件。"""
    (tmp_path / "down_queue_win_777888").write_bytes(b"x")  # 引擎文件，不在活跃 patterns 内
    gw = _gw(str(tmp_path))
    assert "无活跃文件" in gw._client_staleness_diag()


def test_staleness_diag_stale_minutes(tmp_path):
    """诊断态④：活跃文件存在但 mtime 陈旧（>staleness_sec）→ 文案含「陈旧 N 分钟」。"""
    (tmp_path / "miniqmtShmStockListCacheSZO").write_text("x")
    old = time.time() - 3600  # 1 小时前 = 60 分钟陈旧
    os.utime(tmp_path / "miniqmtShmStockListCacheSZO", (old, old))
    gw = _gw(str(tmp_path))
    diag = gw._client_staleness_diag(staleness_sec=300)
    assert "陈旧" in diag and "分钟" in diag


def test_staleness_diag_fresh(tmp_path):
    """诊断态⑤：活跃文件新鲜（mtime 在 staleness_sec 内）→ 文案含「正常」。"""
    (tmp_path / "up_queue_win_xtquant").write_bytes(b"x")  # 刚创建=新鲜
    gw = _gw(str(tmp_path))
    assert "正常" in gw._client_staleness_diag()


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
#
# W1-A/T2-Task10 C3 判定（engine_vs_phases）：本组全部 patch **保 trading.engine.X 不迁**。
# Why：_health_guard 是 TradingEngine 实例方法（engine.py:885），其内读 get_gateway()/
# _mode()/_alert_critical() 均经 **engine 模块全局名** 解析（Python LEGB 模块作用域）：
#   - get_gateway（engine.py:367 engine 自有模块级定义 · 非 phases 路径）
#   - _mode / _alert_critical（engine.py:92-98 `from trading.critical import` re-export ·
#     re-export 后仍是 engine 模块属性 · engine 内部调用点经模块全局名读）
# 故 patch("trading.engine.get_gateway") / setattr(eng_mod, "_mode"/"_alert_critical")
# 替换的是 engine 模块属性 → _health_guard 内经模块全局名读到 mock → 命中。
# phases 路径（phases.pre_open/stop_loss/post_close）不经 _health_guard → 无需迁 phases。
# 经验证：本组 25 个 health_guard engine 路径测试全绿（patch engine.X 命中），C3 决策成立。
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
async def test_health_guard_probe_zombie_sets_connected_false(monkeypatch):
    """T9: _connected=True 但探针连续 N 次失败 → 判僵死，置 _connected=False 走重连。

    物理意图：on_disconnected 不触发的盲区（客户端重启中/假死，socket 看似连着）。
    主动探针 query_account_status 连续 N 次失败 → 不再 no-op 放任废单，置 _connected=False
    强制下轮走 ④/⑥ 重连。N 由 env T9_PROBE_FAIL_THRESHOLD 配（默认 3）。
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from trading.engine import TradingEngine
    monkeypatch.setenv("T9_PROBE_FAIL_THRESHOLD", "3")
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw._risk_halted = False
    gw._reconnecting = False
    gw.probe_account_status = AsyncMock(return_value=(False, "探针超时"))
    gw.connect = AsyncMock()  # spy：判僵死本轮只置标志，不应调 connect
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()  # fail 1/3
        assert gw._connected is True, "1 次失败不应判僵死"
        await eng._health_guard()  # fail 2/3
        assert gw._connected is True, "2 次失败不应判僵死"
        await eng._health_guard()  # fail 3/3 → 判僵死
    assert gw._connected is False, "连续 3 次失败应置 _connected=False 走重连"
    gw.connect.assert_not_awaited()  # 判僵死本轮只置标志，下轮才重连


@pytest.mark.asyncio
async def test_health_guard_probe_success_keeps_connected():
    """T9: _connected=True 且探针成功 → 连接真活着，保持 _connected=True（no-op，清探针计数）。"""
    from unittest.mock import AsyncMock, MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._connected = True
    gw._risk_halted = False
    gw._reconnecting = False
    gw.probe_account_status = AsyncMock(return_value=(True, "rc=0"))
    gw.connect = AsyncMock()  # spy：探针成功不应重连
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    assert gw._connected is True
    gw.connect.assert_not_awaited()


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
    cp.assert_awaited_once_with(ports=eng._ports)  # T1：health_guard 补挂透传 engine._ports


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


@pytest.mark.asyncio
async def test_health_guard_resets_backoff_when_client_becomes_ready():
    """客户端从不可用→可用：清零退避并立即重连（不被旧失败计数拖慢，P0-3）。"""
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    eng._guard_fail_count = 5          # 历史失败已把退避推到 7 轮
    eng._guard_rounds_since_fail = 0
    gw = MagicMock()
    gw._risk_halted = False
    gw._connected = False
    gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=False)
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_not_awaited()            # 未就绪不空跑
    assert eng._guard_fail_count == 5
    gw.is_client_ready.return_value = True     # 客户端就绪（False→True 跃迁）
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    gw.connect.assert_awaited_once()           # 跃迁后立即重连，不再等 7 轮退避
    assert eng._guard_fail_count == 0


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


# ============================================================ W1.2（08-04 静默断线根治）
# _health_guard ④ 未就绪分支旧版静默 return（无日志无告警）→ 网关断线 9 小时无人知，
# 直到 pre_open 失败才暴露。本组补可见性：WARNING + 诊断文案 + 每 10 轮节流钉钉。
def _make_engine_with_not_ready_gw(monkeypatch, *, diag_text="userdata 目录不存在（客户端未安装/路径错）",
                                   mode="live"):
    """构造 engine + 一个未就绪 gw 的共用 helper（复用既有 TradingEngine() 范式）。

    返回 (eng, gw, fired)。fired 收集 _alert_critical 收到的告警正文（节流断言用）。

    Why mode 参数（Fix1 · 用户两轴 review）：_alert_critical 加了 `if _mode()=="live"` 守卫，
        dry_run 模式不推钉钉（防开发/测试环境误推运营群）。既有 5 个测试断「推了几次」
        必须显式 patch _mode=live 才能命中守卫；默认 live 与既有断言一致（行为零变化）。
    """
    from unittest.mock import MagicMock, patch
    import trading.engine as eng_mod
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False
    gw._connected = False
    gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=False)
    gw._client_staleness_diag = MagicMock(return_value=diag_text)
    # Fix1 关键：_alert_critical 已加 live 守卫，必须 patch _mode 才能命中告警分支
    # W1-A/T2-Task10 C3：setattr(eng_mod, "_mode"/"_alert_critical") **保 engine 不迁**——
    # _health_guard 内 _mode()/_alert_critical() 经 engine 模块全局名读（engine.py:92-98
    # from trading.critical import re-export · re-export 后是 engine 模块属性），patch engine
    # 模块属性即命中；critical._mode/_alert_critical 是物理真身但 _health_guard 不经 critical
    # 模块全局名读 → 迁 critical 反而 miss。与上方 health_guard 组 C3 决策一致。
    monkeypatch.setattr(eng_mod, "_mode", lambda: mode)
    fired = []
    monkeypatch.setattr(eng_mod, "_alert_critical", lambda msg: fired.append(msg))
    return eng, gw, fired


@pytest.mark.asyncio
async def test_health_guard_not_ready_warns_with_diag(monkeypatch, caplog):
    """W1.2：客户端未就绪 → WARNING 日志带诊断文案（不再静默 return）。"""
    import logging
    from unittest.mock import patch
    eng, gw, _ = _make_engine_with_not_ready_gw(monkeypatch)
    caplog.set_level(logging.WARNING, logger="trading.engine")
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "客户端未就绪" in r.getMessage()]
    assert len(warns) >= 1, "未就绪分支必须打 WARNING"
    assert "目录不存在" in warns[0].getMessage() or "客户端未安装" in warns[0].getMessage()
    # 首轮即推钉钉（I-1 收口 · 断线立刻可见：_not_ready_rounds==1 立即首推）
    assert eng._not_ready_rounds == 1


@pytest.mark.asyncio
async def test_health_guard_not_ready_alert_fires_on_first_round(monkeypatch):
    """I-1：首次未就绪（第 1 轮）立即推钉钉——断线立刻可见，不延迟到第 10 轮。

    物理意图：盘中 09:22 pre_open 前断线时，旧版节流要等到第 10 轮（≈10min 后）
    才首推，pre_open 已被 gate 静默跳过。新口径 _not_ready_rounds==1 即推一条，
    让操作员在 pre_open 窗口关闭前有机会介入。
    """
    from unittest.mock import patch
    eng, gw, fired = _make_engine_with_not_ready_gw(monkeypatch)
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()  # 仅 1 轮
    assert len(fired) == 1, f"首轮应立即推 1 次钉钉（断线立刻可见），实际 {len(fired)}"
    assert "目录不存在" in fired[0] or "客户端未安装" in fired[0]
    assert "1 轮" in fired[0]
    assert eng._not_ready_rounds == 1


@pytest.mark.asyncio
async def test_health_guard_not_ready_throttles_alert_every_10_rounds(monkeypatch):
    """I-1：连续 12 轮未就绪 → 第 1 轮首推 + 第 10 轮再推（首轮即推 + 后续 10 轮节流）。"""
    from unittest.mock import patch
    eng, gw, fired = _make_engine_with_not_ready_gw(monkeypatch)
    with patch("trading.engine.get_gateway", return_value=gw):
        for _ in range(12):
            await eng._health_guard()
    # 12 轮推 2 次（第 1 轮首推 + 第 10 轮节流推），第 11/12 轮不推
    assert len(fired) == 2, f"12 轮应推 2 次钉钉（首推 + 第 10 轮），实际 {len(fired)}"
    assert "目录不存在" in fired[0] or "客户端未安装" in fired[0]
    assert eng._not_ready_rounds == 12


@pytest.mark.asyncio
async def test_health_guard_not_ready_alert_at_round_20(monkeypatch):
    """I-1：第 20 轮共推 3 次（第 1 轮首推 + 第 10 + 第 20 节流推）。"""
    from unittest.mock import patch
    eng, gw, fired = _make_engine_with_not_ready_gw(monkeypatch)
    with patch("trading.engine.get_gateway", return_value=gw):
        for _ in range(20):
            await eng._health_guard()
    assert len(fired) == 3, f"20 轮应推 3 次（第 1 + 第 10 + 第 20），实际 {len(fired)}"


@pytest.mark.asyncio
async def test_health_guard_resets_not_ready_rounds_when_ready(monkeypatch):
    """W1.2：客户端恢复就绪后 _not_ready_rounds 必须清零（避免下次断线首推延迟）。

    物理意图：清零防计数漂移——若上次断线累计 9 轮不清零，下次新断线第 1 轮即
    9+1=10 触发告警，语义错位（应是连续 10 轮才告警，而非历史遗留 + 1）。
    """
    from unittest.mock import patch
    eng, gw, _ = _make_engine_with_not_ready_gw(monkeypatch)
    with patch("trading.engine.get_gateway", return_value=gw):
        # 先 5 轮未就绪
        for _ in range(5):
            await eng._health_guard()
    assert eng._not_ready_rounds == 5
    # 客户端恢复就绪
    gw.is_client_ready.return_value = True
    gw._connected = False  # 仍断线，触发重连
    gw.connect = AsyncMock()
    with patch("trading.engine.get_gateway", return_value=gw):
        await eng._health_guard()
    # 就绪后清零
    assert eng._not_ready_rounds == 0, "就绪后未就绪计数必须清零"


@pytest.mark.asyncio
async def test_health_guard_not_ready_does_not_block_on_alert_failure(monkeypatch):
    """W1.2：_alert_critical 内部失败不阻塞 _health_guard 主链路（C-4 错误分级：告警软降级）。

    物理意图：告警系统绝不能成为交易主链路的单点故障源。_alert_critical 内部
    fire_and_forget / notifier import 失败时由其自身 try/except 兜底（engine.py
    _alert_critical 函数体 except），守护 job 主路径不被拖垮。本测试构造 notifier
    import 失败场景（_alert_critical 内部 try 块首行就抛），验证守护仍照常累加计数。
    """
    import trading.engine as eng_mod
    from unittest.mock import MagicMock, patch
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False; gw._connected = False; gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=False)
    gw._client_staleness_diag = MagicMock(return_value="无活跃文件")
    # 让 _alert_critical 内部 `from infra.notifier import ...` 抛异常
    # （真实告警通道最常见的失败模式：notifier 模块/import 链断）。
    import builtins
    real_import = builtins.__import__
    def _block_notifier(name, *a, **kw):
        if name == "infra.notifier":
            raise ImportError("模拟 notifier 模块不可用")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", _block_notifier)
    with patch("trading.engine.get_gateway", return_value=gw):
        for _ in range(10):
            await eng._health_guard()  # 第 10 轮触发 _alert_critical，内部 import 失败被兜底
    assert eng._not_ready_rounds == 10  # 计数仍累加（主链路未被异常打断）


# ============================================================ Fix1（用户两轴 review · 告警模式闸）
# _alert_critical 全部调用点加 `if _mode()=="live"` 守卫（既有 7 处 + 新增 6 处 = 13 处）。
# 物理意图：dry_run 模式无真金风险，开发/测试环境误推钉钉到生产运营群是噪音污染，
# 长期会致研究员对告警麻木（真断线时忽略）。守卫与既有 7 处范式一致（pre_open gate 等）。
# 唯一例外：_halt 致命停调度保留无条件（L1 致命 = 真金风险红线，dry_run 永不触发 _halted）。
@pytest.mark.asyncio
async def test_health_guard_not_ready_no_alert_in_dry_run(monkeypatch):
    """Fix1：dry_run 模式下 _health_guard 未就绪 → _alert_critical 不应被调用（防误推钉钉）。

    物理意图：dry_run 是影子/开发模式，无真金风险；客户端未就绪是环境问题（miniQMT 未起），
    推 CRITICAL 到生产运营钉钉群只会污染通道，致研究员对真告警麻木。live 模式才该推。
    """
    from unittest.mock import MagicMock, patch
    import trading.engine as eng_mod
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False; gw._connected = False; gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=False)
    gw._client_staleness_diag = MagicMock(return_value="userdata 目录不存在（客户端未安装/路径错）")
    # Fix1 核心：dry_run 模式 + _alert_critical 被 spy
    monkeypatch.setattr(eng_mod, "_mode", lambda: "dry_run")
    call_count = {"n": 0}
    monkeypatch.setattr(eng_mod, "_alert_critical", lambda msg: call_count.__setitem__("n", call_count["n"] + 1))
    with patch("trading.engine.get_gateway", return_value=gw):
        # 连续 12 轮（live 模式下应推 2 次：第 1 + 第 10；dry_run 必须推 0 次）
        for _ in range(12):
            await eng._health_guard()
    assert call_count["n"] == 0, f"dry_run 模式不应推钉钉（防误推运营群），实际推了 {call_count['n']} 次"
    assert eng._not_ready_rounds == 12  # 守卫不影响计数（日志/计数照常，只是不推钉钉）


@pytest.mark.asyncio
async def test_health_guard_not_ready_alerts_in_live_mode(monkeypatch):
    """Fix1 对照：live 模式下 _health_guard 未就绪 → _alert_critical 照常被调用（守卫不误伤 live）。

    Why 对照测试：防守卫逻辑写反（if _mode()=="dry_run" 才推），live 漏推比 dry_run 误推严重 100 倍
    （真金断线漏告警 = 08-04 事故重演）。本测试与上一测试共同锁定「dry_run 0 次 / live N 次」语义。
    """
    from unittest.mock import MagicMock, patch
    import trading.engine as eng_mod
    from trading.engine import TradingEngine
    eng = TradingEngine()
    gw = MagicMock()
    gw._risk_halted = False; gw._connected = False; gw._reconnecting = False
    gw.is_client_ready = MagicMock(return_value=False)
    gw._client_staleness_diag = MagicMock(return_value="userdata 目录不存在（客户端未安装/路径错）")
    monkeypatch.setattr(eng_mod, "_mode", lambda: "live")
    fired = []
    monkeypatch.setattr(eng_mod, "_alert_critical", lambda msg: fired.append(msg))
    with patch("trading.engine.get_gateway", return_value=gw):
        for _ in range(12):
            await eng._health_guard()
    assert len(fired) == 2, f"live 模式 12 轮应推 2 次（第 1 + 第 10），实际 {len(fired)}"


# ============================================================ Fix3（用户两轴 review · quoter 文件级诊断）
def test_staleness_diag_quoter_inner_files_fresh(tmp_path):
    """Fix3：quoter 目录下文件新鲜（mtime=now）→ 诊断应返「正常」（不报陈旧）。

    物理意图（Windows mtime 失效）：原 patterns 含 "quoter" 匹配目录本身，Windows 目录
    mtime 只在内部文件增删时刷新（行情主推覆盖写已有文件不动目录 mtime）→ 一天只变一次 →
    客户端正常收行情时仍误报陈旧。修复加 "quoter/*" glob 到文件级，行情刷新文件 mtime 即更新。

    本测试复现 Windows 真实场景：
      1. 开盘前创建 quoter/SH/600000.tick（mtime=now，目录 mtime 也 now）；
      2. 等效开盘后：把 quoter/SH/600000.tick mtime 设为 now（行情主推刷新），但
         手动把 quoter 目录本身 mtime 设为 1 小时前（模拟 Windows 不随内部文件内容变）。
      旧逻辑只 glob 到目录 mtime（1 小时前 = 陈旧）→ 误报陈旧；
      新逻辑 glob quoter/* 文件 mtime（now = 新鲜）→ 正确判正常。
    """
    import time as _t, os as _os
    # 构造 quoter 目录 + 内部文件（模拟开盘前建立）
    quoter_dir = tmp_path / "quoter"
    quoter_sh = quoter_dir / "SH"
    quoter_sh.mkdir(parents=True)
    tick_file = quoter_sh / "600000.tick"
    tick_file.write_bytes(b"x")
    # 模拟 Windows 目录 mtime 失效：把 quoter 目录 mtime 设为 1 小时前（陈旧），
    # 但 quoter/SH/600000.tick 文件 mtime 设为 now（新鲜，模拟盘中行情主推刷新文件内容）。
    # Windows 下目录 mtime 不随内部文件内容覆盖而变，只有创建/删除子项才变。
    one_hour_ago = _t.time() - 3600
    _os.utime(quoter_dir, (one_hour_ago, one_hour_ago))   # 目录 mtime 陈旧（Windows 真实行为）
    _os.utime(tick_file, (_t.time(), _t.time()))          # 文件 mtime 新鲜（行情主推刷新）
    gw = _gw(str(tmp_path))
    diag = gw._client_staleness_diag(staleness_sec=300)
    assert "正常" in diag, f"quoter 内部文件新鲜应判正常（Windows mtime 修复），实际：{diag}"
