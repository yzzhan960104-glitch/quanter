# -*- coding: utf-8 -*-
"""掘金终端 audit 台账采集器（QuanterEmquantIngest · QMT 退役方案 P0 / G-2，
原 W3 计划提前落地）。

物理意图：掘金腿的 audit CSV（策略目录旁 audit/audit_YYYYMMDD.csv）是唯一操作
留痕——落 DB 后晨检/复盘/对账变成一条 SQL。每日 15:40（EOD 后）增量采集当日文件；
--date 可回补任意日（幂等：UNIQUE(ts,event,detail) + INSERT OR IGNORE，重跑零重复）。

表结构（experiments.db，新表不动既有表）：
    terminal_audit(audit_id PK, ts, event, detail(原样JSON文本),
                   account_id, strategy_id, ingested_at)
account/strategy 尽力提取：行内 detail JSON 带 account/strategy_id 键即取，
否则继承当日最近一次 INIT 行的值（audit 事件流里 INIT 是唯一稳定携带两者的行）。
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

from ops import gm_ops_common as gc

DB_PATH = ROOT / "experiment" / "experiments.db"

_DDL = """
CREATE TABLE IF NOT EXISTS terminal_audit (
    audit_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT,
    account_id  TEXT,
    strategy_id TEXT,
    ingested_at TEXT NOT NULL,
    UNIQUE(ts, event, detail, account_id)
)
"""


def _ensure_table(con: sqlite3.Connection) -> None:
    """建表或迁移旧唯一键（双腿真雷修复，2026-08-28 双轨方案 §4.3）。

    旧键 UNIQUE(ts,event,detail) 不含 account_id——两腿各自 10:31:00 写
    SCHEDULE_TICK（同秒+同 detail）时第二腿撞键被 INSERT OR IGNORE 静默吞，
    实验腿数据丢失。迁移=新键重建表搬数（terminal_audit 是台账非源数据，audit
    CSV 才是源，重建零损失；同 (ts,event,detail,account_id) 旧行若自身有重复
    搬运时去重——不丢信息只去冗余）。
    """
    con.execute(_DDL)
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='terminal_audit'"
    ).fetchone()
    if sql and "UNIQUE(ts, event, detail, account_id)" not in sql[0]:
        con.execute("ALTER TABLE terminal_audit RENAME TO terminal_audit_old")
        con.execute(_DDL)
        con.execute(
            "INSERT OR IGNORE INTO terminal_audit"
            "(ts,event,detail,account_id,strategy_id,ingested_at)"
            " SELECT ts,event,detail,account_id,strategy_id,ingested_at FROM terminal_audit_old")
        con.execute("DROP TABLE terminal_audit_old")


def _extract_ids(detail_json: str) -> tuple[str | None, str | None]:
    try:
        d = json.loads(detail_json) if detail_json else {}
    except ValueError:
        return None, None
    return d.get("account") or d.get("account_id"), d.get("strategy_id")


def ingest_day(day: str, db_path: Path = DB_PATH,
               strategy_dir: Path | None = None) -> dict:
    """采集单日 audit CSV → dict(inserted, skipped, file)。文件缺失 → file=None。

    账户归属三级兜底（2026-08-28 双轨补强）：行内 detail 键 > 当日最近 INIT 继承 >
    该腿 runtime.json 的 account_id（文件级真值）。第三级是双腿形态必需：bootstrap
    的 audit 序是 RECONCILE **先于** INIT（reconcile→audit INIT，pilot_body.bootstrap
    顺序），INIT 前置行靠行内/继承都拿不到账户——两腿各有 RECONCILE 落 NULL 则无法
    归腿，对照脚本的按账户查询会漏行。"""
    src = gc.audit_csv_path(day, strategy_dir)
    if not src.exists():
        return {"day": day, "file": None, "inserted": 0, "skipped": 0}
    leg_default_account = ""
    leg_default_strategy = ""
    if strategy_dir is not None:
        try:
            cfg = gc.runtime_config(strategy_dir)
            leg_default_account = str(cfg.get("account_id") or "")
            leg_default_strategy = str(cfg.get("strategy_id") or "")
        except (OSError, ValueError):
            pass                                    # runtime 缺失=文件级兜底不可用，降级旧语义
    ingested_at = f"{datetime.now():%Y-%m-%dT%H:%M:%S}"
    inserted = skipped = 0
    last_account = ""
    last_strategy = ""
    with sqlite3.connect(db_path) as con:
        _ensure_table(con)
        with src.open(encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 2 or not row[0]:
                    continue                              # 空行/残行走挡板
                ts, event = row[0], row[1]
                detail = row[2] if len(row) > 2 else None
                acct, sid = _extract_ids(detail)
                if event == "INIT":                       # INIT 携带权威 account/strategy
                    last_account = acct or last_account
                    last_strategy = sid or last_strategy
                cur = con.execute(
                    "INSERT OR IGNORE INTO terminal_audit"
                    "(ts,event,detail,account_id,strategy_id,ingested_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (ts, event, detail,
                     acct or last_account or leg_default_account or None,
                     sid or last_strategy or leg_default_strategy or None, ingested_at))
                inserted += cur.rowcount or 0
                skipped += 0 if (cur.rowcount or 0) else 1
    return {"day": day, "file": str(src), "inserted": inserted, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="掘金 audit CSV → experiments.db 采集器")
    p.add_argument("--date", help="YYYY-MM-DD（缺省=今天）")
    p.add_argument("--backfill", type=int, default=0,
                   help="回补最近 N 天（含今天）")
    p.add_argument("--register", action="store_true",
                   help="注册 QuanterEmquantIngest schtasks（每日 15:40）")
    p.add_argument("--unregister", action="store_true")
    args = p.parse_args(argv)
    if args.register:
        py = ROOT / ".venv310" / "Scripts" / "python.exe"
        rc = subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", "15:40",
                             "/TN", "QuanterEmquantIngest",
                             "/TR", f'"{py}" "{Path(__file__)}"'],
                            capture_output=True).returncode
        print("registered" if rc == 0 else f"failed rc={rc}")
        return rc
    if args.unregister:
        subprocess.run(["schtasks", "/Delete", "/TN", "QuanterEmquantIngest", "/F"],
                       capture_output=True)
        print("unregistered")
        return 0
    days = [args.date] if args.date else [
        f"{datetime.now():%Y-%m-%d}"]
    if args.backfill:
        from datetime import timedelta
        today = datetime.now().date()
        days = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(args.backfill)]
    for d in sorted(days):
        # 双腿形态（2026-08-28 双轨 §4.3）：逐腿采集当日文件——行级 account_id
        # 区分腿（_ensure_table 的唯一键含 account_id，两腿同秒同事件不再互吞）。
        for leg in gc.active_legs():
            print(json.dumps(ingest_day(d, strategy_dir=gc.leg_strategy_dir(leg)),
                             ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
