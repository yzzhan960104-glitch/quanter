# -*- coding: utf-8 -*-
"""caisen.replay_scheduler 异步回测调度器 daemon 线程（Spec 1 · Task 4）。

物理定位：uvicorn 进程内的 daemon 线程，串行调度（concurrency=1）：
- 启动时 reset_running_to_failed（重启恢复，spec §3.3）；
- 周期 poll PENDING → claim_next_pending → submit run_replay_worker（注册 abort_flag）；
- 监控 last_heartbeat 超时 → mark_failed（worker 崩溃，不重跑，spec §7）；
- request_cancel(task_id)：set abort_flag → worker 循环顶命中 → CANCELLED。

并发模型：主进程单点写 SQLite（worker 经 Queue 上报进度，_consume_queues 落库），
杜绝跨进程 SQLite 写锁。abort_flag 用 multiprocessing.Event，主进程 set / 子进程读。
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import threading
from datetime import datetime

from caisen import replay_tasks_db

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0          # poll + sweep 间隔（秒）
_HEARTBEAT_TIMEOUT = 300      # heartbeat 超时（秒）→ 标 FAILED（worker 疑似崩溃）


class ReplayScheduler:
    """异步回测调度器：daemon 线程 poll PENDING → submit worker + 监控 heartbeat。

    参数：
        pool：ProcessPoolExecutor（concurrency=1）。测试可注入 _FakePool。
        abort_flags：task_id → mp.Event 的 dict（外部共享引用，cancel 端点据此 set）。
        db_path：SQLite 任务表路径（显式传，避免依赖模块全局）。
        run_replay_worker：测试注入 no-op；None 时生产从 caisen.replay_worker import。
        clock：时钟注入（heartbeat 超时测试用），默认 datetime.now。
    """

    def __init__(self, pool, abort_flags: dict, db_path: str,
                 run_replay_worker=None, clock=datetime.now):
        self._pool = pool
        self.abort_flags = abort_flags       # task_id → mp.Event（cancel 端点 set）
        self._db_path = db_path
        self._run_replay_worker = run_replay_worker   # None=生产 import；测试注入
        self._clock = clock
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._manager = None     # multiprocessing.Manager（start 时起；Event/Queue 须用其 proxy）

    def start(self):
        """启动 daemon 调度线程（先做重启恢复 + 起 Manager）。幂等：重复 start 会被 _stop 状态挡住。"""
        # 起 Manager：生产 Event/Queue 必须经它创建——mp.Event/Queue 是 Condition，不能作
        # ProcessPoolExecutor.submit 参数（pickle 抛 RuntimeError），Manager proxy 可 pickle。
        if self._manager is None:
            self._manager = mp.Manager()
        self._reset_on_startup()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="replay-scheduler")
        self._thread.start()

    def stop(self):
        """请求停止（set event，daemon 线程下次循环退出；不 join——不阻塞调用方）。"""
        self._stop.set()
        if self._manager is not None:
            try:
                self._manager.shutdown()
            except Exception:
                logger.warning("Manager shutdown 异常（已忽略）", exc_info=True)
            self._manager = None

    def request_cancel(self, task_id: str):
        """cancel 端点调：set abort_flag（worker 循环顶命中即 CANCELLED）。

        未注册 abort_flag（任务未 RUNNING 或已结束）→ 静默 no-op（cancel 已完成的任务无害）。
        """
        flag = self.abort_flags.get(task_id)
        if flag is not None:
            flag.set()

    # ------------------------------------------------------------------ 内部
    def _reset_on_startup(self):
        """重启恢复：残留 RUNNING 标 FAILED（spec §3.3，不自动重跑——用户决定重提）。"""
        n = replay_tasks_db.reset_running_to_failed(self._db_path)
        if n:
            logger.warning("启动恢复：%d 个残留 RUNNING 任务标 FAILED", n)

    def _loop(self):
        """daemon 主循环：周期 poll PENDING + sweep 超时 RUNNING，异常不中断。"""
        while not self._stop.is_set():
            try:
                self._poll_once()
                self._sweep_stale()
            except Exception:
                # 调度循环任何异常都捕获（不让 daemon 线程死掉，否则任务永远不被派发）。
                logger.exception("调度器循环异常（吞掉继续，避免 daemon 挂掉）")
            self._stop.wait(_POLL_INTERVAL)

    def _poll_once(self):
        """领取一个 PENDING → submit worker（concurrency=1：pool 满则 submit 阻塞排队）。"""
        run_worker = self._run_replay_worker or self._import_worker()
        task = replay_tasks_db.claim_next_pending(self._db_path)
        if task is None:
            return
        task_id = task["task_id"]
        abort_flag = self._make_event()
        self.abort_flags[task_id] = abort_flag
        progress_q = self._make_queue()
        heartbeat_q = self._make_queue()
        # submit 到 pool（concurrency=1 串行；pool 满时 submit 内部排队，不丢任务）。
        self._pool.submit(run_worker, task_id, abort_flag, progress_q, heartbeat_q)
        # 队列消费线程：worker 上报的 progress 落库（主进程单点写 DB）。
        threading.Thread(target=self._consume_queues, args=(task_id, progress_q),
                         daemon=True).start()

    def _consume_queues(self, task_id: str, progress_q):
        """消费 worker progress 上报 → 落库（主进程单点写 DB）。

        退出条件：task 进入终态（SUCCESS/FAILED/CANCELLED）或 scheduler stop。
        不用「queue 空即 break」——全市场回测两次 progress 上报间隙（每 50 symbol）可达
        数分钟，queue 暂空就退出会漏消费后续上报、进度卡死。改为查 task 终态才退出。
        """
        while not self._stop.is_set():
            t = replay_tasks_db.get_task(task_id, path=self._db_path)
            if t is None or t["status"] not in ("RUNNING", "PENDING"):
                return   # task 已终态（worker 完成/失败/被取消/被 sweep 标 FAILED）
            try:
                _tid, done, total = progress_q.get(timeout=_POLL_INTERVAL)
            except Exception:
                continue   # queue 暂空（worker 还没到下个上报点），继续等
            pct = int(done * 100 / total) if total else 0
            replay_tasks_db.update_progress(task_id, pct, path=self._db_path)
            replay_tasks_db.update_heartbeat(task_id, path=self._db_path)

    def _sweep_stale(self):
        """扫 RUNNING 任务：last_heartbeat 超时 → mark_failed（worker 疑似崩溃，不重跑）。

        物理意图（spec §7）：worker 子进程崩溃/被 OOM 杀 → 无人 mark 终态 → task 永卡 RUNNING。
        sweep 周期检查 last_heartbeat，超 _HEARTBEAT_TIMEOUT 即标 FAILED + 清 abort_flag。
        """
        now = self._clock()
        for t in replay_tasks_db.list_tasks(status="RUNNING", path=self._db_path):
            hb = t.get("last_heartbeat")
            if not hb:
                continue
            try:
                hb_dt = datetime.fromisoformat(hb)
            except Exception:
                continue   # heartbeat 字段损坏，跳过（不误杀）
            age = (now - hb_dt).total_seconds()
            if age > _HEARTBEAT_TIMEOUT:
                replay_tasks_db.mark_failed(
                    t["task_id"],
                    f"worker heartbeat 超时（{int(age)}s 无更新，疑似崩溃，不自动重跑）",
                    path=self._db_path,
                )
                self.abort_flags.pop(t["task_id"], None)

    def _make_event(self):
        """创建 abort flag。

        生产（Manager 已起）：返 manager.Event()——proxy 可经 ProcessPoolExecutor.submit
        pickle 传给子进程（mp.Event 是 Condition 不能 pickle，submit 会抛 RuntimeError）。
        测试（Manager 未起，_FakePool 不真 spawn）：fallback mp.Event()，类型无关。
        """
        if self._manager is not None:
            return self._manager.Event()
        return mp.Event()

    def _make_queue(self):
        """创建 progress/heartbeat Queue（同 _make_event：生产用 Manager proxy 可 pickle）。"""
        if self._manager is not None:
            return self._manager.Queue()
        return mp.Queue()

    @staticmethod
    def _import_worker():
        """生产路径：惰性 import run_replay_worker（避免模块加载期循环依赖）。"""
        from caisen.replay_worker import run_replay_worker
        return run_replay_worker
