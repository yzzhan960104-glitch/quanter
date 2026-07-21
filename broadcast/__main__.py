# -*- coding: utf-8 -*-
"""每日播报 CLI 入口（一期观测运营层 · `python -m broadcast`）。

一期扩展：从单一行情播报扩展到 4 个机器人
  - market   ：既有每日行情播报（build_daily_brief，本模块历史能力）
  - trading  ：每日交易流水播报（Task 3 接入 build_trading_brief）
  - data     ：每日数据采集播报（Task 4 接入 build_data_brief）
  - strategy ：策略状态播报（Task 5 接入 build_strategy_brief）

每机器人独立幂等文件 `logs/.last_<bot>_brief`，互不干扰：
同日不重发（除非 --force）；周末/节假日 index_daily 最新日不变 → 天然跳过，零废报。
比维护一张 A 股交易日历表极简得多。

降级：dws 推送失败不写 last_brief（下次触发重试）；dry_run 不读/写 last_brief。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from broadcast.brief import build_daily_brief
from broadcast.brief_data import build_data_brief
from broadcast.brief_trading import build_trading_brief
from broadcast.push import push_brief
from config import LAKE_CONFIG
from data.lake_reader import DataLakeReader

logger = logging.getLogger(__name__)

# ===========================================================================
# 多机器人配置（一期：market 既有；trading/data/strategy 为一期新增）
# ===========================================================================

# 支持的机器人清单（CLI --bot choices 与 last_brief_file 校验同源，防散落硬编码）
SUPPORTED_BOTS = ("market", "trading", "data", "strategy")

# 各机器人的 .env 凭证变量名 + 幂等文件名 + 标题前缀（工厂式，避免散落硬编码）
#   robot_env：对应 .env 中该机器人 dws 应用 robot_code；不同机器人 = 不同 dws 应用 = 不同群
#   last     ：幂等去重文件名；分文件防跨机器人误判已播
#   title    ：钉钉消息标题前缀，便于一眼区分来源
_BOT_CFG = {
    "market":   {"robot_env": "DINGTALK_CHAT_ROBOT_CODE", "last": ".last_market_brief",
                 "title": "📈 每日行情播报"},
    "trading":  {"robot_env": "TRADING_BOT_ROBOT_CODE",   "last": ".last_trading_brief",
                 "title": "💰 每日交易播报"},
    "data":     {"robot_env": "DATA_BOT_ROBOT_CODE",      "last": ".last_data_brief",
                 "title": "🗄 每日数据播报"},
    "strategy": {"robot_env": "STRATEGY_BOT_ROBOT_CODE",  "last": ".last_strategy_brief",
                 "title": "♟ 每日策略播报"},
}

# 钉钉群组（所有机器人共用一个运营群；机器人身份靠 robot_code 区分）
_GROUP_ID_ENV = "BROADCAST_GROUP_ID"

# ===========================================================================
# 向后兼容别名（回归红线：tests/test_broadcast_main.py 直接 patch LAST_BC_FILE）
# ---------------------------------------------------------------------------
# 旧单一行情播报时代，幂等文件路径写死在此常量。一期多机器人化后，market 分支
# 走 `last_brief_file("market")`，但既有测试通过 monkeypatch.setattr(bm,"LAST_BC_FILE",...)
# 来隔离文件系统——保留此别名 = 兼容旧测试 + 旧运维脚本可能的直接引用，零成本。
#
# 文件名从 .last_broadcast 改为 .last_market_brief：与其他三机器人命名对齐
# （logs/.last_<bot>_brief）。生产侧后果：升级后首次运行 market 会重发一次当日
# （旧 .last_broadcast 不再读），可接受（dws send-by-bot 不会因重复触发自身限频）。
# ===========================================================================
LAST_BC_FILE = Path("logs") / ".last_market_brief"


def last_brief_file(bot: str) -> Path:
    """返回某机器人的幂等去重文件路径（logs/.last_<bot>_brief）。

    Why 工厂式：4 个机器人各自独立幂等文件，避免 trading 已播 → market 误判已播跳过。
    未知 bot 抛 ValueError（CLI 层 argparse choices 已挡一道，这里是第二道防线）。

    特例：market 分支返回模块级 LAST_BC_FILE（而非新建 Path），回归测试通过
    monkeypatch.setattr(bm,"LAST_BC_FILE",...) 注入 tmp_path，必须经此引用才能生效。
    """
    if bot not in _BOT_CFG:
        raise ValueError(f"未知 bot={bot}，支持：{SUPPORTED_BOTS}")
    if bot == "market":
        # 经 LAST_BC_FILE 引用：兼容既有测试 monkeypatch 注入临时路径（回归红线）
        return LAST_BC_FILE
    return Path("logs") / _BOT_CFG[bot]["last"]


# 播报只用这 4 个湖（不全量 load：a_shares_daily 9M 行/408MB load 慢且浪费，market brief 用不到）。
# 注：一期 trading/data/strategy brief 是否复用同一湖子集，Task 3-5 各自定，本框架层不锁死。
_BRIEF_LAKES = ("index_daily", "ths_daily", "moneyflow", "dragon_list")


def _load_reader() -> DataLakeReader:
    """仅 load 播报用到的 4 湖（复用 server/main.py:78 load 模式，但收窄到 _BRIEF_LAKES）。

    Why 不全量：LAKE_CONFIG['lakes'] 含 a_shares_daily（9M 行/408MB），market 播报用不到，
    load 它纯浪费内存+启动时间。parquet 缺失则 lake_reader 内部离线降级（不阻断）。
    """
    reader = DataLakeReader.get_instance()
    lakes = LAKE_CONFIG.get("lakes", {})
    for key in _BRIEF_LAKES:
        path = lakes.get(key)
        if path:
            reader.load(path, key=key)
    return reader


def _latest_trade_date(reader: DataLakeReader) -> str | None:
    """index_daily 最新交易日（YYYY-MM-DD）；湖空/无 date 层级 → None。"""
    df = reader.get_lake("index_daily")
    if df is None or getattr(df, "empty", True):
        return None
    try:
        dates = df.index.get_level_values("date")
    except Exception:
        return None
    try:
        return pd.Timestamp(dates.max()).strftime("%Y-%m-%d")
    except Exception:
        return None


# ===========================================================================
# 幂等读写（泛化：按 last_file 路径读写；旧函数名保留兼容）
# ===========================================================================

def _read_last(last_file: Path) -> str:
    """读上次播报日期；文件不存在/损坏 → 空串（首次播报或容错）。"""
    try:
        return last_file.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_last(date: str, last_file: Path) -> None:
    """记录本次播报日期（幂等依据）。写失败仅 warning：不影响本次推送，但下次可能重复发。"""
    try:
        last_file.parent.mkdir(parents=True, exist_ok=True)
        last_file.write_text(date, encoding="utf-8")
    except Exception:
        logger.warning("写 %s 失败（不影响本次推送，但下次可能重复）", last_file, exc_info=True)


def _read_last_broadcast() -> str:
    """[向后兼容] 读 market 机器人幂等文件（旧单一播报时代 API，回归测试依赖）。"""
    return _read_last(LAST_BC_FILE)


def _write_last_broadcast(date: str) -> None:
    """[向后兼容] 写 market 机器人幂等文件（旧单一播报时代 API，回归测试依赖）。"""
    _write_last(date, LAST_BC_FILE)


# ===========================================================================
# Brief 构造器路由（market 既有；trading/data/strategy Task 3-5 接入）
# ===========================================================================

def _fetch_trading_snapshot(date: str) -> tuple[list, dict | None, list | None, dict]:
    """取交易机器人当日快照四件套：(trades, asset, positions, status)。

    Why 集中取数 + 兜底降级：
    - __main__ 是同步 CLI，但 trading_service.get_asset/get_positions 是 async（网关
      查询走 broker 回调/线程池）。用 asyncio.run 一次性并发取两个 async（单 event loop），
      避免两次 asyncio.run 各启一个 loop 的开销。
    - 网关未连接/取数失败：asset 传 None、positions 传 None，brief 自动降级文案，绝不抛
      （trading 播报是观测层，断线不应阻断播报，而应如实把「断线」播出去）。
    - status 始终可取（trading_service.get_status 同步，四态之一）。
    - trades 走同步 query_trades（CSV 全表扫描，本身即降级契约：文件不存在返空列表）。
    """
    # 延迟 import：trading_service 顶层 import 了 core.notifier/trading.execution_gateway
    # 等较重链路，且 __main__ 仅 trading 分支需要 → 放函数内，market/data/strategy 分支零负担。
    from server.services import trading_service

    # 同步取数：trades（CSV 流水）/ status（四态镜像）
    try:
        trades_payload = trading_service.query_trades(date, date, limit=100)
        trades = list(trades_payload.get("trades", [])) if trades_payload else []
    except Exception:
        logger.warning("query_trades 取数失败，trading brief 成交节降级为空", exc_info=True)
        trades = []
    try:
        status = trading_service.get_status() or {}
    except Exception:
        logger.warning("get_status 取数失败，trading brief 网关态降级为空", exc_info=True)
        status = {}

    # async 取数：asset / positions。单 event loop 并发取，失败兜底 None（brief 降级）。
    async def _fetch_pair():
        # gather + return_exceptions：任一异常转对象返回，不互相阻塞
        return await asyncio.gather(
            trading_service.get_asset(),
            trading_service.get_positions(),
            return_exceptions=True,
        )

    asset: dict | None = None
    positions: list | None = None
    try:
        results = asyncio.run(_fetch_pair())
    except RuntimeError as e:
        # asyncio.run 在已有 event loop 的环境（如 Jupyter/某些测试）会抛 RuntimeError；
        # 同步 CLI 正常不会触发，兜底日志 + 降级。
        logger.warning("asyncio.run 取 asset/positions 失败（环境无新 event loop?）：%s", e)
        results = []
    except Exception:
        logger.warning("取 asset/positions 异常，trading brief 资金/持仓节降级", exc_info=True)
        results = []

    if len(results) >= 2:
        a, p = results[0], results[1]
        # get_asset：无网关/未连接返 {}（falsy → brief 视为 None 降级，等价语义）
        asset = a if (not isinstance(a, Exception) and a) else None
        # get_positions：无网关/未连接 raise RuntimeError → 兜底 None
        positions = None if isinstance(p, Exception) else (p or None)

    return trades, asset, positions, status


def _fetch_data_snapshot() -> list[dict]:
    """取数据机器人健康度快照：datasets 列表（与 GET /data/datasets 同源）。

    Why 集中取数 + 兜底降级（与 _fetch_trading_snapshot 同纪律）：
    - __main__ 是同步 CLI，data_service.list_datasets() 也是同步（纯读文件系统 + 内存湖，
      无阻塞 IO），直接调用即可——无需 asyncio。
    - 任一异常（DATASET_REGISTRY 未初始化/文件系统异常）均降级为空列表，brief 自动走
      「无数据集」降级文案，绝不抛（数据观测层不应因取数失败而阻断播报）。
    - 补 freshness_hours 字段：list_datasets() 原返字段含 latest_sync 但无实际 lag 小时，
      brief build_data_brief 的「最老 lag」+「异常清单 lag」依赖此字段——这里从 latest_sync
      ISO（UTC）解析为实际 age 小时（None 则不显示 lag，与 missing/failed 语义一致）。
    """
    # 延迟 import：data_service 顶层会触发 config 注册表 + DataLakeReader 耦合，
    # market/trading 分支不需要 → 放函数内，避免无谓依赖加载。
    from datetime import datetime, timezone
    from server.services import data_service

    try:
        raw = data_service.list_datasets() or []
    except Exception:
        logger.warning("list_datasets 取数失败，data brief 降级为空列表", exc_info=True)
        return []

    now_ts = datetime.now(timezone.utc).timestamp()
    out: list[dict] = []
    for item in raw:
        # 只透传 brief 需要的字段（key/status/latest_sync），并按需补 freshness_hours（实际 lag）
        latest_sync = item.get("latest_sync")
        age_hours: float | None = None
        if latest_sync:
            try:
                # list_datasets 写入格式 "%Y-%m-%d %H:%M:%S UTC"（data_service._now_iso 同款）
                ts = datetime.strptime(latest_sync, "%Y-%m-%d %H:%M:%S UTC").replace(
                    tzinfo=timezone.utc
                ).timestamp()
                age_hours = max(0.0, (now_ts - ts) / 3600.0)
            except (ValueError, TypeError):
                # latest_sync 格式异常 → 不显示 lag，brief 自动走「无 lag 数据」分支
                age_hours = None
        enriched = {
            "key": item.get("key", "?"),
            "status": item.get("status", "unknown"),
        }
        if age_hours is not None:
            enriched["freshness_hours"] = age_hours
        out.append(enriched)
    return out


def _build_brief(bot: str, date: str, reader: DataLakeReader):
    """按机器人路由到对应 brief 构造器。

    一期 Task 2 框架 + Task 3 trading + Task 4 data 接入：market 走既有
    build_daily_brief；trading/data 走注入式取数 + 纯函数渲染；strategy 仍
    NotImplementedError（Task 5 接入时改 raise → 真实调用）。
    本函数集中路由，避免 main() 里散落 if/elif。
    """
    if bot == "market":
        # 既有行情播报 brief：return BriefResult(markdown=...)，调用链不变（回归红线）
        return build_daily_brief(date, reader=reader)
    if bot == "trading":
        # 交易机器人：取数注入 → 纯函数渲染。取数失败任一项均降级，不阻断播报。
        trades, asset, positions, status = _fetch_trading_snapshot(date)
        return build_trading_brief(
            date, trades=trades, asset=asset, positions=positions, status=status,
        )
    if bot == "data":
        # 数据机器人：取 datasets 快照 → 纯函数渲染健康度文案。取数失败降级为空列表。
        datasets = _fetch_data_snapshot()
        return build_data_brief(date, datasets=datasets)
    # strategy 占位（Task 5 接入前不可调用，防误用）
    raise NotImplementedError(
        f"bot={bot} 的 brief 构造器尚未实现（Task 5 strategy 接入）；本 Task 仅搭框架"
    )


# ===========================================================================
# CLI 主入口
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。返回 0=成功/跳过，1=无法定播报日，2=推送失败。

    --bot {market|trading|data|strategy}（默认 market）：选择机器人身份，
    决定 brief 构造器 + 凭证 + 幂等文件。market 分支行为与一期前完全一致（回归红线）。
    """
    p = argparse.ArgumentParser(
        prog="python -m broadcast", description="钉钉播报（多机器人 · 一期观测运营层）"
    )
    p.add_argument("--bot", default="market", choices=SUPPORTED_BOTS, help="机器人身份")
    p.add_argument("--date", help="播报日 YYYY-MM-DD（缺省=index_daily 最新交易日）")
    p.add_argument("--dry-run", action="store_true", help="只打印文案不发钉钉")
    p.add_argument("--force", action="store_true", help="忽略幂等去重强制重发")
    args = p.parse_args(argv)

    reader = _load_reader()
    date = args.date or _latest_trade_date(reader)
    if date is None:
        logger.error("无法确定播报日（index_daily 未加载/为空）；用 --date 显式指定")
        return 1

    # 幂等去重：每机器人独立文件，防跨机器人误判；dry_run 不参与去重（随时可预览文案）；
    # 非 force 且今日已播 → 跳过。
    last_file = last_brief_file(args.bot)
    if not args.dry_run and not args.force and _read_last(last_file) == date:
        print(f"{args.bot} 今日({date})已播报，跳过（--force 可重发）")
        return 0

    brief = _build_brief(args.bot, date, reader)
    title = f"{_BOT_CFG[args.bot]['title']} {date}"
    robot_code = os.getenv(_BOT_CFG[args.bot]["robot_env"], "")
    group_id = os.getenv(_GROUP_ID_ENV, "")
    ok = push_brief(
        title, brief.markdown,
        robot_code=robot_code, group_id=group_id, dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0
    if ok:
        _write_last(date, last_file)
        print(f"{args.bot} 播报已推送({date})")
        return 0
    logger.error("%s 推送失败，未写 %s（下次触发重试）", args.bot, last_file)
    return 2


if __name__ == "__main__":
    sys.exit(main())
