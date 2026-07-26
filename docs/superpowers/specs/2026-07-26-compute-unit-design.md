# Mac 远程计算单元(compute_unit)设计 —— discovery 参数回测跨机分摊

> **状态**:设计稿(待评审) · **日期**:2026-07-26 · **作者**:研究员 + Claude
> **依赖背景**:[discovery 引擎状态](../../../memory/discovery-engine-status.md) Plan 1-4 闭环、单组回测 720s、夜跑 8h 出一个冠军
> **关联**:[broadcast 总管](2026-07-26-broadcast-robot-manager-design.md)(钉钉机器人体系,本设计复用其 PUSH 播报通道)

---

## 1. 背景与动机

discovery 参数发现引擎(Plan 1-4 已闭环)的算力瓶颈实证:

- **单组回测成本 720s**(全市场创板科创 universe ~1334 只 × 21 维 params 一组,`scan_symbol` 逐标的串行)
- **夜跑 8h 才出一个冠军**(daemon 跨夜 Sobol+TPE 搜索,Win 主机单机 `eval_batch` 用 ProcessPool 并发,但受限于单机核数)
- **Win 主机核数有限**,参数空间搜索是吞吐瓶颈

研究员有一台**工作机 Mac M1Max(10 核 arm64)**,算力闲置。本设计探索:**把 discovery 的参数回测打包成一个可移植的「计算单元」,让 Mac 同时跑,分摊参数空间搜索**。

### 核心约束(决定一切)

Mac 是**公司封闭工作机**,网络白名单严格:

- ✅ **只能 `git clone/pull` 下载 GitHub 项目**(唯一允许的入站)
- ❌ **不能任何上传**:无 git push、无 scp/SSH 出站、无网盘上传、无 Gist、无对象存储
- ❌ 完全离线,不能调任何外部 API(含钉钉 webhook)

这意味着 **Mac 是一个"只读 git 的离线计算节点"**。所有入站(代码、数据、任务)走 git pull;所有出站(回测结果)只能靠**人**物理搬运。

---

## 2. 目标与非目标

### 目标

1. 打包一个自包含的 `compute_unit` 计算单元,Mac 上 `git pull` + `pip install` 即可运行
2. 输入:一组参数(trial)的描述(task.json);输出:回测指标 + 人可读的 top-N 摘要
3. Mac 算力接入 discovery 参数搜索:Win 主机导出参数批 → Mac 跑 → 人看摘要选好参数 → Win 补跑落库
4. **跨机可比性基石**:Mac 跑的指标口径与 Win discovery **逐字段一致**(同 `scan_symbol` + 同 `split` + 同 `metrics_of`)

### 非目标(YAGNI · 明确不做)

- ❌ **Mac 跑的结果自动回传 discovery SQLite 库**(理由见 §3.3 路径选择——Mac 沙箱下出站人因成本过高,自动回库链路 ROI 太低)
- ❌ **Mac 端做参数搜索**(Sobol/TPE 序贯逻辑留 Win 主机,Mac 只做"给定参数批的评估")
- ❌ **改动 discovery 内核**(`runner/worker/store` 零改动,只在外围加 `task_export`)
- ❌ **支持颈线法以外的策略**(MVP 只覆盖 `strategies/neckline`,与 discovery 当前口径一致)

---

## 3. 架构决策

### 3.1 定位:Mac 是「探索试验台」,不是「discovery 在线 worker」

Mac 跑出的结果**不回库**,只生成人可读摘要。好参数由研究员判断后,在 Win discovery 上**手动补跑**该组(trial_id 与 Mac 算的一致,Win 补跑时 `write_trial` 首次落库,天然去重无冲突)。

> **为什么不让 Mac 结果自动入库?** 见 §3.3 路径选择的红线论证。

### 3.2 协同:准实时批包交换(无 server)

无任何常驻 server、无在线调度。Win 端把参数批打包成 `task.json` push 到 git;Mac `git pull` 拿到后离线跑;结果只本地存 + 生成摘要文本。零网络服务依赖。

