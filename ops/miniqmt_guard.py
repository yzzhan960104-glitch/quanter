# -*- coding: utf-8 -*-
"""miniQMT 客户端看门狗（B2-1/B2-2 · process-gateway-ssot-final spec §4.3）。

物理意图：miniQMT 客户端（XtMiniQmt.exe）是引擎连接的前提——进程不在 → 自动拉起；
进程在但未登录/文件陈旧 → 钉钉 WARN（不假装活、不误杀）；残留 session 队列 → 兜底清理。
独立 5min 任务（QuanterMiniQmtGuard），与引擎生命周期解耦（引擎崩了它还在）。

裁定（2026-08-06 用户采纳）：G1=客户端 exe 用 env QMT_CLIENT_EXE，缺省不猜路径（宁可不
拉起也不拉起错的程序）；G2=登录就绪判据 = userdata 非空 + down_queue_win_* 存在 +
最新相关文件 mtime ≤ 5min；G3=独立 5min schtasks；G4=自动登录由人工在客户端勾一次。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# 读 .env 拿 QMT_USERDATA_PATH/QMT_SESSION_ID（schtasks 环境无 .env，必须自加载）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

from ops import process_topology

STALE_SEC = 5 * 60  # G2：mtime 超过 5 分钟视为陈旧


def _userdata() -> str:
    return os.environ.get("QMT_USERDATA_PATH", "")


def _client_exe() -> str | None:
    """G1：QMT_CLIENT_EXE 显式指定；缺省 None（不猜路径，宁可不拉起）。"""
    return os.environ.get("QMT_CLIENT_EXE") or None


def _newest_mtime(userdata: str) -> float:
    """userdata 下 session 相关文件的最新 mtime（0=无相关文件）。"""
    newest = 0.0
    for pat in ("down_queue_win_*", "lock_*queue_win_*"):
        for f in glob.glob(os.path.join(userdata, pat)):
            try:
                newest = max(newest, os.path.getmtime(f))
            except OSError:
                continue
    return newest


def check_client() -> dict:
    """客户端健康度：running/healthy/stale/missing + 文案。

    G2 判据：进程在 + userdata 非空 + 有 down_queue 文件 + 最新 mtime ≤ 5min → healthy；
    进程在但文件缺失/陈旧 → stale（只 WARN 不杀）；进程不在 → missing。
    """
    st = process_topology.client_status()
    userdata = _userdata()
    if st.get("running") is None:
        return {"status": "unknown", "detail": "客户端进程探测失败", "pid": None}
    if not st["running"]:
        return {"status": "missing", "detail": "XtMiniQmt 进程不在", "pid": None}
    if not userdata or not os.path.isdir(userdata):
        return {"status": "stale", "detail": "进程在但 userdata 目录缺失/为空（未登录）",
                "pid": st.get("pid")}
    try:
        if not any(os.scandir(userdata)):
            return {"status": "stale", "detail": "进程在但 userdata 目录空（未登录）",
                    "pid": st.get("pid")}
    except OSError:
        return {"status": "stale", "detail": "进程在但 userdata 不可读", "pid": st.get("pid")}
    newest = _newest_mtime(userdata)
    if newest == 0.0:
        return {"status": "stale", "detail": "进程在但无 down_queue 会话文件（未登录）",
                "pid": st.get("pid")}
    age = int(time.time() - newest)
    if age > STALE_SEC:
        # 引擎已连接（端口/锁存在）时，文件 mtime 不是假活判据——connect 返回码才是
        # 权威（W1.1）；health_guard 管重连。仅无引擎时才按陈旧报「假活」（防盘前/
        # 收盘 5 分钟 WARN 风暴——08-06 实测引擎连接中文件仍可能 >5min 未动）。
        if process_topology.port_holder_pid(8000) is not None:
            return {"status": "healthy",
                    "detail": f"引擎已连接，客户端可用（文件 mtime {age}s 非活跃期参考）",
                    "pid": st.get("pid")}
        return {"status": "stale",
                "detail": f"进程在但会话文件陈旧 {age}s（>5min，疑似未登录/假活）",
                "pid": st.get("pid")}
    return {"status": "healthy", "detail": f"客户端正常（文件新鲜 {age}s）", "pid": st.get("pid")}


def ensure_client(dry_run: bool = False) -> str:
    """进程不在 → 拉起 XtMiniQmt.exe（QMT_CLIENT_EXE）；缺路径或 dry_run 只告警。"""
    st = check_client()
    if st["status"] != "missing":
        return st["detail"]
    exe = _client_exe()
    if not exe:
        return "客户端不在且 QMT_CLIENT_EXE 未配置，拒绝猜测路径（宁可不拉起）"
    if dry_run:
        return f"[dry-run] 将拉起 {exe}"
    try:
        subprocess.Popen([exe], cwd=str(Path(exe).parent))
        return f"已拉起 {exe}"
    except Exception as e:
        return f"拉起客户端失败 {exe}: {e}"


def cleanup_stale_queues(dry_run: bool = False) -> list[str]:
    """兜底清理：非当前 sid 且 >1h 未动的残留队列（复用 qmt_clear_session_lock 语义）。"""
    from scripts.qmt_clear_session_lock import is_clearable, list_session_locks
    userdata = _userdata()
    current_sid = int(os.environ.get("QMT_SESSION_ID", "123456"))
    now = time.time()
    removed: list[str] = []
    for lock in list_session_locks(userdata):
        if is_clearable(lock, current_sid, now):
            if dry_run:
                removed.append(f"[dry-run] {lock['name']}")
            else:
                try:
                    os.remove(lock["path"])
                    removed.append(lock["name"])
                except OSError as e:
                    removed.append(f"{lock['name']} 删除失败: {e}")
    return removed


def ensure_engine(dry_run: bool = False) -> str | None:
    """B2 扩展：引擎失踪（8000 无监听）→ schtasks /Run 拉起（5min 自愈兜底）。

    物理意图：08-06 引擎多次被外部终止（无 traceback，12:28 复现），schtasks
    RestartOnFailure 又因权限未注册——guard 每 5 分钟顺带检查引擎，失踪即拉起，
    把「引擎死了没人救」的窗口压到 ≤5 分钟。维护期可用
    QUANTER_GUARD_DISABLE_ENGINE=1 显式关闭（人工重启流程中避免误拉起）。
    """
    if os.environ.get("QUANTER_GUARD_DISABLE_ENGINE") == "1":
        return "guard 引擎自愈已禁用（QUANTER_GUARD_DISABLE_ENGINE=1）"
    if process_topology.port_holder_pid(8000) is not None:
        return None  # 引擎在，不动作
    if dry_run:
        return "[dry-run] 引擎缺失，将 schtasks /Run QuanterServer"
    r = subprocess.run(["schtasks", "/Run", "/TN", "QuanterServer"],
                       capture_output=True, text=True, errors="replace", timeout=15)
    if r.returncode == 0:
        return "引擎缺失，已 schtasks /Run QuanterServer 拉起"
    return f"引擎缺失且拉起失败 rc={r.returncode}（{r.stdout.strip() or r.stderr.strip()}）"


def run_once(dry_run: bool = False, alert: bool = True) -> dict:
    """跑一轮：确保客户端 + 陈旧告警 + 队列兜底。"""
    st = check_client()
    launched = ensure_client(dry_run=dry_run) if st["status"] == "missing" else None
    cleaned = cleanup_stale_queues(dry_run=dry_run)
    engine = ensure_engine(dry_run=dry_run)
    if alert and st["status"] in ("missing", "stale") and not dry_run:
        try:
            from infra.notifier import NotificationManager, fire_and_forget
            fire_and_forget(NotificationManager.get_default().notify_risk_event(
                f"miniQMT guard: {st['detail']}", "WARN"))
        except Exception:
            pass
    return {"client": st, "launched": launched, "cleaned": cleaned, "engine": engine}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="miniQMT 客户端看门狗（B2）")
    p.add_argument("--once", action="store_true", help="跑一轮（默认）")
    p.add_argument("--dry-run", action="store_true", help="只展示不拉起/不清理")
    args = p.parse_args(argv)
    print(json.dumps(run_once(dry_run=args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
