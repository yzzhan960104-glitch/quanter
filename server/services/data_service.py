# -*- coding: utf-8 -*-
"""层级一·数据湖资产服务：状态推导 + 同步触发（决策点① = 方案 B，不引 Celery Beat）。

核心设计（反黑盒）：
- 状态真相源 = 文件系统（parquet mtime + data_lake/.syncing/{key} 哨兵），不依赖任何
  调度器守护进程，零新增运维组件，符合 Karpathy 极简原则。
- 同步以 sys.executable 子进程拉起 scripts/sync_*.py（与脚本既有 CLI 语义一致），
  隔离 AKShare/JQData/Binance 等重网络/重内存依赖对 FastAPI 主进程的污染。
- 触发即返回（fire-and-forget）：写哨兵 → 起 daemon 线程跑子进程 → 立即返回 syncing；
  子进程结束后由 daemon 线程删哨兵（成功）或写 .failed（失败）。

拷问三连（已显式处置）：
- 流动性与极端行情 / 重复触发：哨兵存在即拒绝二次派发，防并发同步互相覆盖 parquet。
- 接口与状态机边界 / 半截 parquet：sync 脚本 to_parquet 落盘是 pandas 原子写；子进程被杀
  或超时时哨兵保留为 syncing，不会被误判 healthy（状态机单值，优先级 syncing > 一切）。
- 时区与脏数据：mtime 比较用 time.time() 秒级时间戳，规避本地时区/DST 漂移；
  data_start/end 从内存湖取，绝不为此 read_parquet（大文件 IO 会拖垮 /datasets 响应）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from config import DATASET_REGISTRY, LAKE_CONFIG, SYNCING_DIR

logger = logging.getLogger(__name__)

# 子进程超时（秒）：同步脚本多含网络拉取（jqdata 配额 / binance 大文件 / akshare 限频），
# 给 10 分钟上限；超时则 kill 子进程并转 .failed，避免僵尸 syncing 永久卡住前端状态。
_SYNC_TIMEOUT_SEC = 600
# .failed 文件保留的 stderr 尾部字符数（防巨量日志撑爆 /datasets 响应体）
_ERR_TAIL_CHARS = 500

# trigger_sync 哨兵 check-then-set 锁（#15）：防并发触发同 key 同步。
# 物理意图：原 exists 检查与写哨兵非原子，两并发请求都过检查 → 两个 daemon 子进程互覆盖
# 同一 parquet（半截写入损坏）。Lock 包 check-then-set 使哨兵检查+写入原子化，第二个请求
# 必看到第一个写的哨兵而返"进行中"。daemon 子进程在锁外异步跑（不持锁阻塞其它 key）。
_trigger_lock = threading.Lock()

# 项目根：data_service.py 位于 server/services/，上溯三级 = quanter/。
# 用绝对路径拼 script，保证 uvicorn 以任意 CWD 启动都能定位 scripts/sync_*.py。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 数据集中文展示名（表格首列；与 LAKE key 同步维护，单一维护点）
_DATASET_LABELS: Dict[str, str] = {
    "macro": "宏观信贷湖",
    "sector": "板块日频湖",
    "daily": "A股全市场日线",
    "daily_active": "A股活跃池日线",
    "minute": "A股分钟湖",
    "north_flow": "北向资金日频",
    "dragon_list": "龙虎榜明细",
}


def _now_iso() -> str:
    """当前 UTC 时刻 ISO 字符串（UTC 规避 naive/local 混比，前端按需本地化展示）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _sentinel_path(key: str, *, failed: bool = False) -> str:
    """哨兵文件路径：syncing 用 {key}，失败标记用 {key}.failed。"""
    return os.path.join(SYNCING_DIR, f"{key}.failed" if failed else key)