### 3.3 路径选择(已与研究员对齐)

曾考虑三条路,最终选**路径 2**:

| 路径 | 描述 | 裁决 |
|------|------|------|
| **路径 1 · 完整双向集成** | Mac result 经 zlib+base64 压成文本,人 AirDrop + 手机钉钉发给 Win 端 result 接收机器人(仿 review 桥),自动解码 import 回 SQLite | ❌ **拒绝**:每批人介入出站 + Win 新建 result 接收桥 + 待查证 `dws dev connect` 消息协议 + 一夜多批人要守夜。工程复杂度与"自动入库"收益不成正比 |
| **路径 2 · 探索试验台** ⭐ | Mac 跑探索性参数,结果不回库;只出 top-N 摘要,人 AirDrop 到手机钉钉发群;好参数 Win 手动补跑 | ✅ **采纳**:绕开 result.json 跨设备传输全部难题(大小限制/接收桥/文件消息协议全不碰);Mac 全自动跑 + 只发小摘要;Win discovery **零改动** |
| 路径 3 · Mac 自采数据 | Mac 跑 tushare 自己采 parquet | ❌ Mac 不能联网调 tushare API,违反沙箱约束 |

**路径 2 的本质**:把出站信息量从"全批 result.json(~50KB)"压缩到"top-N 摘要(~几百字)",把钉钉拉回它擅长的"人读播报"位置,不碰结构化数据管道。Mac 的 M1Max 算力照样用上(快速试参数),只是好结果人肉补登——而补登的 trial_id 与 Mac 一致(同 `trial_id_of` 算法),Win 补跑时天然去重,无冲突。

---

## 4. 仓库与数据策略

**单仓库 `quanter`,三样东西全进 git,Mac 只用 `git pull`**:

| 内容 | git 策略 | 理由 |
|------|----------|------|
| **代码** | 本来就在 git(compute_unit + 借用的 discovery/strategies 子集) | 无变化 |
| **`data_lake/a_shares_daily.parquet`(435MB)** | **纳入 git(非 LFS)** —— 改 `.gitignore` 放行此文件 | Mac 只能 git pull,parquet 必须进 git 才能到 Mac。非 LFS:避免 git-lfs 依赖 + GitHub LFS 配额。首次 clone 慢几分钟,后续数据周更的 diff 小(parquet 列存对增量友好) |
| **`tasks/<task_id>.json`** | 进 master 的 `tasks/` 目录,Win commit + push | 任务包是 JSON 文本几十 KB,进 git 无负担。commit message 用 `[task]` 前缀与代码 commit 区分 |

> **master 会偶尔有 task.json 的 commit** —— 可接受。若日后嫌弃 master 历史变脏,再拆 `tasks` 分支或独立 `quanter-tasks` 仓库,MVP 阶段先最简(Karpathy 极简优先)。

**其他 parquet 不进 git**:discovery 回测只读 `a_shares_daily.parquet` 一个文件(见 `discovery/snapshot.py:LAKE_PATH`),其余 4.4GB 湖与 Mac 计算单元无关,继续 `.gitignore` 排除。

---

## 5. 组件清单与接口

### 5.1 Mac 端:`compute_unit/` 包(新建)

| 文件 | 职任 | 关键接口 |
|------|------|----------|
| `compute_unit/__init__.py` | 包标识 | 空 |
| `compute_unit/protocol.py` | Task/Result dataclass + JSON 序列化 | `Task.from_json(path)`、`Result.to_json(path)`、处理 `date`↔str 与 numpy 类型 |
| `compute_unit/env_check.py` | 跨机一致性校验 | `verify(task) -> None`,三件哈希 + snapshot 双校验,漂移抛 `EnvDriftError`(退出码 3) |
| `compute_unit/runner.py` | 跑批核心 | `run(task) -> Result`,`mp.spawn` Pool 并行 `evaluate`,单组异常→`failed`,n_total=0→`degenerate` |
| `compute_unit/summary.py` | 人可读摘要 | `summarize(result, top_n=3) -> str`,按 inner calmar 选 top-N,生成钉钉友好中文文本 |
| `compute_unit/__main__.py` | CLI 入口 | `verify <task.json>` / `run <task.json> -o <result.json>` / `summary <result.json> --top 3` |

