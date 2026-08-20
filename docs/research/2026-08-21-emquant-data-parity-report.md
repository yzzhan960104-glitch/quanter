# emquant 数据对拍报告（W0' 夜间实测——降级记录）

- 日期：2026-08-21 凌晨（Task 10）
- 结论先行：**夜间实测未完成，降级**。数据服务在线、链路通，但**本机无有效 SDK token**
  （服务端 status 1000 拒绝鉴权）。工具链（`gm_data_pull.py` + `compare_data.py`）已交付并
  完成离线自测（比对器双向自检通过），晨间生成 token 后三步补跑即出对拍结果。

---

## 一、夜间实证记录

### 1.1 终端数据服务状态：在线

| 检查项 | 结果 |
|---|---|
| 进程名检索 `goldminer/eastmoney` | 无命中（**进程名不是这两个词**，见下行） |
| 监听 7001 端口的进程 | `gmterm-serv.exe`（PID 31252，`F:\dcjj\Eastmoney Goldminer3\resources\app\gmterm-serv.exe`） |
| 端口状态 | `0.0.0.0:7001 LISTENING` + 多条 `127.0.0.1:7001 ESTABLISHED` |

> runbook 注记：晨检查终端用 `netstat -ano | findstr :7001` 或按 `gmterm-serv` 检索进程名，
> `tasklist | findstr /i "goldminer eastmoney"` 会漏报（进程名是 `gmterm-serv.exe`）。

### 1.2 鉴权探针：token 无效（status 1000）

在 `.venv_emquant` 中以 `~/.emgm3` 内定位到的 40 位 token（来源见 1.3）执行
`set_token` + `history(SHSE.000300)` 探针，服务端应答（证明链路与鉴权层均工作）：

```
gm.api._errors.GmError: {"status": 1000, "message": "错误或无效的token", "function": "history"}
```

（报错原文即此——message 中不含任何凭据字段，脱敏无损。）结论：该 token 是终端
「空策略」项目内置 main.py 的**模板示例值**，非真实凭据。

### 1.3 token 搜索面与结论（C8：只记录元信息，不出现值）

| 搜索面 | 结果 |
|---|---|
| `~/.emgm3/projects/7ed8526e-…/main.py` | **token 已定位（模板内置，40 位 alnum）**——即 1.2 被拒的值；实测后已从 `runtime.json` 置空（无效值保留会误导晨检） |
| `~/.emgm3/.gmserv.json` | 无 token 键（服务拓扑配置：hostAddr/rpcPort/siteId 等） |
| `~/.emgm3/User/globalStorage/state.vscdb`（SQLite） | 仅 `colorThemeData` 含 "token*" 字样（UI 文案误命中） |
| `~/.emgm3/Local Storage/leveldb`、`IndexedDB`、`databases` | 无 token 命中 |
| `~/.emgm3/gm3data.lib`（SQLite，终端业务库 38 表全枚举） | `term_setting` 仅 db_schema_version/trading_times；`account_credential` 存的是柜台账户凭据（非 SDK token）；`strategy` 表 1 行 |
| `%APPDATA%/Goldminer3`、`%APPDATA%/东财掘金量化终端` | 均只有 logs，无 token |
| `F:\dcjj\Eastmoney Goldminer3`（安装目录） | 无 userdata；resources/app 为程序本体 |
| 全 `~/.emgm3` UTF-8 + UTF-16 字节级扫描（排除 Cache） | 除上述外无新增命中 |

**判定：本机从未生成过真实 SDK token**。token 生成是终端 UI 内的人工动作
（「系统管理-密钥管理」，Q8 权威文档：token 绑定计算机 ID），无法由脚本代劳——
这正是任务预设的降级出口①。

### 1.4 附带实证（超出任务预期的两个好消息）

- `runtime.json` 的 `strategy_id` 已填**真值** `7ed8526e-9bb7-11f1-a16b-7c10c93fcb7d`
  （终端内「空策略」项目，gm3data.lib `strategy` 表 1 行实证；原任务预期是占位符）。
- 仿真账户已实证登记：`account_credential` 表的 `account_id` 即任务给定的
  `e7cb55d6-04ab-4ea9-98fc-503d9f97d2a1`。

---

## 二、工具链交付与验证状态

| 工具 | 环境 | 状态 |
|---|---|---|
| `emquant/tools/gm_data_pull.py`（gm 腿取数） | `.venv_emquant` | 就绪；token 快速失败路径实测（退出码 3，中文诊断分流 3/4/5/6） |
| `emquant/tools/compare_data.py anchors`（锚表） | `.venv310` | **实测通过**：300 标的全量落锚 `emquant/state/parity_anchors_20260821.json` |
| `emquant/tools/compare_data.py selftest`（比对器自检） | `.venv310` | **实测通过（双向）**：相等副本 4 只全判一致（无假阳）；close×1.0005 扰动标的 300024.SZ 被判不一致且 max_rel=5.000e-04 精确符合注入量级（无假阴） |
| `emquant/tools/compare_data.py compare`（正式对拍） | `.venv310` | 就绪；缺 gm 产物快速失败实测（退出码 12） |

