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
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from broadcast.brief import build_daily_brief
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

def _build_brief(bot: str, date: str, reader: DataLakeReader):
    """按机器人路由到对应 brief 构造器。

    一期 Task 2 仅搭框架：market 走既有 build_daily_brief；其余三个抛
    NotImplementedError，Task 3-5 各自接入时改 raise → 真实调用。
    本函数集中路由，避免 main() 里散落 if/elif。
    """
    if bot == "market":
        # 既有行情播报 brief：return BriefResult(markdown=...)，调用链不变（回归红线）
        return build_daily_brief(date, reader=reader)
    # 一期新增三机器人框架占位（Task 3-5 接入前不可调用，防误用）
    raise NotImplementedError(
        f"bot={bot} 的 brief 构造器尚未实现（Task 3-5 接入）；本 Task 仅搭框架"
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