def _lake_key(key: str) -> str:
    """取数据集 key 对应的「物理湖 key」（LAKE_CONFIG["lakes"] 索引键）。

    物理意图：多数数据集 key 与湖 key 一一相等；但复用湖场景（top_list→dragon_list、
    hsgt_top10→north_flow，切 Tushare 替代 akshare）数据集名 ≠ 湖 key，需在 DATASET_REGISTRY
    用 lake_key 字段显式指向既有湖。本函数读 lake_key，缺省 fallback 到数据集 key 自身——
    保证既有 daily/macro 等正常数据集零回归。

    Why 集中在此：_parquet_path / _loaded_data_span 都靠它作湖索引，单一维护点防分叉。
    """
    return DATASET_REGISTRY.get(key, {}).get("lake_key", key)


def _parquet_path(key: str) -> Optional[str]:
    """取湖 key 对应的 parquet 路径（LAKE_CONFIG["lakes"] 单一真相源）；未登记返 None。

    复用湖（top_list/hsgt_top10）经 _lake_key 映射到 dragon_list/north_flow 的物理路径，
    否则前端 list_datasets 会误报这两个数据集 status=missing（即便物理湖已同步）。
    """
    return LAKE_CONFIG.get("lakes", {}).get(_lake_key(key))


def _derive_status(key: str, parquet_path: Optional[str]) -> Tuple[str, Optional[str]]:
    """联合「哨兵 + parquet 存在性 + mtime 新鲜度」推导状态。

    返回 (status, last_error)。优先级（状态机单值，前者压倒后者）：
      1. .syncing/{key} 存在 → syncing（同步进行中是最强状态，覆盖健康/陈旧判定）
      2. .syncing/{key}.failed 存在 → failed（上次同步失败，读尾部错误）
      3. parquet 不存在 → missing（从未同步成功过）
      4. parquet mtime 距今 ≤ freshness_hours → healthy
      5. 否则 → stale（数据陈旧，建议重同步）
    """
    # 1. syncing 哨兵优先
    if os.path.exists(_sentinel_path(key)):
        return "syncing", None
    # 2. 失败哨兵
    failed_path = _sentinel_path(key, failed=True)
    if os.path.exists(failed_path):
        try:
            with open(failed_path, "r", encoding="utf-8") as f:
                err = f.read().strip() or "上次同步失败（未捕获详细错误）"
            return "failed", err[-_ERR_TAIL_CHARS:]
        except OSError:
            return "failed", "上次同步失败（错误日志读取失败）"
    # 3. parquet 缺失 → missing
    if not parquet_path or not os.path.exists(parquet_path):
        return "missing", None
    # 4/5. mtime 新鲜度（秒级时间戳比较，规避时区/DST 漂移）
    freshness_hours = DATASET_REGISTRY.get(key, {}).get("freshness_hours", 24)
    age_hours = (time.time() - os.path.getmtime(parquet_path)) / 3600.0
    return ("healthy" if age_hours <= freshness_hours else "stale", None)


def _loaded_data_span(key: str) -> Tuple[Optional[str], Optional[str]]:
    """从 DataLakeReader 已载入的内存湖取数据起止日（best-effort 展示）。

    设计取舍（前视/一致性）：
    - 只读已载入内存（reader._lakes），绝不为此再 read_parquet（大文件 IO 拖垮响应）。
    - 同步刚完成时内存湖是旧快照，data_end 会滞后——这是可接受的展示语义：
      latest_sync（mtime）反映「上次同步动作时刻」，data_end 反映「进程内数据跨度」，
      二者解耦，各自诚实。用户刷新触发 reader 重载后自洽。
    """
    try:
        import pandas as pd  # 延迟 import，避免模块级耦合
        from data.lake_reader import DataLakeReader
        reader = DataLakeReader.get_instance()
        df = reader.get_lake(_lake_key(key))  # 复用湖（top_list→dragon_list 等）映射到物理湖 key
        if df is None or len(df) == 0:
            return None, None
        idx = df.index
        # MultiIndex(date,symbol) 取 date 层；DatetimeIndex 直接取整体
        dates = idx.get_level_values("date") if isinstance(idx, pd.MultiIndex) else idx
        if len(dates) == 0:
            return None, None
        return str(pd.Timestamp(dates.min()).date()), str(pd.Timestamp(dates.max()).date())
    except Exception as exc:
        # 任何异常都不阻断 /datasets（数据跨度是展示项，非关键路径）
        logger.debug("数据跨度读取失败(key=%s): %s", key, exc)
        return None, None