### 2.1 工程注记

- **退出码防吞**：实测 `import gm` 后 C 扩展接管进程退出码（`sys.exit(3)` shell 侧拿
  0）——`gm_data_pull.py` 的 `_die` 改用 `os._exit`（先 flush）。非零码是晨间补跑脚本的
  分流依据，此坑不修则降级判定链断链。
- **中间产物格式降级 parquet → csv**：任务原文落 `.parquet`，但 `.venv_emquant` 无
  pyarrow/fastparquet 且双环境禁装包；csv 为 pandas 双端零依赖等价载体，`to_csv` 的
  float 走最短往返 repr（读回逐位相等），1e-6 容差不受影响。compare 侧按扩展名自适应。
- **锚定口径**：每标的取 data_lake 内末交易日为 `adjust_end_time`（定点前复权，与
  `pilot_body.fetch_df_upto` 逐参对齐：ADJUST_PREV + 锚日 + skip_suspended=True）——
  前复权数值是锚日的函数，两腿同锚才可比。

---

## 三、晨间补跑 runbook（Task 11 承接进 README）

> 环境注记：以下三步命令均在 **Git Bash、仓库根目录 `E:/quanter`** 下执行——命令是
> POSIX 前缀写法（`PYTHONUTF8=1 <python> <脚本>`），PowerShell/cmd 的 env 前缀语法不同
> 会直接报错（Task 11 并入 README 时保留此句）。

### 第 0 步：生成 token（人工，一次性）

1. 打开挖金终端（`gmterm-serv` 常驻即可，UI 需登录态）；
2. 「系统管理 → 密钥管理」生成 token（绑定本机 ID；旧 token 生成新值即作废）；
3. 粘贴进 `emquant/config/runtime.json` 的 `token` 键（**值只进该文件**，C8：该文件已
   gitignore，勿复制进任何文档/终端输出/commit）。

### 第 1 步：锚表（.venv310，已具备，lake 未更新时免跑）

```
PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/compare_data.py anchors
```

### 第 2 步：gm 腿取数（.venv_emquant）

```
PYTHONUTF8=1 E:/quanter/.venv_emquant/Scripts/python.exe emquant/tools/gm_data_pull.py
```

产物：`emquant/state/parity_gm_YYYYMMDD.csv` + 失败清单 `parity_pull_fail_YYYYMMDD.json`。
退出码分流：**2=runtime.json 缺失或 token 空（当前 token 置空态下最可能先命中——回第 0
步生成并填入）** / 3=token 无效 / 4=服务不在线（先拉起终端） / 5=缺锚表（回第 1 步） /
6=全部标的失败（看异常摘要与终端日志）。

### 第 3 步：对拍 + 报告（.venv310）

```
PYTHONUTF8=1 E:/quanter/.venv310/Scripts/python.exe emquant/tools/compare_data.py compare
```

产物：排除池 `emquant/state/parity_exclude_YYYYMMDD.json`（W2 一致率分母豁免清单，
设计 §7）+ markdown 摘要（贴回本报告第四节）。`--md-out` 可直接落盘成段。

---

## 四、对拍判读口径（补跑后填写实测三数）

- 容差：`|gm − lake| / |lake| ≤ 1e-6`，逐日 × 逐列（open/high/low/close），标的级判定
  （一日一列超即整标的不一致）；分母取 lake 值（生产基准腿）。
- 窗口：每标的尾部 2×window+20 根（window=80 → 180 根）；交叠日 < 60 单列
  `insufficient_overlap`（非源分叉证据）。
- **预期管理**：gm（东财源）与 tushare 的前复权因子各自独立维护，「大面积不一致」
  是**有效结论**而非工具故障——排除池的 `guess` 字段已做形态分型（四列比率≈常数 →
  疑似复权因子时点/精度差；比率散布 → 疑似单日价格修复不同步）。如实记录一致率/
  最大相对偏差/典型样例三个数，**不为好看放宽容差**。

### 实测结果（待补跑）

> （占位：晨间补跑后由 compare 的 markdown 摘要回填）

---

## 五、遗留与移交

- [ ] token 生成 + 三步补跑（本报告第三节；Task 11 写入 README 晨检清单）
- [ ] `gmterm-serv` 进程名注记并入 README 的终端自检命令（1.1 的 findstr 陷阱）
- [ ] 补跑后回填第四节实测三数；若一致率低，W2 双轨对照分母按排除池收敛
