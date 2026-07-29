# -*- coding: utf-8 -*-
"""M1 网关健康守卫单测：就绪探测 + 重连互斥 + 守护 job。"""
import os, time, pytest

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