**Mac 端只借 discovery 三个纯函数**(零搜索/落库/调度依赖):

```python
from discovery.objective import evaluate      # evaluate(params, universe, split) -> {inner, outer, n_total}
from discovery.snapshot import freeze          # freeze(lake_start) -> (universe, SnapshotMeta)
from discovery.split import holdout_split      # holdout_split(embargo_days) -> HoldoutSplit
```

### 5.2 Win 端:`compute_unit/task_export.py`(新建,Win 侧用)

| 接口 | 职责 |
|------|------|
| `export_task(params_list, lake_start, embargo_days, seed, out_path)` | 读 params 批 → 当前 `freeze` 取 SnapshotMeta → 算 `trial_id_of` + `engine_hash` + `parquet_sha256` + `git rev-parse HEAD` → 写 `tasks/<task_id>.json` |
| CLI:`python -m compute_unit.task_export --params-file <sobol_batch.json> --out tasks/<id>.json` | 从 discovery Sobol 采样结果或手工 params 文件导出 task |

**discovery 内核零改动**。Phase 2 才在 `discovery/runner.py:run_search` 的 Sobol 阶段后挂一个可选的 `export_task` 调用(MVP 阶段手工导出)。

---

## 6. 数据流(完整闭环)

```
Win 主机                                    Mac M1Max(封闭 · 只 git pull)
─────────                                   ────────────────────────────────
[Phase 1 一次性]                            [Mac 环境准备 · 一次性]
git add data_lake/a_shares_daily.parquet    git clone <quanter>
git commit + push                           cd quanter
                                            python -m venv .venv && source .venv/bin/activate
                                            pip install pandas numpy pyarrow

[每次跑批]                                  [每次跑批]
discovery Sobol 采样 → params 批             git pull
compute_unit.task_export                      (拉代码 + parquet 增量 + tasks/<id>.json)
  → tasks/<id>.json                          
git add tasks/ && commit "[task] ..."       
git push ──────────────────git──►            compute_unit verify tasks/<id>.json
                                                (三件哈希校验 + snapshot 双校验)
                                              compute_unit run tasks/<id>.json -o result.json
                                                (mp.spawn Pool 并行 evaluate 全批)
                                              compute_unit summary result.json --top 3
                                                → summary.txt(top-3 trial 中文摘要)
                                            人 AirDrop summary.txt → iPhone
                  ◄──────AirDrop───────     summary.txt
人在手机钉钉粘贴 summary 发群
人看摘要 → 选好参数(trial_id / params)
discovery 跑该组(eval_batch 单组)
  → write_trial(trial_id 与 Mac 一致 · 首次落库)
```

---

## 7. 协议

### 7.1 task.json(Win → Mac)

