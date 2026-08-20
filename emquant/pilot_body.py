# -*- coding: utf-8 -*-
"""§2-§7 执行编排（pilot_body）——单文件的业务躯干（Task 5-8 逐节增补）。

物理定位：
    本文件是【源】而非生成物：build_pilot.py 把它整段拼到组装单文件的 §2-§7 区
    （§0 参数区/§1 识别内核在前）。试点执行编排按任务波次落进来后重跑组装器。

拼接纪律（Why——单文件位次约束，违者 SyntaxError 或 C2/C4 红线事故）：
    - 禁 `from __future__ import ...`：future import 只允许出现在组装文件顶部
      （head 已放），本段拼在模块中部，再出现即 SyntaxError；
    - 禁相对 import（`from .xxx import`）：单文件无包结构，相对 import 必炸；
    - 禁顶层 `import gm`（C4）：gm 只允许函数体内惰性 import——保证无 gm 环境
      可完整 import 单文件跑识别内核等价性测试（本任务阶段天然无 gm，纪律先行）；
    - 只许标准库（C3 同源）：不 import 仓库任何模块——单文件部署到掘金终端后无
      仓库上下文，任何仓库依赖都当场 ImportError。本段 §3/§4 全部 stdlib 实现。
"""
import csv
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path


# ============================ §3 状态层（state.pkl 原子读写 + 人工风控双值文件）============================
# 物理布局（设计 §3：掘金腿没有 DB/钉钉/台账，state.pkl + 两个人工可编辑文件就是全部持久面）：
#   <BASE_DIR>/state/state.pkl         策略唯一持久状态（schema v1，见 _initial_state）
#   <BASE_DIR>/state/RISK_BLOCK.flag   人工风控开关（存在即拦增量；ADR-16 block_new_orders 的文件化身）
#   <BASE_DIR>/state/CAP.txt           人工仓位上限（ADR-16 max_total_position 的文件化身，缺省 1.0）
#   <BASE_DIR>/audit/audit_YYYYMMDD.csv 对拍审计（本地腿同机直接读，§7 逐行补全事件族）
# Why flag/cap 放 state/ 内：三件都是「运行时真值」，落 gitignore 的 state/ 目录，
# 仓库工作区零污染；人工触达路径（touch/编辑）由 README runbook 钉死。
BASE_DIR = Path(__file__).resolve().parent   # 产物自定位：仓库内跑落 emquant/{state,audit}/，
STATE_DIR = BASE_DIR / "state"               # 部署到掘金终端策略目录时自动落策略文件旁（零配置）
AUDIT_DIR = BASE_DIR / "audit"
CONFIG_DIR = BASE_DIR / "config"             # runtime.json（token/strategy_id/account）——Task 8 读
STATE_FILE = STATE_DIR / "state.pkl"         # 文件名沿用设计文档；内容是 JSON（见 save_state 的 Why）
RISK_BLOCK_FLAG = STATE_DIR / "RISK_BLOCK.flag"
CAP_FILE = STATE_DIR / "CAP.txt"


def _initial_state() -> dict:
    """state.pkl schema v1 的空白态（缺文件/首启时的落点）。

    schema v1（设计 §3，冻结——load_state 见到别的 version 一律拒载）：
        version    int      schema 版本号（未来迁移的哨兵，防静默猜结构）
        scan_done  set[str] 当日已扫描标记（"YYYY-MM-DD"，幂等防重扫——识别本身是纯
                          函数重扫零风险，重扫只产生重复 audit 噪声行）
        placed     dict[str, list[str]] {date: [cl_ord_id, ...]} 当日已挂（幂等防重挂，
                          对齐本地 has_order(OPEN)+UNIQUE 的防重意图，cl_ord_id 是订单主键）
        orders     dict[str, dict] {cl_ord_id: {symbol,date,price,qty,purpose,
                          exec_params,status,...}}（Task 7 生命周期判定消费）
        positions  dict[str, dict] {symbol: {entry_date,entry_price,qty,remaining_qty,
                          stop,tp1_price,tp1_done,tp2_price,trailing:{...},exec_params}}
    """
    return {"version": 1, "scan_done": set(), "placed": {}, "orders": {}, "positions": {}}