def _clear_sentinel(key: str) -> None:
    """清掉 syncing 与 .failed 哨兵（同步成功的收尾）。静默忽略文件不存在。"""
    for path in (_sentinel_path(key), _sentinel_path(key, failed=True)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _mark_failed(key: str, message: str) -> None:
    """写 .failed 哨兵（先清 syncing 再写 .failed，保证状态机单值）。"""
    _clear_sentinel(key)
    try:
        os.makedirs(SYNCING_DIR, exist_ok=True)
        with open(_sentinel_path(key, failed=True), "w", encoding="utf-8") as f:
            f.write(message)
    except OSError as exc:
        logger.error("无法写入失败哨兵(key=%s): %s", key, exc)


def _run_sync_subprocess(key: str) -> None:
    """daemon 线程入口：子进程跑 sync 脚本，按结果处理哨兵。

    成功：清哨兵（→ 状态回到 healthy/stale，由 mtime 决定）。
    失败：写 .failed（含 stderr 尾部），供前端展示失败原因。
    超时：subprocess.TimeoutExpired → kill 子进程，按失败处理。
    任意异常：兜底写 .failed，绝不留永久 syncing 假状态。
    """
    spec = DATASET_REGISTRY.get(key, {})
    script_rel = spec.get("script")
    args = list(spec.get("args", []))
    try:
        os.makedirs(SYNCING_DIR, exist_ok=True)
        # 绝对路径拼 script（_PROJECT_ROOT 锚定，不受 uvicorn CWD 影响）
        script_abs = os.path.join(_PROJECT_ROOT, script_rel)
        # sys.executable 保证用当前解释器（含 venv），与开发机/生产一致
        cmd = [sys.executable, script_abs, *args]
        logger.info("数据集同步开始: %s → %s", key, " ".join(cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_SYNC_TIMEOUT_SEC, check=False,
            cwd=_PROJECT_ROOT,  # 锚定 CWD：脚本内相对路径（如 data_lake/）解析一致
        )
        if proc.returncode == 0:
            _clear_sentinel(key)
            logger.info("数据集同步成功: %s", key)
        else:
            tail = (proc.stderr or proc.stdout or f"退出码 {proc.returncode}")
            _mark_failed(key, tail.strip()[-_ERR_TAIL_CHARS:])
            logger.warning("数据集同步失败: %s 退出码=%s stderr=%s",
                           key, proc.returncode, (proc.stderr or "")[:200])
    except subprocess.TimeoutExpired:
        _mark_failed(key, f"同步超时（>{_SYNC_TIMEOUT_SEC}s），子进程已终止")
        logger.warning("数据集同步超时: %s", key)
    except Exception as exc:
        # 兜底：任何未预见异常都转 .failed，杜绝僵尸 syncing
        _mark_failed(key, f"同步异常: {type(exc).__name__}: {exc}")
        logger.exception("数据集同步未预见异常: %s", key)


def trigger_sync(key: str) -> Dict[str, Any]:
    """触发某数据集同步（写哨兵 + 起 daemon 线程跑子进程，立即返回）。

    幂等保护：syncing 哨兵已存在 → 直接返回 syncing，不重复派发（防 parquet 互覆盖）。
    无 script 配置 → 抛 KeyError（路由层转 404）。
    """
    spec = DATASET_REGISTRY.get(key)
    if not spec or not spec.get("script"):
        raise KeyError(f"未登记的数据集或缺失同步脚本: {key}")
    os.makedirs(SYNCING_DIR, exist_ok=True)
    # #15：Lock 包 check-then-set，原子化哨兵检查+写入（防并发触发同 key 双子进程互覆盖 parquet）。
    # 锁内只做哨兵 check-改-写；起 daemon 子进程在锁外（异步，不持锁阻塞其它 key 的 trigger）。
    with _trigger_lock:
        # 重复触发保护：syncing 中直接拒绝二次派发
        if os.path.exists(_sentinel_path(key)):
            return {"key": key, "status": "syncing", "message": "同步进行中，请勿重复触发"}
        # 清掉历史 .failed，本次重试
        failed_path = _sentinel_path(key, failed=True)
        if os.path.exists(failed_path):
            try:
                os.remove(failed_path)
            except OSError:
                pass
        # 写 syncing 哨兵（含触发时刻，便于排查）
        try:
            with open(_sentinel_path(key), "w", encoding="utf-8") as f:
                f.write(_now_iso())
        except OSError as exc:
            logger.warning("写 syncing 哨兵失败(key=%s): %s", key, exc)
    # 起 daemon 线程跑子进程（锁外：daemon=True 进程退出时自动回收，不阻塞 uvicorn 关停）
    threading.Thread(target=_run_sync_subprocess, args=(key,), daemon=True).start()
    return {"key": key, "status": "syncing", "message": "已触发同步，后台子进程执行中"}


def list_datasets() -> List[Dict[str, Any]]:
    """枚举全部数据集资产（反射 DATASET_REGISTRY + 派生 status/时间）。"""
    out: List[Dict[str, Any]] = []
    for key, spec in DATASET_REGISTRY.items():
        parquet_path = _parquet_path(key)
        status, last_error = _derive_status(key, parquet_path)
        # latest_sync = parquet mtime ISO（缺失则 None）
        latest_sync: Optional[str] = None
        if parquet_path and os.path.exists(parquet_path):
            try:
                ts = os.path.getmtime(parquet_path)
                latest_sync = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            except OSError:
                latest_sync = None
        data_start, data_end = _loaded_data_span(key)
        out.append({
            "key": key,
            "name": _DATASET_LABELS.get(key, key),
            "source": spec.get("source", ""),
            "market": spec.get("market", ""),
            "granularity": spec.get("granularity", ""),
            "schedule": spec.get("schedule", ""),
            "status": status,
            "data_start": data_start,
            "data_end": data_end,
            "latest_sync": latest_sync,
            "last_error": last_error,
        })
    return out


def sweep_stale_on_startup() -> List[str]:
    """启动同步 sweep：扫 DATASET_REGISTRY 对 stale/missing 数据集调 trigger_sync（#6）。

    物理意图：后端启动时静默补过期/缺失数据（用户诉求 #6「启动后静默更新数据」）。
    复用 trigger_sync 的子进程 + 哨兵幂等 + JQData QuotaExceeded 优雅停，不引独立
    调度器（契合 config.py「方案 B 零守护进程」——线程寄生主进程，非 Celery Beat/APScheduler）。

    Why freshness 而非 schedule：DATASET_REGISTRY 的 schedule 是中文展示串（"每日18:00"，
    非机器可读 cron），用 _derive_status 的 freshness_hours 隐式定时——stale/missing 即补，
    等价"超计划节奏即重跑"，无需解析 cron。

    返回：本次触发同步的 key 列表（stale/missing 的），便于上层日志记录与测试断言。
    """
    triggered: List[str] = []
    for asset in list_datasets():
        if asset.get("status") in ("stale", "missing"):
            try:
                trigger_sync(asset["key"])
                triggered.append(asset["key"])
            except KeyError:
                pass   # 无 script 配置的数据集跳过（不阻断 sweep 其余数据集）
    return triggered
