# -*- coding: utf-8 -*-
"""gm 腿对拍取数器（东财掘金颈线单文件试点 Task 10 产物）。

Why 存在：W0' 数据对拍需要 gm（东财源）与 data_lake（tushare 源）在同一
「定点复权锚」下各拉一份同窗日线，逐列比数值——两条数据链路的因子精度、
除权事件时点、源端口径任何一处分叉都会在这里现形。对拍不通过的标的进
排除池（W2 一致率分母豁免，设计 §7），对拍工具链本身是试点数据可信度的
地基，而非锦上添花的观测。

运行环境（严格双环境纪律，禁向任一 venv 装包）：
    PYTHONUTF8=1 E:/quanter/.venv_emquant/Scripts/python.exe emquant/tools/gm_data_pull.py
    （本脚本 import gm——只能在 .venv_emquant 跑；对拍另一腿 compare_data.py
     读 data_lake parquet 须 pyarrow，只能在 .venv310 跑。两步分环境执行，
     中间产物落 emquant/state/ 交接。）

前置（缺一即快速失败，中文诊断指路 runbook）：
    1) emquant/config/runtime.json 的 token 为终端「系统管理-密钥管理」生成的
       真实 token（C8：token 是鉴权凭据，只许活在 runtime.json——gitignore 已
       钉死；本脚本全程只读不说，任何输出只出现长度元信息，绝不出现值）；
    2) 掘金终端数据服务 gmterm-serv 在线（默认 127.0.0.1:7001）；
    3) 锚表 emquant/state/parity_anchors_YYYYMMDD.json 已生成（先在 .venv310
       跑 compare_data.py anchors——每标的取 data_lake 内末日为定点复权锚，
       两腿同锚才可比：前复权数值是「锚日」的函数，锚不同则数值必不同）。

产物：
    emquant/state/parity_gm_YYYYMMDD.csv   （universe 逐标的尾部 2×window+20 根
        定点前复权日线；MultiIndex(date,symbol)+open/high/low/close/volume，
        symbol 为 ts 口径——与 data_lake 同构，compare 直接 join。Why csv 而
        非任务原文的 parquet：.venv_emquant 无 pyarrow/fastparquet 且禁装包，
        csv 是 pandas 双端零依赖的等价载体；to_csv 的 float 走最短往返 repr，
        精度无损，1e-6 容差不受影响。若未来该 venv 装了 parquet 引擎，脚本
        自动改写 .parquet（--format 可钉死）。）
    emquant/state/parity_pull_fail_YYYYMMDD.json （逐标的失败清单：gm 异常/空
        结果——晨检先看这份再下「源不可用」结论。）
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# 工具脚本与 pilot_body 同住 emquant/，插入父目录后复用其符号映射——对拍腿与
# 试点主链路共用同一份 ts↔gm 换装逻辑（单源纪律：映射散两处必漂移）。
_TOOLS_DIR = Path(__file__).resolve().parent
_EMQUANT_DIR = _TOOLS_DIR.parent
if str(_EMQUANT_DIR) not in sys.path:
    sys.path.insert(0, str(_EMQUANT_DIR))

from pilot_body import to_gm_symbol  # noqa: E402 （须在 sys.path 注入后）

STATE_DIR = _EMQUANT_DIR / "state"
CONFIG_DIR = _EMQUANT_DIR / "config"
RUNTIME_JSON = CONFIG_DIR / "runtime.json"

# 交易日历推导源指数（与 pilot_body._CALENDAR_INDEX 同值保持同源：指数每个交易
# 日必有 bar，探针拿它验证「服务在线 + token 过鉴权」两件事一次完成）
_PROBE_SYMBOL = "SHSE.000300"

# 连接探针回看窗：10 自然日必含 ≥5 个交易日 bar，窗小快速失败、窗大无谓等待
_PROBE_LOOKBACK_DAYS = 10

# 取数回看自然日窗：与 pilot_body._FETCH_LOOKBACK_DAYS 同值同由——500 自然日 ≈
# 340 交易日 >> 目标 180 根，长停牌/次新也有余量；「有多少返多少」不硬凑根数
_FETCH_LOOKBACK_DAYS = 500


def _die(code: int, msg: str) -> None:
    """快速失败统一出口：中文诊断 + 非零退出码（runbook/晨检按码分流）。

    Why os._exit 而非 sys.exit：实测 gm 的 C 扩展会接管进程退出码（import gm 后
    sys.exit(3) 的 shell 侧拿到 0）——非零码是晨间补跑脚本的分流依据，被吞即
    降级判定链断链；os._exit 跳过 gm 的退出钩子，代价是不走 atexit 清理（本
    工具无待清理资源：数据已落盘/无连接须手工关闭），先 flush 再走。
    """
    print(f"[gm_data_pull] 终止：{msg}", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def _load_token() -> str:
    """读 runtime.json 的 token（C8：值不出现在任何输出/日志/异常文本）。"""
    try:
        cfg = json.loads(RUNTIME_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _die(2, f"缺 {RUNTIME_JSON}——按 README runbook 创建（token/strategy_id/account_id 三键）")
    token = str(cfg.get("token") or "").strip()
    if not token:
        _die(2, f"{RUNTIME_JSON} 的 token 为空——须在掘金终端「系统管理-密钥管理」生成后填入再重跑")
    return token


def _probe_connection(gm_api, token: str) -> None:
    """连接+鉴权探针：拉指数日线 1 根即证明「终端在线且 token 过鉴权」。

    分流诊断（夜间实测主障碍面，逐类给中文指路）：
        - status 1000「错误或无效的token」：链路通而凭据拒——token 是模板
          示例值/已作废，去终端重新生成（生成动作只能在终端 UI 完成，C8）；
        - 连接类异常（Connection/timeout/1001 段）：gmterm-serv 不在线——
          检查 7001 端口与终端进程，不要试图由脚本拉起 GUI；
        - 其他 GmError：原样透出 message 供晨检定位（message 不含 token 值）。
    """
    gm_api.set_token(token)
    print(f"[gm_data_pull] set_token OK（token 长度 {len(token)}，值不显示）", flush=True)
    start = (datetime.now() - timedelta(days=_PROBE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    try:
        df = gm_api.history(symbol=_PROBE_SYMBOL, frequency="1d", start_time=start,
                            end_time=datetime.now().strftime("%Y-%m-%d"),
                            fields="eob", df=True)
    except Exception as e:  # GmError 与连接异常在此分流
        msg = str(e)
        if "1000" in msg or "无效的token" in msg or "token" in msg.lower():
            _die(3, "token 无效（服务端 status 1000 拒绝鉴权）——token 是模板示例值或已作废；"
                    "到掘金终端「系统管理-密钥管理」重新生成并粘贴进 runtime.json 后重跑")
        _die(4, f"gmterm-serv 连接失败（127.0.0.1:7001）——先确认掘金终端已启动（本脚本不代启 GUI）；"
                f"原始异常：{type(e).__name__}: {msg[:200]}")
    if df is None or len(df) == 0:
        _die(4, "探针返回 0 行（服务应答但无数据）——检查终端登录态与数据服务配置后重试")
    print(f"[gm_data_pull] 连接探针 OK（{_PROBE_SYMBOL} 近 {_PROBE_LOOKBACK_DAYS} 自然日 {len(df)} 根）", flush=True)


def _latest_anchors(explicit: str) -> dict:
    """定位锚表：显式路径优先，否则取 state/ 下日期最新的 parity_anchors_*。"""
    if explicit:
        p = Path(explicit)
        if not p.exists():
            _die(5, f"锚表不存在：{p}——先在 .venv310 跑 "
                    f"compare_data.py anchors 生成（每标的以 data_lake 末日为定点复权锚）")
        return json.loads(p.read_text(encoding="utf-8"))
    cands = sorted(STATE_DIR.glob("parity_anchors_*.json"))
    if not cands:
        _die(5, "缺锚表（emquant/state/parity_anchors_*.json）——先在 .venv310 跑：\n"
                "  PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/compare_data.py anchors")
    p = cands[-1]  # 文件名含 YYYYMMDD，字典序即时间序
    anchors = json.loads(p.read_text(encoding="utf-8"))
    print(f"[gm_data_pull] 锚表：{p.name}（{len(anchors)} 标的）", flush=True)
    return anchors


def _pull_one(gm_api, ts_symbol: str, anchor: str, n_roots: int):
    """单标的定点前复权日线 → (date, ohlcv DataFrame) 或 None（异常/空容错）。

    取数口径与 pilot_body.fetch_df_upto 逐参对齐（对拍命门，改动须两处同步）：
    adjust=ADJUST_PREV + adjust_end_time=锚日——前复权曲面由 [上市, 锚] 区间的
    除权事件唯一决定，幂等可重放；skip_suspended=True——停牌日无 bar，与本地腿
    tushare 行集同构；fields 不含 amount（对拍只比 OHLC 四列，amount 口径两源
    本就不同义：东财元 vs tushare 千元）。
    """
    start = (datetime.strptime(anchor, "%Y-%m-%d") - timedelta(days=_FETCH_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    raw = gm_api.history(symbol=to_gm_symbol(ts_symbol), frequency="1d",
                         start_time=start, end_time=anchor,
                         fields="eob,open,high,low,close,volume",
                         skip_suspended=True, fill_missing=None,
                         adjust=gm_api.ADJUST_PREV, adjust_end_time=anchor, df=True)
    if raw is None or len(raw) == 0:
        return None
    out = raw[["open", "high", "low", "close", "volume"]].copy()
    # gm df=True 时 eob 既可以是列也可以是 index（SDK 版本相关）：统一折 YYYY-MM-DD
    # 字符串序列做 index——日线 eob 的时分秒（15:00 或零点，两种实测都见过）不参与
    # 对拍，日期粒度即两腿公共锚（pilot_body.fetch_df_upto 的 normalize 同义）
    eob = raw["eob"] if "eob" in raw.columns else raw.index
    out.index = [pd.Timestamp(t).strftime("%Y-%m-%d") for t in pd.to_datetime(eob)]
    return out.tail(n_roots)


def main() -> None:
    ap = argparse.ArgumentParser(description="gm 腿对拍取数器（详见模块 docstring）")
    ap.add_argument("--anchors", default="", help="锚表路径（缺省取 state/ 下最新 parity_anchors_*.json）")
    ap.add_argument("--format", choices=["auto", "csv", "parquet"], default="auto",
                    help="落盘格式（auto：有 parquet 引擎用 parquet，否则 csv——当前 .venv_emquant 无引擎）")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    # ── 1) token + 连接探针（快速失败在前，不烧 300 只取数时间）────────────────
    import gm.api as gm_api  # 探针前才导入：让 token/环境类错误先给中文出口
    _probe_connection(gm_api, _load_token())

    # ── 2) 锚表 + universe + 根数窗 ────────────────────────────────────────────
    anchors = _latest_anchors(args.anchors)
    universe = json.loads((CONFIG_DIR / "universe.json").read_text(encoding="utf-8"))["symbols"]
    params = json.loads((CONFIG_DIR / "params_snapshot.json").read_text(encoding="utf-8"))
    window = int(params["id_params"]["window"])           # §0 快照 window=80
    n_roots = 2 * window + 20                             # 180 根：识别窗+预热冗余
    # 锚表与 universe 的差集处理：无锚标的（lake 缺数）进失败清单而非静默跳过——
    # 对拍分母必须可对账，「少了谁」本身是晨检要看的信号
    targets = [s for s in universe if s in anchors]
    no_anchor = [s for s in universe if s not in anchors]
    print(f"[gm_data_pull] universe {len(universe)} 只，有锚 {len(targets)} 只，"
          f"无锚（lake 缺数）{len(no_anchor)} 只；每标的取尾部 {n_roots} 根", flush=True)

    # ── 3) 逐标的取数（容错续跑 + 每 30 只进度）───────────────────────────────
    frames, fails = {}, []
    for i, sym in enumerate(targets, 1):
        try:
            df = _pull_one(gm_api, sym, anchors[sym], n_roots)
            if df is None:
                fails.append({"symbol": sym, "reason": "empty", "anchor": anchors[sym]})
            else:
                frames[sym] = df
        except Exception as e:  # 单标的 gm 异常不炸整轮：记录续跑，晨检看清单
            fails.append({"symbol": sym, "reason": f"{type(e).__name__}: {str(e)[:160]}",
                          "anchor": anchors[sym]})
        if i % 30 == 0 or i == len(targets):
            print(f"[gm_data_pull] 进度 {i}/{len(targets)}（成功 {len(frames)}，失败 {len(fails)}）", flush=True)
    for sym in no_anchor:
        fails.append({"symbol": sym, "reason": "no_anchor_in_lake", "anchor": None})

    if not frames:
        _die(6, f"全部 {len(targets)} 只取数失败——服务/口径级问题，看上方异常摘要与终端日志")

    # ── 4) 落盘（与 data_lake 同构：MultiIndex(date,symbol)+五列，ts 符号）─────
    data = []
    for sym, df in frames.items():
        part = df.reset_index()
        part.columns = ["date"] + list(df.columns)
        part.insert(1, "symbol", sym)      # ts 口径落盘（对拍两腿各持符号纪律的 gm 侧例外：中间产物按本地腿口径，compare 免换装）
        data.append(part)
    out_df = pd.concat(data, ignore_index=True).set_index(["date", "symbol"]).sort_index()

    fmt = args.format
    if fmt == "auto":
        try:
            import pyarrow  # noqa: F401
            fmt = "parquet"
        except ImportError:
            fmt = "csv"
    out_path = STATE_DIR / f"parity_gm_{today}.{fmt}"
    if fmt == "parquet":
        out_df.to_parquet(out_path)
    else:
        # to_csv 的 float 输出走最短往返 repr：读回与内存值逐位相等，1e-6 容差无损
        out_df.to_csv(out_path, encoding="utf-8")
    fail_path = STATE_DIR / f"parity_pull_fail_{today}.json"
    fail_path.write_text(json.dumps(fails, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[gm_data_pull] 完成：成功 {len(frames)}/{len(universe)}，共 {len(out_df)} 行", flush=True)
    print(f"[gm_data_pull] 产物：{out_path}", flush=True)
    print(f"[gm_data_pull] 失败清单：{fail_path}（{len(fails)} 条）", flush=True)
    print("[gm_data_pull] 下一步（.venv310）：PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe "
          "emquant/tools/compare_data.py compare", flush=True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # 兜底：完整栈落 stderr 供定位，但不让静默成功假象流出
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)  # 同 _die：gm C 扩展吞非零码，显式绕过（见 _die 头注）