def load_state(path=None) -> dict:
    """读 state.pkl → 内存 dict；缺文件返空白态（首启语义）。

    反序列化规则：scan_done 在盘上是 JSON list（set 非 JSON 原生），载入即回转 set；
    placed/orders/positions 为 JSON 原生结构原样返回。

    Why 损坏即抛（fail-loud 而非静默重置）：save_state 的 tmp+rename 原子写已消灭
    「写到一半崩溃」这一唯一常态损坏源——文件仍坏只可能是磁盘损坏或人工误编辑。
    此时静默回空白态会把 orders/positions 一并抹掉 = 持仓裸奔（无止损管理）；
    抛错让策略停在启动期，人工核对柜台实况后处置（对齐本地引擎「宁停不裸奔」
    基调，同 pre_open DB 幂等读失败即 _CriticalHalt 的取舍）。version 不等 1
    或缺 v1 必备键同抛：schema 冻结 v1，未来升版必须显式迁移，绝不静默猜。

    可选 path 参数：默认 STATE_FILE；测试/编排传显式路径实现隔离（不污染真值区）。
    """
    p = Path(path) if path is not None else STATE_FILE
    if not p.exists():
        return _initial_state()
    raw = json.loads(p.read_text(encoding="utf-8"))            # 损坏 → JSONDecodeError 上抛（fail-loud）
    if not isinstance(raw, dict) or raw.get("version") != 1:
        seen = raw.get("version") if isinstance(raw, dict) else type(raw).__name__
        raise RuntimeError(f"state.pkl schema 异变（version={seen!r} != 1，冻结 v1 须显式迁移）：{p}")
    missing = [k for k in ("scan_done", "placed", "orders", "positions") if k not in raw]
    if missing:
        raise RuntimeError(f"state.pkl 缺 schema v1 必备键 {missing}（文件不完整，须人工核对）：{p}")
    raw["scan_done"] = set(raw["scan_done"])                   # 盘上 list → 内存 set（成员测 O(1)，防重语义天然）
    return raw


def save_state(state: dict, path=None) -> None:
    """state 落盘（tmp+rename 原子写，设计 §3 写入纪律）。

    Why 原子写：进程在写入中途被杀/断电，若直接覆写目标文件，崩溃点落在半途 =
    state 损坏 = 持仓/挂单记忆全丢。先写同目录临时文件、fsync、再 os.replace
    原子改名（同目录保证同一文件系统，rename 不可分割），消灭整类恢复场景。
    replace 失败（断电/被占用）时清理临时文件后原样上抛——不留垃圾 tmp（试点
    目录人工晨检要看，残留 .tmp 会当事故排查半天），也绝不吞错假成功。

    序列化（JSON 而非 pickle，尽管文件名叫 state.pkl）：pickle 对象绑定 Python
    版本/类路径，掘金终端与仓库的 Python 版本不保证一致；JSON 文本人工可检视、
    可跨版本手工修复，且 scan_done 的 set→list 转换显式可控。落盘前浅拷贝转换
    （不改动调用方 state 对象——编排层手里还持有它继续跑当轮）；scan_done 排序
    落盘保证同状态两次落盘字节一致（人工 diff/未来对拍友好）。
    """
    target = Path(path) if path is not None else STATE_FILE
    payload = dict(state)                                      # 浅拷贝：只改写 scan_done 一个键
    payload["scan_done"] = sorted(state.get("scan_done") or ())
    payload.setdefault("version", 1)                           # 兜底：防手工构造的 dict 落出无版本文件
    target.parent.mkdir(parents=True, exist_ok=True)           # 首启目录不存在就地创建（终端冷启动）
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))   # 值不可序列化 → TypeError 上抛（写侧免费校验）
            f.flush()
            os.fsync(f.fileno())                               # 先刷盘再改名：封掉「rename 成功而数据仍在页缓存」的断电窗口
        os.replace(tmp_name, target)                           # 原子改名（Windows MoveFileEx REPLACE_EXISTING 同语义）
    except BaseException:
        try:
            os.unlink(tmp_name)                                # 失败清理残 tmp（晨检目录干净）
        except OSError:
            pass                                               # 清理失败无害：tmp 不会被任何读路径触达
        raise


