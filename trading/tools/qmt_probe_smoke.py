# -*- coding: utf-8 -*-
"""T9 前置实证：query_account_status 主动探针在客户端四态的返回行为。

物理意图（Why，T9.md + research/T4-miniQMT-connection.md 待实证 #1 红线）：
  T9 要在 ``_health_guard`` 加主动探针，补 xtquant ``on_disconnected`` 回调的盲区——
  ``on_disconnected`` 仅在 socket 显式断时触发，**客户端重启中 / 启动失败 / session 假死**
  都不触发（T4 根因 C 层实证），engine 却以为 ``_connected=True`` 继续对僵死连接发废单。
  但探针选型（``query_account_status`` vs ``query_stock_asset`` vs 封装层 ``query_asset``）
  与「连续 N 次失败判僵死」的阈值 N，强依赖探针在客户端各种故障态的真实返回行为
  （返回值 / 异常类型 / 阻塞或超时）。本脚本长跑定时探针，用户手动注入 4 类故障，
  产出时间序列 CSV 供 T9 探针 design 定阈值——不臆测。

4 场景（用户手动注入，CSV 时间戳对齐）：
  S1 基线      —— 客户端正常稳态跑 2min（探针成功态长什么样 = 判僵死的对照基准）。
  S2 未启动    —— 脚本启动前不开 miniQMT（is_client_ready 应 False，探针返回对照）。
  S3 重启中    —— 稳态后 ``taskkill /F`` miniQMT 客户端（**核心盲区**：on_disconnected
                  是否触发 → ``_lock_down`` 是否变 True；探针返回是否立即变化）。
  S4 假死      —— 进程挂起（任务管理器挂起 / pssuspend）模拟卡住（探针是否超时）。

每轮探针（默认 10s 一轮）：
  ① ``query_account_status()``        —— 底层直调 ``gw._trader``（无参同步），投线程池 + 超时。
  ② ``query_stock_asset(account)``    —— 底层直调 ``gw._trader.query_stock_asset(gw._account)``。
  ③ ``gw.query_asset()``              —— 封装层对比（看现状是否已能感知僵死）。
  ④ gw 状态快照                       —— ``_connected`` / ``_lock_down`` / ``_reconnecting``
                                          / ``is_client_ready()`` / ``_client_staleness_diag()``。

记录 → ``logs/qmt_probe_<YYYYMMDD_HHMMSS>.csv``，每轮一行 + 墙钟时间戳。故障注入时刻由
用户在 taskkill 时记一下墙钟时间，事后与 CSV 的 ts_iso 列对齐即可。

铁律（CLAUDE.md 模拟仓无顾忌但仍守序）：
  - 严格只读：connect / 三个探针 / 状态快照，不发任何 submit_order / cancel_order。
  - 探针全部 try/except + 超时包裹，单探针失败/超时不阻断后续轮次。
  - connect 失败不退出（S2/S3 要观测未连/僵死态的探针行为，正是盲区核心）。

运行：
  .venv310/Scripts/python.exe trading/tools/qmt_probe_smoke.py
  .venv310/Scripts/python.exe trading/tools/qmt_probe_smoke.py --duration 600 --interval 10
"""
import argparse
import asyncio
import csv
import os
import sys
import time
import traceback
from datetime import datetime

# Windows 控制台默认 GBK，中文会 UnicodeEncodeError——强制 stdout/stderr 走 utf-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 脚本位于 项目根/trading/tools/，须三层 dirname 上溯项目根（两层只到 trading/，
# 会被 insert 进 sys.path[0] 遮蔽标准库 calendar + 致 from trading 找不到包——
# 见 memory [[syspath-calendar-shadowing]] 历史坑）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _PROJECT_ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except ImportError:
    pass

from trading.qmt_gateway import QmtExecutionGateway  # noqa: E402


# ============================================================================
# 探针实现：三个主动探针 + 状态快照，统一 (status, detail, ms) 三元组返回。
# status ∈ {"ok","timeout","exc","skipped"}，detail 携带 repr/异常文案（截断防 CSV 爆）。
# ============================================================================
def _trunc(s: str, n: int = 80) -> str:
    """截断字符串到 n 字符（CSV 单元格防爆 + 防换行污染行结构）。"""
    s = str(s).replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[:n] + "..."