```jsonc
{
  "protocol_version": 1,
  "task_id": "a1b2c3d4",                    // uuid4 前 8,任务幂等键
  "created_at": "2026-07-26T12:00:00",      // ISO 时间戳(Win 端 _now_iso)
  "git_commit": "<HEAD sha40>",             // 代码一致性:Mac 校验本地 HEAD == 此值
  "engine_hash": "<sha256[:12]>",           // backtest.py + method_v0.py 指纹(Mac 重算校验)
  "parquet_sha256": "<sha256 hex>",         // a_shares_daily.parquet 全文件指纹(Mac 校验本地文件)
  "lake_start": "2025-01-01",               // freeze 加载起始日
  "embargo_days": 5,                        // holdout_split embargo
  "snapshot_meta": {                        // discovery SnapshotMeta 原样(Win 权威,Mac 不重算)
    "snapshot_hash": "<sha256[:16]>",
    "universe_def": "创板科创/2025截面/近30日均额≥1e5千元",
    "universe_count": 1334,
    "date_range": "2025-01-01~2026-07-25",
    "lake_start": "2025-01-01"
  },
  "split": {                                // HoldoutSplit 序列化(date → "YYYY-MM-DD" str)
    "inner": {"name": "inner_2025", "start": "2025-01-01", "end": "2025-12-31"},
    "outer": {"name": "outer_2026", "start": "2026-01-01", "end": "2026-12-31"},
    "embargo_days": 5
  },
  "trials": [                               // 一批 params(Win 端用 trial_id_of 预算 trial_id)
    {
      "trial_id": "<trial_id_of(params, snapshot_hash, seed)[:12]>",
      "params": {"window": 20, "min_touches": 3, "...21 维": "..."},
      "source": "discovery_search",
      "seed": 42
    }
  ]
}
```

**trial_id 权威来源**:Win 端 `task_export` 用 `discovery.store.trial_id_of(params, snapshot_hash, seed)` 预算好放进 task.json。**Mac 不算 trial_id**,原样回填到 result。这保证跨机 trial_id 必然一致(同一算法、同一份输入),Win 补跑时去重无冲突。

### 7.2 result.json(Mac 本地,**不回传**)

```jsonc
{
  "task_id": "a1b2c3d4",                    // 与 task.json 配对
  "git_commit": "<回填>",                    // task.json 的值原样回填(便于追溯)
  "parquet_sha256": "<回填>",
  "ran_at": "2026-07-26T20:00:00",          // Mac 跑批时间(ISO)
  "results": [
    {
      "trial_id": "...",
      "status": "ok",                       // ok | failed | degenerate
      "inner": {"n": 1164, "ann": 0.184, "sharpe": 1.2, "max_dd": 0.025, "kelly": 0.14, "calmar": 7.24, "curve": 1.18},
      "outer": {"n": 800, "ann": 0.143, "sharpe": 0.9, "max_dd": 0.013, "kelly": 0.11, "calmar": 10.76, "curve": 1.14},
      "n_total": 1964,
      "error": ""                           // failed 时填异常摘要
    }
  ]
}
```

> `inner`/`outer` metrics 字段与 `discovery.store.write_trial` 落库的 `inner_metrics`/`outer_metrics` JSON **同构**(见 `discovery/objective.py:metrics_of`)——保证 Win 补跑该组落库时,字段口径完全一致。

---

## 8. 跨机一致性保证(基石)

discovery 的可比性靠 `snapshot_hash + engine_hash + trial_id` 三件套。Mac 计算单元必须保证这三件与 Win **字节级一致**,否则 Mac 跑的是脏数据,摘要里的 calmar 和 Win 补跑的对不上,整套筛选失去意义。校验分两层:

### 8.1 三件哈希校验(`env_check.verify`)

| 字段 | Mac 端校验逻辑 | 失败处置 |
|------|----------------|----------|
| `git_commit` | `subprocess.run(["git","rev-parse","HEAD"])` 本地 HEAD 与 task.json 值比对 | 不符 → `EnvDriftError("git_commit 漂移:Mac 本地 <sha> ≠ task <sha>,请 git pull")` |
| `engine_hash` | 重算本地 `strategies/neckline/backtest.py` + `method_v0.py` 的 sha256[:12](与 `discovery/runner.py:_engine_hash` 同款算法) | 不符 → `EnvDriftError("engine_hash 漂移:回测内核代码不一致")` |
| `parquet_sha256` | 重算本地 `data_lake/a_shares_daily.parquet` 全文件 sha256 | 不符 → `EnvDriftError("parquet_sha256 漂移:数据滞后或损坏,请 git pull 最新 parquet")` |

任一漂移 → `compute_unit verify` 退出码 3,`compute_unit run` 启动前先 verify,漂移拒跑。

### 8.2 snapshot 双校验(逻辑层保险)