def audit_log(event: str, audit_dir=None, **fields) -> None:
    """对拍审计逐行追加（audit_YYYYMMDD.csv）——§7 审计事件的物理写入原语在此先落位。

    行格式恒定三列：ts,event,detail（detail 为排序 JSON 字段包）。Why 塞 detail
    而非逐字段开列：审计事件族（CAP WARN/信号/挂单/撤单/成交/巡检动作……Task 8
    逐个补）键值不定，三列结构让 Excel 人工复核与文本 grep 两头都好使，新事件
    零迁移。Why CSV 而非 JSONL：设计 §2 降级清单明示「audit CSV 每日人工复核
    （替代钉钉 CRITICAL 链）」——晨检工具是 Excel/文本编辑器，CSV 是最大公约数。
    追加模式（审计行只增不改，完整性优先于整洁；崩溃留半行人工可辨，不做原子写）。
    """
    d = Path(audit_dir) if audit_dir is not None else AUDIT_DIR   # 调用时查模块常量：monkeypatch 即生效
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"audit_{date.today():%Y%m%d}.csv"                 # 按日分文件：晨检只看当天、复核期自然滚动
    with p.open("a", encoding="utf-8", newline="") as f:       # newline=""：csv 模块接管换行（Windows CRLF 可控）
        csv.writer(f).writerow([
            datetime.now().isoformat(timespec="seconds"), event,
            json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str)])


def is_blocked(path=None) -> bool:
    """人工风控开关（ADR-16 block_new_orders 的文件化身）：RISK_BLOCK.flag 存在即 True。

    Why 只看存在性不看内容：开关的触达方式就是人工 touch/rm（回退 SOP 第一步），
    任何内容语义（0/1/off）都给「着急拦单」多加一步出错机会。与本地
    pre_open.py:478 同口径：block 只拦【增量】（挂新买单整体跳过），存量管理
    （撤昨日单/超期平仓/trailing）照常执行——编排层（Task 8 pre_open 阶段④）
    在「扫描后、挂单前」调用本函数，为 True 跳过挂单段而非整个 pre_open。
    """
    p = Path(path) if path is not None else RISK_BLOCK_FLAG
    return p.exists()


def read_cap(path=None) -> float:
    """人工仓位上限（ADR-16 max_total_position 的文件化身）：读 CAP.txt → [0.0, 1.0]。

    两分支刻意不对称（与本地 state_store.py:2016 resolve_risk_control 同口径）：
      - 缺文件 → 1.0（不限制）+ audit WARN：CAP.txt 是可选收紧项，试点默认态就是
        「不设总仓位上限」（另有单日 ≤2 + 单票 ≤5% 两道试点硬闸兜底）——缺文件是
        正常态不是事故，但 WARN 留痕让晨检能发现「以为设了 0.5 其实文件没了」的
        静默失效；
      - 内容非法（非数字/越界 [0,1]/NaN/不可读）→ 0.0（全拦）+ audit WARN：
        人工想收紧却写错（典型：把 50% 写成 50）时，最坏组合是系统静默按 50.0
        放行——fail-closed，宁可误拦人工改文件。与本地「值损坏视同 0 全跳」
        （I-4）逐字同义。
    NaN 防渗透：float('nan') 参与链式比较 0<=v<=1 恒 False，反向写法
    `not (0<=v<=1)` 恰把 nan 归入非法支（正向写法 `v<0 or v>1` 拦不住 nan）。
    """
    p = Path(path) if path is not None else CAP_FILE
    if not p.exists():
        audit_log("WARN", type="cap_missing", msg=f"CAP.txt 缺失，按缺省 1.0（不限制总仓位）运行：{p}")
        return 1.0
    try:
        v = float(p.read_text(encoding="utf-8").strip())       # strip：容忍人工编辑的首尾空白/换行
    except (OSError, ValueError):
        audit_log("WARN", type="cap_invalid", msg=f"CAP.txt 不可读/非数字，视同 0（全拦）fail-closed：{p}")
        return 0.0
    if not (0.0 <= v <= 1.0):                                  # 链式比较把 NaN 也判 False → 非法支
        audit_log("WARN", type="cap_invalid", msg=f"CAP.txt 值 {v!r} 越界 [0,1] 或 NaN，视同 0（全拦）：{p}")
        return 0.0
    return v