async def _probe_blocking(func, *, timeout: float, label: str) -> tuple[str, str, int]:
    """通用同步探针包装：投线程池 + 超时，返回 (status, detail, ms)。

    Why 投线程池：xtquant 的 query_* 是同步阻塞调用（C++ pyd 后端），直接在事件循环
    调用会阻塞所有协程。asyncio.to_thread 把它丢到默认线程池，asyncio.wait_for 控超时——
    僵死态探针可能永久阻塞，超时是判定「假死」的关键信号（S4 场景）。
    """
    t0 = time.monotonic()
    try:
        ret = await asyncio.wait_for(asyncio.to_thread(func), timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        return ("ok", _trunc(repr(ret)), ms)
    except asyncio.TimeoutError:
        return ("timeout", f">{int(timeout)}s", int(timeout * 1000))
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return ("exc", _trunc(f"{type(e).__name__}: {e}"), ms)


async def _probe_gw_asset(gw, *, timeout: float) -> tuple[str, str, int]:
    """封装层 query_asset 探针（已是 async），wait_for 控超时。成功时记 total_asset 数值。"""
    t0 = time.monotonic()
    try:
        ret = await asyncio.wait_for(gw.query_asset(), timeout=timeout)
        ms = int((time.monotonic() - t0) * 1000)
        # 成功时提取 total_asset 数值（封装层正常返 {cash,total_asset,market_value}）；
        # 失败/异常态 ret 可能为 None → repr 兜底。
        if isinstance(ret, dict) and "total_asset" in ret:
            return ("ok", f"total_asset={ret.get('total_asset')}", ms)
        return ("ok", _trunc(repr(ret)), ms)
    except asyncio.TimeoutError:
        return ("timeout", f">{int(timeout)}s", int(timeout * 1000))
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return ("exc", _trunc(f"{type(e).__name__}: {e}"), ms)


async def _probe_round(gw, *, timeout: float) -> dict:
    """单轮探针：三个主动探针 + gw 状态快照。返回一行 dict（CSV 列）。"""
    trader = getattr(gw, "_trader", None)
    account = getattr(gw, "_account", None)

    # ① query_account_status（无参）—— T9 探针首选。trader=None（未装配）记 skipped。
    if trader is not None:
        qas_status, qas_detail, qas_ms = await _probe_blocking(
            trader.query_account_status, timeout=timeout, label="qas")
    else:
        qas_status, qas_detail, qas_ms = ("skipped", "trader=None", 0)

    # ② query_stock_asset(account) —— 备选探针（带 StockAccount 参数）。
    if trader is not None and account is not None:
        qsa_status, qsa_detail, qsa_ms = await _probe_blocking(
            lambda: trader.query_stock_asset(account), timeout=timeout, label="qsa")
    else:
        qsa_status, qsa_detail, qsa_ms = ("skipped", "trader/account=None", 0)

    # ③ 封装层 query_asset —— 现状对比（看 gw 当前是否已能感知僵死）。
    asset_status, asset_detail, asset_ms = await _probe_gw_asset(gw, timeout=timeout)

    # ④ gw 状态快照 —— _lock_down 变 True = on_disconnected 已触发（盲区核心观测项）。
    #    hasattr 兜底防 gw 未升级字段（防御性，理论上 __init__ 必置）。
    snap = {
        "gw_connected": str(getattr(gw, "_connected", None)),
        "gw_lock_down": str(getattr(gw, "_lock_down", None)),
        "gw_reconnecting": str(getattr(gw, "_reconnecting", None)),
        "gw_client_ready": str(gw.is_client_ready()),
        "gw_staleness_diag": _trunc(
            gw._client_staleness_diag() if hasattr(gw, "_client_staleness_diag") else "无诊断", 60),
    }

    return {
        "qas_status": qas_status, "qas_detail": qas_detail, "qas_ms": qas_ms,
        "qsa_status": qsa_status, "qsa_detail": qsa_detail, "qsa_ms": qsa_ms,
        "asset_status": asset_status, "asset_detail": asset_detail, "asset_ms": asset_ms,
        **snap,
    }


# ============================================================================
# 主循环
# ============================================================================
CSV_FIELDS = [
    "ts_iso", "elapsed_s", "round",
    "qas_status", "qas_detail", "qas_ms",
    "qsa_status", "qsa_detail", "qsa_ms",
    "asset_status", "asset_detail", "asset_ms",
    "gw_connected", "gw_lock_down", "gw_reconnecting", "gw_client_ready", "gw_staleness_diag",
]


async def run(duration: int, interval: int, probe_timeout: float) -> str:
    """长跑探针主循环：连接 → 每 interval 秒一轮 → CSV 追加。返回 CSV 路径。"""
    print("=" * 72)
    print("T9 前置实证：query_account_status 主动探针行为观测")
    print(f"account={os.getenv('QMT_ACCOUNT_ID')}  userdata={os.getenv('QMT_USERDATA_PATH')}")
    print(f"session={os.getenv('QMT_SESSION_ID', '123456')}  duration={duration}s  interval={interval}s  probe_timeout={probe_timeout}s")
    print("=" * 72)
    print("【场景引导】请按需手动注入故障，记下墙钟时间事后与 CSV ts_iso 对齐：")
    print("  S1 基线    —— 客户端正常跑（先观测稳态成功返回）")
    print("  S2 未启动  —— 启动前不开 miniQMT（脚本仍会跑，观测未连态探针）")
    print("  S3 重启中  —— 稳态后 taskkill /F miniQMT 客户端（核心盲区）")
    print("  S4 假死    —— 任务管理器挂起 miniQMT 进程（观测探针是否超时）")
    print("-" * 72)

    # 连接装配（复用 headless smoke 范式）。connect 失败不退出——S2/S3 正要观测未连/僵死态。
    gw = QmtExecutionGateway()
    try:
        await gw.connect()
        print(f"[connect] 完成 _connected={gw._connected} is_locked={gw.is_locked}")
    except Exception as e:
        # connect 失败：gw._trader 通常已赋值（_run_bootstrap 在 connect 前置位），
        # 探针仍可调（正是 S2「客户端未启动」要观测的态）。trader 若仍 None，探针记 skipped。
        print(f"[connect] 异常 {type(e).__name__}: {e}（继续探针观测未连态）")

    # CSV 路径：logs/qmt_probe_<YYYYMMDD_HHMMSS>.csv
    logs_dir = os.path.join(_PROJECT_ROOT, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    csv_path = os.path.join(logs_dir, f"qmt_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    t_start = time.monotonic()
    print(f"[csv] 记录 → {csv_path}")
    print("-" * 72)

    max_rounds = max(1, duration // interval)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        try:
            for i in range(1, max_rounds + 1):
                row = await _probe_round(gw, timeout=probe_timeout)
                ts_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                elapsed = int(time.monotonic() - t_start)
                full = {"ts_iso": ts_iso, "elapsed_s": elapsed, "round": i, **row}
                writer.writerow(full)
                f.flush()  # 每轮刷盘，防 taskkill 客户端时脚本意外退出丢数据
                # 控制台一行摘要（墙钟 + 三探针 status + _lock_down 变化，一眼扫故障注入生效）
                print(f"[r{i:03d} | {elapsed:4d}s | {ts_iso}] "
                      f"qas={row['qas_status']}/{row['qas_ms']}ms "
                      f"qsa={row['qsa_status']}/{row['qsa_ms']}ms "
                      f"asset={row['asset_status']}/{row['asset_ms']}ms "
                      f"| conn={row['gw_connected']} lock_down={row['gw_lock_down']} "
                      f"ready={row['gw_client_ready']}")
                if i < max_rounds:
                    await asyncio.sleep(interval)
        except KeyboardInterrupt:
            print("\n[中断] 用户退出（CSV 已保存）")
        finally:
            try:
                await gw.disconnect()
            except Exception:
                pass

    print("=" * 72)
    print(f"[done] CSV 已保存：{csv_path}")
    print("下一步：把 taskkill / 挂起的墙钟时间与 CSV ts_iso 列对齐，标注 S3/S4 场景，")
    print("       交回供 T9 探针 design 定「连续 N 次失败判僵死」阈值 N + 探针选型。")
    print("=" * 72)
    return csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description="T9 前置实证：query_account_status 探针行为观测")
    parser.add_argument("--duration", type=int, default=600,
                        help="总时长（秒，默认 600=10min）")
    parser.add_argument("--interval", type=int, default=10,
                        help="探针轮询间隔（秒，默认 10）")
    parser.add_argument("--probe-timeout", type=float, default=10.0,
                        help="单探针超时（秒，默认 10；S4 假死场景观测超时的关键）")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.duration, args.interval, args.probe_timeout))
    except KeyboardInterrupt:
        print("\n[中断] 用户退出。")
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    main()