即便三件哈希通过(文件没换),仍要防"universe 定义逻辑改了"——Mac 本地 `freeze(lake_start)` 重算 `universe_count + date_range`,与 task.json 的 `snapshot_meta` 比对:

```python
universe, meta = freeze(task.lake_start)
if meta.universe_count != task.snapshot_meta.universe_count:
    raise EnvDriftError(f"universe_count 漂移:Mac freeze {meta.universe_count} ≠ task {task.snapshot_meta.universe_count}")
if meta.date_range != task.snapshot_meta.date_range:
    raise EnvDriftError(f"date_range 漂移:Mac {meta.date_range} ≠ task {task.snapshot_meta.date_range}")
```

这层保险捕获哈希漏掉的逻辑层漂移(如 `is_target_board` 改了过滤规则、`load_universe` 流动性阈值调了)。

---

## 9. 错误处理与边界(CLAUDE.md 拷问三连)

### 9.1 流动性与极端行情(拷问①)

- **Mac parquet 周更滞后**:Win 主机每天 tushare 采数据,parquet 每天变。Mac pull 到旧 parquet → `parquet_sha256` 校验失败 → 报"数据滞后,请等 Win push 最新 parquet 后再 pull"。**绝不会拿旧数据跑出脏结果冒充新结果**。
- **Mac 跑批中途 Win 推了新 parquet**:Mac 本次跑批用的还是 pull 时的 parquet(result.json 回填当时的 sha256),不受影响;下次 pull 后 verify 才会发现变化。无并发污染。

### 9.2 接口与状态机边界(拷问②)

- **单 trial 异常**:spawn worker `_eval_worker` 捕获单组异常 → result 标 `status="failed"` + 填 error 摘要,**不阻断批**(对应 `discovery/worker.py:_eval_worker` 返回 None 的语义)。summary 跳过 failed 的 trial。
- **退化裁剪**:`evaluate` 返回 `n_total==0`(params 退化,全 universe 挂单区间空)→ 标 `status="degenerate"`,summary 不展示(对应 `_eval_worker` 的 n_total==0 裁剪)。
- **spawn 跨平台**:`compute_unit/runner.py` 显式 `mp.get_context("spawn")`(与 `discovery/worker.py` 同款),Win/Mac 行为一致,不踩 fork 继承父进程内存/锁状态的坑。worker 函数顶层定义、universe 经 initializer 注入模块全局(不随每 params pickle,否则 455MB universe 每次 map 序列化爆掉)——**完全沿用 discovery.worker 的 spawn 四铁律**。
- **断点续跑**:Mac 跑批中断,result.json 覆盖写(或写 `.partial` 完成后 rename),重跑无状态丢失。task.json 不动(只读)。

### 9.3 策略风险敞口(拷问③)

- **Mac 跑的指标与 Win 不完全 bit 级一致**:Win x64 vs Mac arm64 浮点末位差,calmar 等指标可能有 1e-15 级抖动。**无害**——Mac 不落库,只用于人看摘要选参数;Win 补跑该组时 trial_id 由 `params + snapshot_hash + seed` 算(与浮点无关),必然一致,`write_trial` 首次落库自动去重。
- **人误判摘要选错参数**:Mac 摘要只展示 top-N,人可能漏掉次优。**缓解**:摘要同时展示 inner+outer calmar + max_dd + n(样本笔数),人按"outer calmar 高 + max_dd 小 + n 足够"综合判断,与 discovery 的 judging 口径一致(信息隔离:outer 不反馈搜索,但供人参考)。

---

## 10. 跨架构注意