# ============================ §4 风控闸（ADR-16 双值 + 试点三硬闸合一预检）============================
def check_caps(sod_state, equity, positions_mv, open_buy_amount, price, qty, today, cap=None):
    """挂单前三闸合一预检（纯函数：不落盘、不查柜台——一切实况由调用方喂入）。

    闸序（任一不过即返 (False, 中文原因)，全过返 (True, "")；原因字符串直接进
    audit，人工晨检靠它定位是哪道闸拦的）：
      ① fail-closed 前置：equity/positions_mv/open_buy_amount 任一为 None，或
         equity<=0，或本单金额<=0 → 拒。上游查询失败（gm get_cash/get_positions
         拉取异常**静默返空**——Task 2 核对结论）必须显式传 None 进来，绝不拿
         0 冒充真值；「不知道占多少时宁可多拦不可盲放」对齐 pre_open.py:508-539
         的三查任一失败全跳。NaN 防渗透：判空判负全用反向写法 not (x>0)，nan
         参与比较恒 False → 自然落拒（正向写法拦不住 nan）。
      ② CAP 总仓位额度：本单金额 ≤ equity×CAP − 持仓市值 − 未终态买单占额。与
         pre_open.py:540 同式（`总权益×比例 − market_value − open_buy`）；其中
         open_buy_amount 口径与 state_store.py:1470 get_open_buy_amount 同义——
         未终态 buy 委托按 (qty−filled)×price 合计（成交部分已体现在持仓市值不
         重复扣；卖单是退出方向不占增量额度）。「逐单扣减」由调用方承担：每挂出
         一单把本单金额累进 open_buy_amount 再喂下一单（对齐 pre_open 循环侧）。
      ③ 单日新挂 ≤ PILOT_MAX_NEW_ORDERS_PER_DAY（试点硬闸 FR3，§0 写死 2）：
         当日 placed 已达上限 → 拒（试点期规模闸，验收后可放开）。
      ④ 单票金额 ≤ PILOT_MAX_POSITION_PCT×equity（试点硬闸 FR3，§0 写死 5%）：
         一单一票，单票新增敞口即本单金额；同票次日补挂的聚合敞口由 ② 总闸兜底。
         挂单量公式 qty=⌊equity×pos_cap/entry/100⌋×100 本就按 5% 定尺，本闸是
         对「定尺漂移/人工误用」的二次核验。
    恰等边界一律放行：② 与 ④ 的比较均用严格 >（对齐 pre_open.py:602
    `_order_amt > _pos_quota` 才拦——恰好吃满额度是合法满仓，不是违规）。
    cap 参数：默认 None → 现场读 CAP.txt（read_cap 内嵌 WARN 审计留痕）；测试与
    编排层可显式注入数值，免文件依赖。
    """
    # ① fail-closed 前置：先归一再判（None 守卫在前，防 float(None) 直接 TypeError；
    #    数值脏成非数字串则 float() 抛错上抛——编排层 bug 该炸就炸，不静默吞）
    eq = None if equity is None else float(equity)
    mv = None if positions_mv is None else float(positions_mv)
    ob = None if open_buy_amount is None else float(open_buy_amount)
    amount = float(price) * float(qty)
    if eq is None or mv is None or ob is None or not (eq > 0):
        return False, (f"fail-closed：权益/持仓/挂额查询不完整（equity={equity!r} "
                       f"positions_mv={positions_mv!r} open_buy={open_buy_amount!r}），当日不挂")
    if not (amount > 0):
        return False, f"委托参数残缺（price={price!r} qty={qty!r}，金额非正），拒挂"
    # ② CAP 总仓位额度（pre_open.py:540 同式；open_buy 口径 state_store.py:1470 同义）
    c = read_cap() if cap is None else float(cap)
    quota = eq * c - mv - ob
    if amount > quota:
        return False, (f"总仓位额度不足：本单 {amount:.2f} > 余量 {quota:.2f}"
                       f"（equity×CAP{c:g}−持仓{mv:.2f}−已挂{ob:.2f}）")
    # ③ 单日新挂上限（试点硬闸 FR3）
    placed_today = sod_state.get("placed", {}).get(today, [])
    if len(placed_today) >= PILOT_MAX_NEW_ORDERS_PER_DAY:
        return False, (f"单日新挂已达试点上限 {PILOT_MAX_NEW_ORDERS_PER_DAY}"
                       f"（placed[{today}] 共 {len(placed_today)} 单）")
    # ④ 单票金额上限（试点硬闸 FR3）
    sym_cap = PILOT_MAX_POSITION_PCT * eq
    if amount > sym_cap:
        return False, f"单票金额 {amount:.2f} 超试点上限 {PILOT_MAX_POSITION_PCT:.0%}×equity={sym_cap:.2f}"
    return True, ""


def run_pilot():
    """试点主入口（Task 8 实现：五阶段事件编排——预开/开盘/盘中巡检/收盘/盘后）。"""
    raise NotImplementedError("Task 8 实现")


if __name__ == "__main__":
    run_pilot()
