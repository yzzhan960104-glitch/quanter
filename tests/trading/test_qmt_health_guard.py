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