| 维度 | Win x64 | Mac arm64 | 处置 |
|------|---------|-----------|------|
| Python | 3.10(`.venv310`) | 3.10+ | Mac 自建 venv,版本对齐 |
| pandas/numpy | x64 wheel | arm64 wheel(官方有) | `pip install` 自动选架构 |
| pyarrow(parquet) | x64 wheel | arm64 wheel(官方有) | 同上 |
| multiprocessing | spawn(显式) | spawn(显式) | 显式 `mp.get_context("spawn")`,行为一致 |
| 浮点 | x87/SSE | NEON | 末位差 1e-15,见 §9.3(无害) |
| 文件路径 | `data_lake/a_shares_daily.parquet`(正斜杠) | 同上 | 用 `pathlib.Path`,不硬编码分隔符 |
| xtquant/qmt/broker | Win-only(实盘) | **不需要** | compute_unit 只 import 纯 python 的 strategies/discovery 子集,不碰 broker/xtquant |

---

## 11. 测试策略(TDD)

每个组件先写失败测试,再实现。**核心红线:同机跨实现等价**——在同一台 Win 机器上,compute_unit 跑出的指标必须与 `discovery.objective.evaluate` 直跑**逐字段相等**(同机同架构,浮点完全一致,可严格断言)。跨机(Win x64 vs Mac arm64)的浮点末位差见 §9.3,不在单测覆盖范围(单测只跑 Win 本机)。

| 测试文件 | 覆盖 | 红线断言 |
|----------|------|----------|
| `tests/compute_unit/test_protocol.py` | Task/Result 序列化往返(date↔str、numpy float、split 嵌套) | `Task.from_json(p) == original_task`;date 字段往返无损 |
| `tests/compute_unit/test_env_check.py` | 三件哈希校验(各件漂移分支)+ snapshot 双校验分支 | 各分支抛 `EnvDriftError`;三件全通过返 None |
| `tests/compute_unit/test_runner.py` | 小 universe fixture(3~5 标的合成 parquet),跑一组 params | `runner.run(task).results[0].inner == evaluate(params, universe, split)["inner"]` **逐字段相等** |
| `tests/compute_unit/test_summary.py` | 给定 fixture result,断言 top-N 选取 + 中文摘要格式 | top-N 按 inner calmar 降序;摘要含 trial_id/calmar/max_dd/ann |
| `tests/compute_unit/test_task_export.py` | mock freeze+git,断言 task.json 字段完整 | trial_id == `trial_id_of(params, snapshot_hash, seed)`;engine_hash/parquet_sha256/git_commit 字段在 |
| `tests/compute_unit/test_e2e.py` | 端到端:task_export → runner.run → summary | 全链路跑通,无漂移;result 与 Win 直跑一致 |

**集成等价测试(最关键)**:Win 本机 `evaluate(p, universe, split)` 直跑 vs `task_export(p) → compute_unit.run → result`,同 params 的 inner/outer metrics **逐字段相等**。这条测试守的是"compute_unit 不能偷偷改指标口径"的命根子(对应 discovery ADR8 内核零改动精神)。

---

## 12. 分阶段交付

### Phase 1 · MVP(跑通全链路)

**范围**:
- `compute_unit/` 五个组件(protocol/env_check/runner/summary/__main__)+ 单测
- `compute_unit/task_export.py` + 单测
- `.gitignore` 放行 `data_lake/a_shares_daily.parquet`,首次 `git add` 纳入(单独一个 commit,435MB 进 git)
- 端到端集成测试

**验收**:
- `compute_unit verify + run + summary` 在 Win 本机(模拟 Mac)跑通
- compute_unit 跑出的 inner/outer metrics 与 Win 本机 `discovery.objective.evaluate` 直跑**逐字段相等**
- 三件哈希校验各漂移分支单测全绿

**不包含**:discovery 内核改动、自动导出钩子、Mac 真机部署(Win 本机模拟即可验证)。

### Phase 2 · 接 discovery 实际采样 + Mac 真机部署

**范围**:
- `discovery/runner.py:run_search` 的 Sobol 阶段后,挂一个**可选**的 `task_export` 调用(由 `--export-task` flag 或 env 触发,默认关闭,不影响现有 daemon)
- Mac 真机环境准备文档(SOP):clone、venv、pip install、首次 pull parquet
- Win/Mac 双端跑批 SOP 文档
- 补跑 SOP:从摘要读 trial_id → Win `discovery` 跑该组落库

