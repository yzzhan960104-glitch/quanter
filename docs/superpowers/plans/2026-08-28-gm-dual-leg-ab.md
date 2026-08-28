# 掘金双腿并行 A/B(champion–challenger)落地方案

> 日期:2026-08-28 | 状态:待执行(方案定稿,用户已裁决三项关键决策)
> 前置裁决记录(本会话):
> ① 实验账户期初资金 = **10 万**(用户裁决,替代 agent 推荐的 1000 万);
> ② 开双轨前主腿先刷新 c243da10 对齐版(消除版本差基线);
> ③ 首个双轨用例 = pos_cap 整手 A/B 校准跑(落地形态见 §6.1 分歧台账 #1)。

## 一、目标与非目标

**目标**:策略更新(candidate)先部署到实验账户验证,主账号(incumbent)零影响;验证维度 = 工程有效性 + 行为符合预期,而非收益预测。

**非目标**(明确排除):
- 不验证真实成交质量(两腿同为仿真撮合,R7c 先知调度教训已证仿真读数不可作收益证据);
- 不引入第二策略家族/新引擎(双腿同产物管线,单一变量原则);
- 不动研究面四 cron(ops_sched engine=None 路径与掘金腿无关)。

## 二、现状锚点(2026-08-28 master)

| 事实 | 锚点 |
|---|---|
| 主腿策略目录 | `C:\Users\yzzhan\.emgm3\projects\d9324346-9d1b-11f1-ae25-7c10c93fcb7d`(env `GM_STRATEGY_DIR` 可覆写) |
| 主账户 | `67334fef-a137-11f1-8228-52560acd7da0`(编译进产物 §0,build_pilot.py:129) |
| 产物管线 | build_pilot.py 五路输入幂等拼接;stamp=git 锚;指纹 7fe3d5b3;782 emquant 测试绿 |
| 三件套配置面 | ops/gm_ops_common.py:`runtime_config/audit_csv_path/state_pkl_path` 已按 strategy_dir 参数化 |
| audit 台账 | QuanterEmquantIngest 15:40 → experiment/experiments.db `terminal_audit` 表,UNIQUE(ts,event,detail) |
| 看护 | QuanterGmGuard 5min(交易窗 09:10–15:40),probe/auto_heal 按 strategy_dir.name 匹配进程 |
| 掘金多仿真账户 | 官方支持:账户管理→添加仿真账户(可多个)→连接→策略绑定 |

## 三、架构(四层隔离)

```
掘金终端(单实例 gmterm-serv,单 token 复用)
├── 主腿:projects/d9324346…/  main.py=master 产物(PILOT_ACCOUNT_ID=67334fef)
├── 实验腿:projects/<新UUID>/  main.py=实验产物(PILOT_ACCOUNT_ID=实验账户)
└── 7002 API ── 三件套+对照脚本按 LEGS 注册表分腿采集
```

隔离保证:账户层(柜台级)、目录层(部署只写实验目录)、进程层(启动/停止/自愈互不触碰;probe 按目录名匹配天然不误杀)、运维层(告警分腿标签,实验腿故障不触发主腿动作)。唯一共享物=终端进程(重启两腿齐死,既有纪律不变)。

## 四、变更清单(代码)

### 4.1 emquant/build_pilot.py — 腿参数化(产物侧唯一改动)

- `build(output_path=None, account_id=MAIN_ACCOUNT_ID)`:§0 的 `PILOT_ACCOUNT_ID` 按参数注入;stamp 追加腿后缀(`…2c85bc7d [exp]`)。
- CLI:`python emquant/build_pilot.py [--leg main|exp]`;`--leg exp` 需 `--account-id <UUID>`(缺省读 .env `GM_EXP_ACCOUNT_ID`),产出 `emquant/emquant_neckline_pilot_exp.py`(**入库**,同主产物纪律:幂等断言+部署核对+评审 diff 全依赖它在库)。
- 主腿路径行为字节不变(缺省参数=现值,主产物回归零 diff)。

**C1 语义零改动**:`_c1_guard` 仍是单值等值比较——每腿产物编译各自的账户,误交叉部署(实验产物粘主目录/反之)启动期即拒。d9324346「双策略同账户」事故形态被产物级锁死正面根治。pilot_body.py 不动(其缺省常量=主账户,直构测试语义不变)。

### 4.2 ops/gm_ops_common.py — 腿注册表(单源)

```python
@dataclass(frozen=True)
class LegDef:
    key: str; label: str                 # "main"/"exp"、"[主腿]"/"[实验腿]"
    strategy_dir_env: str; account_env: str
    default_strategy_dir: Path | None    # main=d9324346 缺省;exp=None
def active_legs() -> list[LegDef]:       # env 缺省主腿;GM_EXP_STRATEGY_DIR 未设=exp 未启用
```

- .env 新键(部署后写入,读者=三件套/ingest/归档/对照/build CLI):`GM_EXP_STRATEGY_DIR`、`GM_EXP_ACCOUNT_ID`。
- 向后兼容:`GM_STRATEGY_DIR` 保留为 main 腿覆写别名,已注册 schtasks 命令行零变更。
- exp 未启用(无 env 键)时三件套静默跳过该腿——不产生"未部署期日日报警"噪音。

### 4.3 ops 三件套+ingest+归档 — 双腿迭代

- **emquant_morning_check / gm_terminal_guard / emquant_eod_report**:主循环 `for leg in gc.active_legs()`,告警文案带 `[主腿]/[实验腿]` 标签;guard 的 auto_heal 对实验腿同样启用(relaunch ps1 实验腿版部署于实验目录,杀旧匹配按目录 UUID 隔离)。
- **emquant_audit_ingest**:`terminal_audit` 唯一键 `UNIQUE(ts,event,detail)` → **扩为 `(ts,event,detail,account_id)`**。
  ⚠️ 真雷(本方案侦察发现):两腿各自 10:31:00 写 SCHEDULE_TICK(ts 同秒+detail 同 `{probe:true}`)→ 第二腿插入撞键被吞,数据静默丢失。experiments.db 加迁移(新表+搬数,或 DROP 重建——terminal_audit 是台账非源数据,audit CSV 才是源,重建可接受,实现时按数据量定)。
- **emquant/tools/archive_terminal_state.py**:多目录发现从「退出要求 --src」改为按 LEGS 分腿归档 `emquant/archive/<leg>/<YYYY-MM-DD>/`;腿发现与 ops 注册表同源(import ops.gm_ops_common,dotenv ImportError 已有容错)。

### 4.4 ops/emquant_ab_compare.py — 日度对照(新增,schtasks QuanterGmAbCompare 15:45)

数据源=experiments.db(ingest 已分腿落库)+ 7002 双腿快照(可选)。MVP 输出(钉钉+落档):
1. **工程闸**(硬):各腿 INIT stamp/account 双锚、SCHEDULE_TICK 计数、pre_open 五阶段事件族、WARN 分类(拒单拒因分布);
2. **信号对照**:SIGNAL 行按 (symbol, entry_price) 对齐 → 新增/消失/价漂移;校准轮预期 diff=0,候选轮对照预登记 diff 预算;
3. **订单对照**(可比子集):挂单集合 diff;qty 差标注"资金口径差(预期)";
4. **绩效参考**(软):归一化 TWR,仅展示永不单独构成晋级依据。

diff 预算机制:每轮 candidate 立案时在方案/工单里预登记「允许变化面」,对照脚本将 diff 二分为预期内/预期外,**预期外=红旗**。绩效归一化与预算分类可后置到候选轮(MVP 先人工核对)。

## 五、部署 runbook

**自动化边界(2026-08-28 三轮侦察定稿,添加账户已全自动)**:
- 本机 7002(gmterm-serv,终端 token):策略/交易查询/行情/回测族 + `POST /v3/strategies`(新建策略,空 body 即创建的坑已实测记录)+ **`/v5/term/*` 账户管理面**(gmterm-serv 代理 walle-v5):`create-account` 一体化创建(账户+通道+资金+撮合)。CLI=`emquant/tools/gm_sim_account.py`。
- **实验账户已建(08-28 20:19,零 GUI)**:`c4ba3b2e-a2da-11f1-9262-52560acd7da0`(title=实验腿,init_cash=100000,engine=simulate,channel=gm-broker-1 与主账户同款,login 已激活)。**仿真账户配额=2 已满**(主+实验),再建须先删。
- CDP 判死(改造壳带 --remote-debugging-port 启动即挂死,实测);GUI 像素点击死路(既有结论);键盘 SendKeys 抢焦点不满足无人值守。
- **终端重启=登录人闸**(实测:杀终端重启后弹密码窗等人工,gmterm-serv/7002 全挂起)——部署 runbook 涉及重启处已提示;看护自愈的终端级恢复场景同样受制;建议终端勾「自动登录」。

**用户手动:已归零**(账户创建的 GUI 步骤被 `/v5/term/account/create-account` 替代;策略目录/main.py/runtime.json/relaunch 全 agent 侧)。

**agent 代做(全部已验证或既有模式)**:
1. `POST /v3/strategies {name:"neckline_exp", language:"python"}`(本机 token)→ 得新 strategy_id 与目录;
   ⚠️ 该路由**空 body 也会创建**(2026-08-28 探测实锤,当场创建+删除恢复)——调用必须带完整字段,探测变更路由禁用空 body;
2. `build --leg exp` → 产物直接文件写入 `<新目录>/main.py`(同机文件系统,免粘贴);
3. `config/runtime.json`:token 从主腿复用(**勿重生成**)、strategy_id=新、account_id=实验账户;
4. `relaunch_strategy.ps1` 实验腿版(纯 ASCII,参数复刻终端命令行;杀旧匹配按目录 UUID 隔离,不误伤主腿);
5. `.env` 写 `GM_EXP_STRATEGY_DIR`/`GM_EXP_ACCOUNT_ID` 两键;
6. 主腿刷新(裁决②):master 主产物写入 d9324346 目录 → relaunch(避开交易时段)→ 验 INIT stamp;
7. 启动后必验:双进程数=1×2 + 两腿 audit 各自 INIT 行(stamp/account 双锚)。
   注:relaunch 拉起(非终端 UI「运行」)时 `context.accounts` 可能空表 → 既有 `bootstrap_accounts_empty` WARN 留痕,晨检可见非阻断(d9324346 同款形态)。

**每轮 candidate 复用**:实验策略目录/账户/relaunch 全部复用,换 main.py 重启即可——首轮之后零 GUI、零新增手工。

## 六、验证协议

### 6.1 首轮 = 零变量校准(设计偏离留档)

两腿部署**完全相同**的 master 参数(0.075),仅账户/目录不同。通过标准:≥10 交易日信号 diff=0、双工程闸全绿、订单流 diff 仅 qty 归因于资金差。此后候选轮的 diff 才可唯一归因于变更本身。

**分歧台账 #1**:用户裁决③原文为「pos_cap A/B(A=0.05 维持/B=0.075)双侧」;结合裁决②(主腿刷新至 0.075)落地为「B 侧零变量校准」。理由:A 侧行为(10 万×0.05=5000/单→>50 元标的零整手)是确定性数学,无需实验证明;首轮同时引入新账户+新参数两变量将使 diff 不可归因。若需 A 侧实证,第二轮以 0.05 产物跑实验腿即可(变更是「实验腿换产物」,主腿仍零感知)。

### 6.2 候选轮(candidate 上腿)

- **快道**(纯工程修复):3–5 交易日工程闸全绿即晋级;
- **慢道**(参数/信号变更):15 交易日 + 信号 diff 归因完毕(对照预登记预算)+ 研究面预登记闸(fresh_window 纪律:成熟 600 笔/15 日口径由研究面承担,实验腿不重复)。
- **晋级** = 唯一触碰主腿的动作:candidate 产物粘主目录 + 重启(离散决策,用户裁决);
- **回退** = 停实验腿进程,主腿零感知;实验账户可清仓重置进下一轮。

### 6.3 10 万资金行为边界(裁决①推演,立案留档)

pos_cap 0.075 → 单笔 7,500 元:entry<37.5 元→2 手;37.5–75 元→1 手;**>75 元→零整手不挂单(信号仍产生并落 audit)**。UNIVERSE 中科创板高价股部分出局订单面——信号对照完整,订单/绩效对照按可比子集解读。此边界恰是首轮校准要采集的行为数据(为未来小资金实盘提供参数依据)。

## 七、测试计划

| 面 | 用例 |
|---|---|
| test_kernel_equivalence | `build(--leg exp)` 幂等;实验产物 PILOT_ACCOUNT_ID=实验账户+stamp 带 `[exp]`;主产物字节不变(回归) |
| test_events_orchestration | 从实验产物文件 import,断言其 `_c1_guard(主账户ID)` 拒绝、`_c1_guard(实验账户ID)` 放行 |
| ops 新增 | LEGS 注册表/env 覆写/exp 未启用跳过;ingest 双腿同秒同事件两行均在(唯一键扩展回归) |
| 对照脚本 | 纯函数:fixture audit 行 → 信号 diff/工程闸计数 |
| 门槛 | 后端 2286 + emquant 782 全绿 |

## 八、时间线

- **D0(盘后)**:§4 代码+测试(单 session);用户 runbook 步骤 1–4;agent 代写三件;主腿刷新;
- **D0 晚**:实验腿首启(常驻过夜,等 D1 09:31 pre_open 定时);
- **D1–D10+**:校准轮,15:45 对照日报;
- 校准通过 → 首个候选轮(届时按 6.2 立案预登记)。

## 九、风险清单

| 风险 | 处置 |
|---|---|
| ingest 唯一键撞腿吞数据 | §4.3 键扩展(方案内修复,非遗留) |
| 双策略同账户(d9324346 事故形态) | C1 产物级账户锁死,误交叉部署启动期即拒 |
| 终端重启两腿齐死 | 既有纪律(避开交易时段),不变 |
| 高价股零整手(10 万口径) | §6.3 立案,订单对照按可比子集 |
| CPU/带宽 ×2 | 单腿轻量 IO 进程,可接受;长跑 diag 发射前查活跃进程(既有纪律) |
| .env 死键 | 两键读者明确(§4.2),回滚时随腿一起摘 |

## 十、回滚

停实验腿进程(终端 stop/杀进程)→ 删 .env 两键(三件套回落单腿)→ 实验产物与 schtasks 可留(零读者零成本)或摘除。全程主腿零感知。
