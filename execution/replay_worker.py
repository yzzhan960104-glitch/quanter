# -*- coding: utf-8 -*-
"""caisen.replay_worker 异步回测 worker 进程入口（Spec 1 · Task 3）。

（待迁·Step4 移出 caisen 包至执行编排层）本模块当前物理位于 caisen/infra/ 过渡子包，
Step4 将连同 storage/execution/replay_*/viz_* 整体迁出 caisen 包至独立的执行编排层。
当前位置仅为 Step3 分层重构的中间态。

Step4e 反向债已收口：_load_price_data/_merge_cfg 改 import data.price_loader 模块级函数
（原 from server.services.caisen_service import 是 execution→server 反向依赖，Step2.2 过渡债）。

物理定位：被 ProcessPoolExecutor submit 在子进程跑单次回测。
- _init_worker：进程 initializer，加载 data_lake 一次（数 GB parquet），所有 task 复用。
- run_replay_worker：读 task → 装配 price_data → 跑 replay(progress_cb/abort_cb)
  → 写回 SUCCESS/FAILED/CANCELLED。abort 经 multiprocessing.Event 传入，
  progress 经 Queue 回报主进程（主进程单点写 DB，避免跨进程 SQLite 锁）。

可测试性（关键）：
    _load_price_data/_merge_cfg 从 caisen_service 【模块级】import，成为本模块属性，
    测试 monkeypatch replay_worker._load_price_data 即生效。若改成函数内
    `from ... import`，每次调用重新绑定到源模块，monkeypatch 本模块属性将失效。
    无循环 import：caisen_service 依赖 backtest_replay/storage 等，不反向依赖 replay_worker。
"""
from __future__ import annotations

import json
import logging
import os

from caisen import replay_tasks_db
from caisen.backtest_replay import replay, ReplayAborted
from caisen.risk import RiskManager
# 注意：strategies.caisen_pattern 不在模块级 import——会触发循环
# （execution.__init__→replay_worker→strategies→caisen→execution）。改 run_replay_worker 内延迟 import。
# 模块级 import → _load_price_data/_merge_cfg 成为本模块属性（测试 monkeypatch 生效）。
# Step4e 反向债收口：原 ``from server.services.caisen_service import _load_price_data,
# _merge_cfg`` 是 execution→server 反向依赖（Step2.2 过渡债）。现改 import data.price_loader
# 的模块级函数（逻辑单源，与 facade 同源），消除反向依赖。alias 保持 _load_price_data /
# _merge_cfg 模块名 → replay_worker._load_price_data 测试 monkeypatch 语义不变。
from data.price_loader import load_price_data as _load_price_data, merge_cfg as _merge_cfg

logger = logging.getLogger(__name__)

# 模块级：worker 进程内复用的 data_lake reader（_init_worker 装配，所有 task 共享）。
_reader = None


def _init_worker():
    """ProcessPoolExecutor initializer：加载 daily 湖一次（子进程常驻复用）。

    物理意图：全市场 parquet 加载占数 GB + 耗时，每 task 重 load 不可接受。
    进程池 worker 常驻 → _init_worker 首次调用 load 一次 → 后续 task 直接复用 _reader。

    防御性（CLAUDE.md 边界审查）：daily parquet 缺失（离线/CI）时 os.path.exists 守卫跳过
    load，_reader 保持 None 不抛——生产 _load_price_data 自带守卫会降级返空 dict →
    worker 据「空 price_data」显式标 FAILED（spec §7 data_lake 离线不卡死）。
    """
    global _reader
    if _reader is not None:
        return
    from data.lake_reader import DataLakeReader
    from config import LAKE_CONFIG
    _reader = DataLakeReader.get_instance()
    daily_path = LAKE_CONFIG.get("lakes", {}).get("daily")
    loaded = getattr(_reader, "loaded", False)
    lakes = _reader.lakes() if hasattr(_reader, "lakes") else []
    if daily_path and os.path.exists(daily_path) and (not loaded or "daily" not in lakes):
        _reader.load(daily_path, key="daily")


def run_replay_worker(task_id: str, abort_flag, progress_q, heartbeat_q) -> None:
    """worker 入口：跑单次回测 + 写回状态。任何异常都落 FAILED（不抛出子进程外）。

    参数：
        task_id：任务 id。
        abort_flag：multiprocessing.Event，主进程 set 后 worker 循环顶命中即 CANCELLED。
        progress_q：worker 上报 (task_id, done, total)，主进程消费落库（单点写 DB）。
        heartbeat_q：预留（worker 周期心跳，调度器据此识别崩溃；当前未写，留扩展点）。
    """
    try:
        _init_worker()
        task = replay_tasks_db.get_task(task_id)
        if task is None:
            logger.warning("worker 任务不存在：task_id=%s", task_id)
            return
        strategy_name = task.get("strategy_name", "caisen")
        # universe 已由 replay_tasks_db 从 universe_json 还原：None=全市场 / list=指定标的。
        price_data = _load_price_data(task["universe"], task["end"])
        if not price_data:
            # spec §7：data_lake 离线 / universe 无数据 → 装配空 → 显式 FAILED
            # （不卡死、不跑空回测返零统计伪装 SUCCESS——区分「真无样本」与「数据链路断」）。
            replay_tasks_db.mark_failed(
                task_id, "price_data 装配为空（data_lake 离线或 universe 无可用数据）")
            return

        # abort 回调：查 Event（主进程 set 后 replay symbol 循环顶命中即抛 ReplayAborted）。
        def _abort():
            return abort_flag.is_set()

        # progress 回调：投 Queue 由主进程消费落库（跨进程经 Queue，避免跨进程 SQLite 写锁）。
        def _progress(done, total):
            try:
                progress_q.put((task_id, done, total))
            except Exception:
                pass   # Queue 故障不阻断回测（进度丢失可接受，最终结果不丢）

        # 阶段C：按 strategy_name 构造策略。函数内 import 避免模块级循环
        # （execution.__init__→replay_worker→strategies→caisen→execution）。
        if strategy_name == "neckline":
            from strategies.neckline_method import NecklineMethodStrategy
            strategy = NecklineMethodStrategy(cfg_override=task.get("cfg_override"))
        else:  # "caisen"（caisen 形态，阶段E 随形态代码删）
            cfg = _merge_cfg(task["cfg_override"])
            risk = RiskManager(cfg)
            from strategies.caisen_pattern import CaisenPatternStrategy
            strategy = CaisenPatternStrategy(cfg, risk, 1_000_000.0)
        report = replay(
            price_data, strategy,
            start=task["start"], end=task["end"],
            progress_cb=_progress, abort_cb=_abort,
        )
        # ReplayReport dataclass → __dict__ → JSON（default=str 兜底 Timestamp/numpy 类型）。
        replay_tasks_db.mark_success(
            task_id, json.dumps(report.__dict__, ensure_ascii=False, default=str))
    except ReplayAborted:
        replay_tasks_db.mark_cancelled(task_id)
        logger.info("worker 任务被取消：task_id=%s", task_id)
    except Exception as exc:
        # 兜底：装配/回测任何异常都标 FAILED（不裸抛出子进程，避免 ProcessPoolExecutor 崩）。
        replay_tasks_db.mark_failed(task_id, f"{type(exc).__name__}: {exc}")
        logger.exception("worker 任务异常：task_id=%s", task_id)