**验收**:
- discovery Sobol 采样后自动导出 task.json 到 `tasks/`
- Mac 真机 git pull + compute_unit run 跑通一组真实 params
- Win 补跑该组,trial_id 与 Mac 一致,write_trial 落库成功

### Phase 3 · 可选(评估后再定)

若 Phase 2 用得顺手且想要"自动回库",再评估升级到**路径 1**(result 接收桥 + import 回库)。届时单独开 spec,本设计不预设。

---

## 13. 风险与取舍

| 风险 | 影响 | 缓解 |
|------|------|------|
| parquet 435MB 进 git,仓库膨胀 | clone/clone 慢;`.git` 历史累积 | 后续数据增量小(周更 diff);若累积过大,Phase 2+ 评估改 git LFS 或独立数据仓库 |
| Mac git pull 拉大仓库慢 | 每次跑批前 pull 耗时 | parquet 增量小,只有首次 clone 慢;后续 pull 秒级 |
| 人因成本(AirDrop + 手机钉钉) | 每跑一批人介入出站 | 路径 2 已把出站压到"top-N 摘要几百字",人操作 2 步(AirDrop + 粘贴);跑批 10h vs 人操作 5 分钟,可接受 |
| Mac 浮点与 Win 末位差 | 摘要 calmar 与 Win 补跑可能有 1e-15 差 | 无害(§9.3):Mac 不落库,trial_id 由非浮点字段算 |
| 任务包进 master 脏历史 | master 偶有 `[task]` commit | commit message 前缀区分;若嫌弃 Phase 2 拆 tasks 分支 |
| discovery Sobol 自动导出钩子影响 daemon | Phase 2 改 run_search | 默认关闭(flag 触发),现有 daemon 零影响;单测守护 |

---

## 14. 用户侧外向动作(AI 不替按 · 实施完成后由研究员执行)

```bash
# Phase 1 一次性:parquet 纳入 git(AI 改 .gitignore 后,研究员执行 add+push)
cd F:/quanter
git add data_lake/a_shares_daily.parquet
git commit -m "chore(data): 纳入 a_shares_daily.parquet 入 git(Mac 计算单元需 git pull 取数)"
git push

# Phase 2 Mac 真机准备(研究员在 Mac 上执行)
git clone <quanter>
cd quanter
python3 -m venv .venv && source .venv/bin/activate
pip install pandas numpy pyarrow
# 首次 pull 已含 parquet(435MB,clone 时已下)

# Phase 2 每次跑批(Mac)
git pull
.venv/bin/python -m compute_unit verify tasks/<latest>.json
.venv/bin/python -m compute_unit run tasks/<latest>.json -o result.json
.venv/bin/python -m compute_unit summary result.json --top 3 > summary.txt
# 人 AirDrop summary.txt 到 iPhone → 手机钉钉粘贴发群

# Phase 2 Win 补跑好参数(研究员在 Win 执行)
# 从摘要读 trial_id / params → discovery 跑该组落库(具体命令 Phase 2 SOP 定)
```

---

## 15. 开放问题(Phase 2 再定)

1. **discovery Sobol 自动导出的触发方式**:`run_search` 加 `--export-task <path>` flag,还是 `discovery/daemon.py` 跑完后统一导出?Phase 2 设计时定。
2. **摘要的 top-N 排序键**:按 inner calmar(与 discovery 冠军排序同口径)还是按 outer calmar(去偏锚)?MVP 先 inner calmar(与 discovery 一致),Phase 2 看用户偏好调整。
3. **task.json 的清理策略**:`tasks/` 累积历史 task,何时清理?MVP 不清理(文本小),Phase 2 看累积量定。
4. **Mac 端 Python 版本对齐**:Win 用 `.venv310`(Python 3.10),Mac 系统自带 python3 版本可能不同。Phase 2 部署时确认 Mac Python 版本,必要时用 pyenv 装 3.10。
