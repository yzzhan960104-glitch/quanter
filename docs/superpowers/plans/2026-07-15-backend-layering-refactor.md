# 后端分层重构实现计划（Step 1/2/3）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用渐进 strangler 把后端从「上帝配置 + 上帝模型包 + 零门面穿透」收敛为「config/ 按层拆分包 + caisen/ facade 门面 + engines/optimize/infra/advisor 四子包」，全程零逻辑改动、每步测试绿可中断。

**Architecture:** 三铁律贯穿——① 新结构优先，旧入口保留**模块路径级 re-export 转发垫片**（不止改 `__init__.py`，对 `from core.notifier import X` 这类用法必须保留同名模块文件转发）；② 每步收尾 = 全量 `pytest` 绿 + 一个 commit + 一个可中断点；③ 只动结构，不动逻辑/参数/风控阈值。Step1 拆 `config.py`(857行)→`config/` 8 子文件 + 解散 `core/`（indicator→factors、notifier→infra）；Step2 建 `caisen/facade.py` 收口 8 处穿透 import；Step3 把 `caisen/` 上帝包按职责分 engines/optimize/infra/advisor 四子包。

**Tech Stack:** Python 3.10、Pydantic v2、FastAPI、pytest（基线 86 个测试文件）、纯标准库 re-export 垫片（无新依赖）。

**Spec 来源:** `docs/superpowers/specs/2026-07-15-backend-layering-refactor-design.md`（设计已获批）。本 plan 是其「待转实现计划」的产物，并对 spec 中与代码现状不符之处做了**事实订正**（见下「关键事实基线」）。

---

## Global Constraints

- **语言**：所有新增/修改代码注释、docstring、commit message 使用标准中文（CLAUDE.md 全中文协议）。
- **strangler 红线**：`git diff master --stat` 三步完成时应**几乎只有文件 rename + re-export 垫片 + 新增 `__init__.py`**，**不应有实质算法/参数 diff**。出现大段逻辑改动即偏离原则，必须回退。
- **配额闸门不动**：`JQDATA_CONFIG` 的 `quota_manual_limit=950_000`/`quota_warn_spare=50_000`/`calibrate_every=10` 是防 JQData 按次计费超额扣费的命门，**原样搬运、一个字符不动**。`LAKE_CONFIG` 全部 parquet 路径、`DATASET_REGISTRY`/`TUSHARE_DATASETS` 全部字段**原样搬运**。
- **异常契约不吞**：facade 必须完整透传 `ValidationError/ValueError/KeyError`，对齐 `server/api/v1/caisen.py::_map_service_exception`（行 68：KeyError→404 / ValidationError→422 / ValueError→422），**不得在封装层吞异常降级**。
- **范围边界（明确不做）**：不动 `trading/`、不合并双 risk、不动 `data_lake/` 物理存储、不改任何策略算法/参数/风控阈值、不碰 `feat/tushare-data-collection` 与 `bridge/`、不重命名 `data/`/`data_lake/` 目录。
- **执行环境**：Windows + Git Bash。Python venv 按项目惯例激活后执行 `pytest`。文件移动用 `git mv`（保留历史）。
- **每步 TDD 形态**：纯结构重构无新逻辑，TDD 体现为——每个 Task 先在 `tests/test_layering_compat.py` 追加**兼容性契约断言**（assert 旧 import 路径 + 新 import 路径均可用 + 关键符号可访问），移动后跑该断言 + 全量 `pytest` 绿，再 commit。既有 86 个测试文件是行为锁定安全网。

---

## 关键事实基线（spec 订正点 · 执行者必读）

核对代码现状发现 spec 与实际有偏差，本 plan 已按实际订正，执行时以本表为准：

| 项 | spec 说法 | 实际（已核对） | plan 处理 |
|---|---|---|---|
| `config.py` 行数 | 819 行 | **857 行** | 按实际行段（见 Task 1.1 行段表） |
| `AKSHARE_CONFIG` | 未提 | 存在 @158–163 | 补进 `config/data.py` |
| `get_credential()` | 未提 | 存在 @39–59 | 补进 `config/credentials.py` |
| `LAKE_CONFIG` 构造 | 当单段 | base@113–117 + `["lakes"]`/`["default_lake"]` 追加@171–230（**跨段拼接**） | `config/data.py` 内保序：先 base 后追加 |
| dotenv 副作用 | 未提 | @14–21 `load_dotenv()`，所有凭证依赖它 | 移入 `config/__init__.py` 包入口（最早执行） |
| `core/*` 引用形式 | 「改 `core/__init__.py`」 | 实际全是 `from core.notifier import X` / `from core.indicator import atr`（**模块路径**） | **必须保留 `core/notifier.py`、`core/indicator.py` 同名模块文件作 1 行转发垫片**，仅改 `__init__.py` 不够 |
| `core.notifier` 导出符号 | 未列 | `NotificationManager/fire_and_forget/DingTalkChannel/build_default_manager` + 各 Channel 类，被 bridge/caisen/data/server/trading+多测试引用 | 转发垫片用 `from infra.notifier import *` 一网打尽 |
| 空壳目录 | 「空」 | `factors/strategies/backtest/` 连 `__init__.py` 都没有（仅 `__pycache__`） | Step1 移入文件时**先建 `__init__.py`** 成包 |
| facade 用例数 | 10 | 10 ✅（run_scan/list_plans/approve_plan/activate_plan/get_plan/run_replay/run_replay_async/list_replay_runs/get_replay_run/delete_replay_run） | 签名 1:1 对齐 |
| caisen_service 穿透 import | 6 处 | **8 个符号**：`plan_mod/backtest_replay/replay_runs/replay_tasks_db/storage/StrategyConfig/PatternScreener/RiskManager` | facade 内部全收口 |
| `caisen/` 规模 | 5891 行/24 文件 | **6573 行/24 .py**（新增 training_dingtalk/training_loop） | Step3 归类补全（见 Task 3.x） |
| pytest 基线 | — | **86 个测试文件** | Task 0 锁基线快照 |

---

## File Structure（全局文件蓝图）

**Step 1 新增/修改/删除：**
- Create：`config/{__init__,credentials,market,data,macro,viz,broker,celery,registry}.py`（9 个）
- Delete：`config.py`（原 857 行，内容拆入 `config/` 各子文件）
- Create：`factors/{__init__,atr}.py`、`infra/{__init__,notifier}.py`
- Modify→转发垫片：`core/notifier.py`（1 行 `from infra.notifier import *`）、`core/indicator.py`（1 行 `from factors.atr import atr`）
- Delete：原 `core/notifier.py`、`core/indicator.py` 的**实现**（迁至 infra/factors，原文件降级为垫片）
- Modify：`data/__init__.py`（docstring 追加边界声明）、`core/macro_regime.py`（顶部加归属注释）、`core/__init__.py`（docstring 更新）
- Create：`data_lake/.README`、`tests/test_layering_compat.py`

**Step 2 新增/修改：**
- Create：`caisen/facade.py`（`CaisenFacade` 类 + 4 内部辅助，方法体从 `caisen_service.py` 原样搬入）
- Modify：`server/services/caisen_service.py`（降级为薄壳：单例 `_facade = CaisenFacade()` + 10 模块级函数转发，删除 8 处 caisen 内部 import）
- Modify：`tests/test_layering_compat.py`（追加 facade 契约 + service 不穿透断言）

**Step 3 新增/修改/删除：**
- Create：`caisen/{engines,optimize,infra,advisor}/__init__.py`（4 个子包，3a 阶段 re-export 垫片）
- `git mv`：`caisen/` 内 ~20 个文件进对应子包（3b 阶段，按子包分批）
- Modify：`caisen/__init__.py`（re-export 兼容旧路径 `from caisen.plan import ...`）

---

## Task 0：切分支 + 锁基线

**Files:**
- Create: `tests/test_layering_compat.py`
- 无源码改动

**Interfaces:**
- Produces: `tests/test_layering_compat.py`（贯穿三步的兼容性契约测试文件，后续 Task 向其追加断言）；pytest 基线快照（commit message 记录通过数）。

- [ ] **Step 1: 切出隔离分支**

当前在 `master`，且有与本次重构无关的在途改动（`server/services/review_service.py` 钉钉 review、`data/replay_tasks.db-*`、`scripts/verify_dingtalk_review.py`）。这些**不阻塞重构**，随分支携带即可（重构 commit 只 add 重构相关文件）。

Run:
```bash
git checkout -b refactor/backend-layering
git status --short      # 确认已在新分支，无关改动仍在工作区（正常）
```
Expected: `On branch refactor/backend-layering`，无关 modified/untracked 文件仍在列表中。

- [ ] **Step 2: 锁定 pytest 基线快照**

跑一次全量测试，记录基线通过数（后续每步对比，不许退化）。

Run: `pytest -q 2>&1 | tail -5`
Expected: 全绿，记录 `<N> passed`。若基线本身有 fail/skip，**先记录现状**（不要在此 Task 修复无关测试），在 commit message 标注基线 = `<N> passed / <M> skipped`。

- [ ] **Step 3: 建兼容性契约测试骨架**

Create `tests/test_layering_compat.py`：
```python
# -*- coding: utf-8 -*-
"""后端分层重构兼容性契约测试（Step 1/2/3 贯穿）。

物理意图（strangler 红线守护）：
    本文件是「只动结构不动逻辑」的安全网——每个 Task 移动文件/re-export 后，
    此处断言「旧 import 路径仍可用 + 新 import 路径已可用 + 关键符号可访问」。
    全量 pytest 绿 + 本文件绿 = 结构重构未破坏任何既有契约。

设计纪律：只做 import 与符号存在的断言，不做业务行为断言（行为由既有 86 个测试守护）。
"""
from __future__ import annotations


# ============================================================================
# Step 1 契约：config 包拆分后，所有旧顶层名仍可 from config import
# ============================================================================
def test_config_package_reexports_legacy_names():
    """config.py(857行) 拆为 config/ 包后，from config import X 零改动可用。"""
    from config import (  # noqa: F401
        DATA_SOURCE_CREDENTIALS, MARKET_HOURS, DATA_CONFIG, MACRO_CONFIG,
        VIZ_CONFIG, MOCK_TRADING_CONFIG, LAKE_CONFIG, MACRO_CLIENT_CONFIG,
        CELERY_CONFIG, JQDATA_CONFIG, AKSHARE_CONFIG,
        DATASET_REGISTRY, TUSHARE_DATASETS, SYNCING_DIR, get_credential,
    )
    # LAKE_CONFIG 跨段拼接正确性：base 键 + 追加键都在
    assert "default_path" in LAKE_CONFIG          # base（@113-117）
    assert "lakes" in LAKE_CONFIG and "default_lake" in LAKE_CONFIG  # 追加（@171-230）
    assert LAKE_CONFIG["default_lake"] == "daily"


def test_config_credentials_dotenv_loaded():
    """dotenv 副作用随包入口执行——DATA_SOURCE_CREDENTIALS 结构完整（值可为空但键在）。"""
    from config import DATA_SOURCE_CREDENTIALS
    assert "fred" in DATA_SOURCE_CREDENTIALS and "tushare" in DATA_SOURCE_CREDENTIALS
```

- [ ] **Step 4: 跑骨架测试确认通过（基线态：旧 config.py 仍在，断言应已绿）**

Run: `pytest tests/test_layering_compat.py -v`
Expected: 2 passed（当前 `config.py` 仍是单文件，这些断言对现状也成立——证明基线契约被正确捕获）。

- [ ] **Step 5: Commit**

```bash
git add tests/test_layering_compat.py
git commit -m "test(layering): Task0 建 test_layering_compat 兼容契约骨架 + 锁 pytest 基线"
```

---

## Phase 1 · Step 1 先正名（零逻辑改动 · re-export 垫片）

### Task 1.1：`config.py`(857行) → `config/` 包（8 子文件 + 兼容垫片）

**Files:**
- Create: `config/__init__.py`、`config/credentials.py`、`config/market.py`、`config/data.py`、`config/macro.py`、`config/viz.py`、`config/broker.py`、`config/celery.py`、`config/registry.py`
- Delete: `config.py`（内容拆入子文件后删除单文件；因新建 `config/` 目录会使原 `config.py` 冲突，须先删 `config.py` 再建包）

**Interfaces:**
- Produces: `config/` 包，`from config import <任一旧顶层名>` 与 `import config; config.<名>` 零改动可用。

**搬运行段表（基于 config.py 实际行号 · 原样剪切一个字符不动）：**

| 目标子文件 | 搬运内容（config.py 行段） | 顶部需补的 import |
|---|---|---|
| `config/credentials.py` | `DATA_SOURCE_CREDENTIALS`(27-36) + `get_credential()`(39-59) | `import os`（getenv 用） |
| `config/market.py` | `MARKET_HOURS`(62-67) | 无 |
| `config/data.py` | `DATA_CONFIG`(70-74) + `LAKE_CONFIG` base(113-117) + `LAKE_CONFIG["lakes"]`追加+`default_lake`(171-230) + `MACRO_CLIENT_CONFIG`(122-125) + `JQDATA_CONFIG`(147-152) + `AKSHARE_CONFIG`(158-163) | `import os` as `_os`（base 用 `_os.getenv`）+ `from typing import Dict, Any`（JQDATA/AKSHARE 类型标注） |
| `config/macro.py` | `MACRO_CONFIG`(77-84) | 无 |
| `config/viz.py` | `VIZ_CONFIG`(87-92) | 无 |
| `config/broker.py` | `MOCK_TRADING_CONFIG`(95-100) | 无 |
| `config/celery.py` | `CELERY_CONFIG`(130-134) | `import os` as `_os`（`_os.getenv`） |
| `config/registry.py` | `DATASET_REGISTRY`(254-393) + `TUSHARE_DATASETS`(411-853) + `SYNCING_DIR`(855-857) | `import os`（SYNCING_DIR 用 os.path）+ `from typing import Dict, Any` |

> **`config/data.py` 保序红线**：`LAKE_CONFIG` 必须先 base 定义(113-117) 再追加 lakes(171-230)，**顺序颠倒会 NameError**。剪切时整段顺序保持原样。base 段引用 `_os.getenv`，故 `data.py` 顶部须 `import os as _os`（从 config.py 108 行带过来的别名，让搬运代码零改动）。

- [ ] **Step 1: 备份参照——确认 config.py 行段**

`config.py` 此刻仍是单文件，作为剪切参照源。先 `Read config.py` 确认行段表行号与最新代码一致（行段表基于本 plan 核对时的 857 行版本）。**此步不删 config.py**——文件系统层面 `config.py`（文件）与 `config/`（目录）可共存，且 Python 导入时**包优先于同名 `.py`**，故建好 `config/` 后 `import config` 自动走新包，旧 config.py 暂留无害。

- [ ] **Step 2: 建 8 个子文件，按行段表原样搬运**

逐文件 Create。每个子文件顶部加归属注释 + 补表中所列 import，主体 = config.py 对应行段**原样剪切**（含原有中文注释，一个字符不动）。示例 `config/credentials.py` 头部：
```python
# -*- coding: utf-8 -*-
"""数据源凭证（横切·密钥层）—— 从 config.py 拆出（归属：横切）。

凭证隔离策略不变：API Key/Token 通过 python-dotenv 从 .env 加载，绝不硬编码。
dotenv load 副作用由 config/__init__.py 包入口统一执行（最早加载）。
"""
import os


# （原样剪切 config.py 行 27-36）
DATA_SOURCE_CREDENTIALS = {
    "fred": {"api_key": os.getenv("FRED_API_KEY", "")},
    "tushare": {"token": os.getenv("TUSHARE_TOKEN", "")},
}


# （原样剪切 config.py 行 39-59）
def get_credential(source: str, key: str) -> str:
    ...
```
其余 7 个子文件同理：头部归属注释 + 补 import + 原样剪切对应行段。**`registry.py` 最大（约 600 行），整段剪切 DATASET_REGISTRY + TUSHARE_DATASETS + SYNCING_DIR，不省略任何字段/注释。**

- [ ] **Step 3: 建 `config/__init__.py` 兼容垫片（含 dotenv 包入口）**

Create `config/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""项目配置包（从原 config.py 819→857 行上帝文件按归属层拆分）。

归属层映射：
    credentials/market/data/registry → 数据层；macro → 模型层·宏观；
    viz → 横切·可视化；broker → 执行层；celery → 执行编排。

兼容垫片（strangler 铁律①）：保持 `from config import DATASET_REGISTRY`、
`import config; config.LAKE_CONFIG` 等全部旧用法零改动。

dotenv 包入口：load_dotenv() 在此执行一次（包被 import 即触发），
保证所有 credentials 子模块读到 .env 注入的凭证——这是原 config.py 顶部
副作用（行 14-21）的等价迁移，迁移后仍是最早执行点。
"""
# 包入口副作用：加载 .env（原 config.py 行 14-21 等价迁移，最早执行）
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .credentials import DATA_SOURCE_CREDENTIALS, get_credential
from .market import MARKET_HOURS
from .data import (
    DATA_CONFIG, LAKE_CONFIG, MACRO_CLIENT_CONFIG, JQDATA_CONFIG, AKSHARE_CONFIG,
)
from .macro import MACRO_CONFIG
from .viz import VIZ_CONFIG
from .broker import MOCK_TRADING_CONFIG
from .celery import CELERY_CONFIG
from .registry import DATASET_REGISTRY, TUSHARE_DATASETS, SYNCING_DIR

__all__ = [
    "DATA_SOURCE_CREDENTIALS", "get_credential", "MARKET_HOURS", "DATA_CONFIG",
    "LAKE_CONFIG", "MACRO_CLIENT_CONFIG", "JQDATA_CONFIG", "AKSHARE_CONFIG",
    "MACRO_CONFIG", "VIZ_CONFIG", "MOCK_TRADING_CONFIG", "CELERY_CONFIG",
    "DATASET_REGISTRY", "TUSHARE_DATASETS", "SYNCING_DIR",
]
```

- [ ] **Step 4: 跑兼容契约测试**

Run: `pytest tests/test_layering_compat.py -v`
Expected: 2 passed（`test_config_package_reexports_legacy_names` + `test_config_credentials_dotenv_loaded`）。若失败，先核对 `data.py` 内 LAKE_CONFIG 拼接顺序、`__init__.py` 的 re-export 名是否齐全。

- [ ] **Step 5: 删除旧 `config.py` + 全量 pytest 确认零回归**

Step 4 已证明新包工作，旧单文件已无引用，清理：
Run: `git rm config.py`
Expected: `rm 'config.py'`（删后 `import config` 走 `config/` 包，与 Step 4 行为一致）。

再跑全量：
Run: `pytest -q 2>&1 | tail -5`
Expected: 通过数 ≥ Task 0 基线（不许退化）。常见失败：某模块 `from config import X` 拿到 None → 检查 `__init__.py` 是否漏 re-export 该名。

- [ ] **Step 6: Commit**

```bash
git add config/ tests/test_layering_compat.py
git commit -m "refactor(config): Step1.1 config.py(857行)→config/ 8子文件包 + 兼容垫片(dotenv包入口)"
```

---

### Task 1.2：`core/indicator.py` → `factors/atr.py`（填空目录 + 转发垫片）

**Files:**
- Create: `factors/__init__.py`、`factors/atr.py`（`core/indicator.py` 原样搬入，44 行）
- Modify: `core/indicator.py` → 降级为 1 行转发垫片（保留模块路径，让 `from core.indicator import atr` 零改动）
- 无删除（`core/indicator.py` 文件保留为垫片）

**Interfaces:**
- Produces: `factors.atr.atr`（新位置）、`core.indicator.atr`（旧位置经垫片转发，仍可用）。

**被引用点（已核对，垫片必须覆盖）：** `server/api/v1/macro.py:225 from core.indicator import atr`、`tests/test_final_fixes.py:15 from core.indicator import atr`。

- [ ] **Step 1: 建 `factors/` 包（首次成为 Python 包）**

Create `factors/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""因子层：纯计算因子（ATR 等），从 core/ 解散迁入（归属：模型层·因子）。

factors/ 此前是空壳目录（连 __init__.py 都没有），Step1 首次落地——
原本规划的抽象层开始成型（design §5.3「债务清零」信号）。
"""
from .atr import atr

__all__ = ["atr"]
```

- [ ] **Step 2: 搬运 `core/indicator.py` → `factors/atr.py`（原样，含全部中文注释）**

Run: `git mv core/indicator.py factors/atr.py`
然后编辑 `factors/atr.py`：**内容零改动**，仅在模块 docstring 顶部加一行归属注释：
```python
"""通用技术指标（ATR 等）：从 factors 体系剥离的纯计算函数集合。
（归属：模型层·因子。Step1 从 core/indicator.py 迁入 factors/，逻辑零改动。）
...
```

- [ ] **Step 3: 建 `core/indicator.py` 转发垫片**

Create `core/indicator.py`（替代被 mv 走的原文件）：
```python
# -*- coding: utf-8 -*-
"""兼容垫片（Step1 迁移）：atr 已迁至 factors.atr。

strangler 铁律①：保留旧模块路径转发，`from core.indicator import atr` 零改动。
新代码请用 `from factors.atr import atr`。
"""
from factors.atr import atr  # noqa: F401

__all__ = ["atr"]
```

- [ ] **Step 4: 追加兼容契约 + 跑测试**

在 `tests/test_layering_compat.py` 末尾追加：
```python
# ============================================================================
# Step 1 契约：core/indicator → factors/atr 后，新旧路径并存
# ============================================================================
def test_factor_atr_legacy_and_new_path():
    """core.indicator.atr 迁至 factors.atr，两条 import 路径都可用且同一对象。"""
    from core.indicator import atr as atr_legacy
    from factors.atr import atr as atr_new
    from factors import atr as atr_pkg  # 包级 re-export
    assert atr_legacy is atr_new is atr_pkg
```
Run: `pytest tests/test_layering_compat.py -v`
Expected: 3 passed。

- [ ] **Step 5: 全量 pytest + Commit**

Run: `pytest -q 2>&1 | tail -3`（Expected: 不退化）
```bash
git add factors/ core/indicator.py tests/test_layering_compat.py
git commit -m "refactor(factors): Step1.2 core/indicator→factors/atr 填空目录 + core 转发垫片"
```

---

### Task 1.3：`core/notifier.py` → `infra/notifier.py`（横切 + 转发垫片）

**Files:**
- Create: `infra/__init__.py`、`infra/notifier.py`（`core/notifier.py` 原样搬入，297 行）
- Modify: `core/notifier.py` → 降级为转发垫片
- 无删除

**Interfaces:**
- Produces: `infra.notifier.*`（新位置）、`core.notifier.*`（旧位置经垫片转发，仍可用）。

**被引用点（已核对，垫片必须覆盖符号）：** `bridge/alarmer.py`、`caisen/training_dingtalk.py`、`data/clients/{alpha_vantage,jqdata,yfinance}_client.py`、`server/main.py`、`server/services/trading_service.py`、`trading/{emt,qmt}_gateway.py`、`tests/{test_notifier,test_emt_reconnect}.py`、`tests/bridge/test_alarmer.py`。引用符号含 `NotificationManager/fire_and_forget/DingTalkChannel/build_default_manager` + 各 Channel 类。垫片用 `import *` 一网打尽。

- [ ] **Step 1: 建 `infra/` 横切包**

Create `infra/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""横切基础设施层：通知、日志等跨领域设施（从 core/ 解散迁入）。

归属：横切（Cross-cutting）。core/ 杂物间解散后，notifier 这类
被多层级引用的基础设施归此包；indicator（纯因子）归 factors。
"""
```

- [ ] **Step 2: 搬运 `core/notifier.py` → `infra/notifier.py`（原样，297 行全搬）**

Run: `git mv core/notifier.py infra/notifier.py`
编辑 `infra/notifier.py`：内容零改动，模块 docstring 顶部加一行归属注释「（归属：横切。Step1 从 core/notifier.py 迁入 infra/，逻辑零改动。）」。

> **注意**：`infra/notifier.py` 内部若 `from core.xxx import` 其它 core 模块，暂不动（core/macro_regime Step1 仍留 core/）；只确认它不反向 import infra 自身（搬入后无自引用即可）。

- [ ] **Step 3: 建 `core/notifier.py` 转发垫片（import * 覆盖全部符号）**

Create `core/notifier.py`：
```python
# -*- coding: utf-8 -*-
"""兼容垫片（Step1 迁移）：通知管理器已迁至 infra.notifier。

strangler 铁律①：保留旧模块路径转发，所有 `from core.notifier import X`
（NotificationManager/fire_and_forget/DingTalkChannel/build_default_manager/...）
零改动。新代码请用 `from infra.notifier import ...`。
"""
from infra.notifier import *  # noqa: F401,F403  —— 一网打尽全部公开符号
# 显式再导出 import * 可能漏的（被 __all__ 排除或下划线开头但被外部引用的）：
from infra.notifier import (  # noqa: F401
    NotificationManager, fire_and_forget, DingTalkChannel, build_default_manager,
)
```
> 若 `infra/notifier.py` 未定义 `__all__`，`import *` 会导出所有非下划线名，覆盖面足够；显式再导出 4 行是双保险（grep 确认这 4 个是高频引用符号）。执行时若 `pytest` 报某符号未导出，按报错补到显式再导出列表。

- [ ] **Step 4: 追加兼容契约 + 跑测试**

在 `tests/test_layering_compat.py` 追加：
```python
# ============================================================================
# Step 1 契约：core/notifier → infra/notifier 后，新旧路径并存且符号同源
# ============================================================================
def test_notifier_legacy_and_new_path():
    """core.notifier 迁至 infra.notifier，关键符号新旧路径同源。"""
    from core.notifier import NotificationManager, fire_and_forget
    from infra.notifier import NotificationManager as NM2, fire_and_forget as ff2
    assert NotificationManager is NM2
    assert fire_and_forget is ff2
```
Run: `pytest tests/test_layering_compat.py -v`
Expected: 4 passed。

- [ ] **Step 5: 全量 pytest + Commit**

Run: `pytest -q 2>&1 | tail -3`（Expected: 不退化；`test_notifier.py`/`test_emt_reconnect.py`/`tests/bridge/test_alarmer.py` 必须仍绿——它们 `patch("core.notifier.fire_and_forget")` 经垫片转发仍命中同一对象）
```bash
git add infra/ core/notifier.py tests/test_layering_compat.py
git commit -m "refactor(infra): Step1.3 core/notifier→infra/notifier 横切迁出 + core 转发垫片"
```

---

### Task 1.4：边界声明（data/ vs data_lake/）+ core 残留归属注释

**Files:**
- Modify: `data/__init__.py`（docstring 追加边界声明一句）
- Modify: `core/__init__.py`（docstring 更新：声明 core/ 解散进度）
- Modify: `core/macro_regime.py`（顶部加归属注释，文件暂留 core/）
- Create: `data_lake/.README`

**Interfaces:** 无新接口；纯文档/注释边界声明。

- [ ] **Step 1: `data/__init__.py` docstring 追加边界声明**

在 `data/__init__.py` 现有 docstring 末尾（`"""` 前）追加：
```python
    【边界声明·Step1】data/ = 取数代码包（clients/fetcher/cleaner/resilience/
    tushare_sync/lake_reader），只放 .py 代码，不放数据文件。物理 parquet 存储在
    data_lake/（见 data_lake/.README）。二者命名相似但职责正交，勿混。
```

- [ ] **Step 2: 建 `data_lake/.README` 边界声明**

Create `data_lake/.README`：
```
data_lake/ = 物理存储层（Parquet 数据湖）。
================================================================
边界声明（Step1 后端分层重构）：
  - 只放 .parquet 数据文件 + .syncing/ 同步状态哨兵目录。
  - 禁止放 .py 代码（取数代码归 data/ 包）。
  - 数据集资产元信息（source/market/granularity/script/freshness）单一真相源 =
    config/registry.py 的 DATASET_REGISTRY + TUSHARE_DATASETS。
  - 多湖路径注册 = config/data.py 的 LAKE_CONFIG["lakes"]。
```

- [ ] **Step 3: `core/macro_regime.py` 顶部加归属注释（文件暂留 core/）**

在 `core/macro_regime.py` 模块 docstring 后插入：
```python
"""（归属：模型层·宏观域。Step1 暂留 core/，最终随 Step3/4 迁入模型层·宏观子域。
CreditRegime 信贷周期识别，依赖 data_lake macro 湖。）"""
```

- [ ] **Step 4: `core/__init__.py` docstring 更新解散进度**

Modify `core/__init__.py`：
```python
"""core 包：跨领域残留（解散进行中 · Step1）。

Step1 解散进度：
  - indicator（atr）→ factors/atr.py ✅（core/indicator.py 留转发垫片）
  - notifier      → infra/notifier.py ✅（core/notifier.py 留转发垫片）
  - macro_regime  → 暂留（最终归模型层·宏观域，随 Step3/4 迁出）
新代码勿再向 core/ 添加无关职责模块。
"""
```

- [ ] **Step 5: 全量 pytest + Commit**

Run: `pytest -q 2>&1 | tail -3`（Expected: 不退化）
```bash
git add data/__init__.py data_lake/.README core/__init__.py core/macro_regime.py
git commit -m "docs(layering): Step1.4 data/与data_lake/边界声明 + core解散进度注释"
```

---

### Task 1.5：Step 1 收尾验证

- [ ] **Step 1: 全量 pytest 终检**

Run: `pytest -q 2>&1 | tail -5`
Expected: 通过数 ≥ Task 0 基线，`test_layering_compat.py` 4 项全绿。

- [ ] **Step 2: 旧 import 兼容性巡检（数量不降）**

Run:
```bash
echo "from config import 计数:"; grep -rn "from config import\|from config\." --include="*.py" | wc -l
echo "from core.notor/import core 计数:"; grep -rn "from core\.\|from core import" --include="*.py" | grep -v ".venv" | wc -l
```
Expected: 计数与重构前持平（这些 import 全部经垫片仍可用，无需改动调用点）。

- [ ] **Step 3: git diff 形态巡检（strangler 红线）**

Run: `git diff master --stat`
Expected: 以 `config/`(新增)、`factors/`、`infra/`、`core/{indicator,notifier}.py`(缩小为垫片)、删除 `config.py` 为主，**无大段算法 diff**。

- [ ] **Step 4: 可中断点标记（不 commit，仅确认）**

Step 1 完成。系统完整可用，`config.py` 拆分 + core 解散 + 边界声明已落地，所有旧 import 零改动。可在此停手合并。

---

## Phase 2 · Step 2 立防腐层 facade

### Task 2.1：建 `caisen/facade.py`（CaisenFacade 收口 8 处穿透）

**Files:**
- Create: `caisen/facade.py`

**Interfaces:**
- Consumes: `caisen.plan.generate`、`caisen.backtest_replay.replay`、`caisen.replay_runs.{save_run,list_runs,get_run,delete_run}`、`caisen.replay_tasks_db.{init_db,create_task}`、`caisen.storage.{save_plans,load_plans,update_plan,get_plan}`、`caisen.config.StrategyConfig`、`caisen.patterns.screener.PatternScreener`、`caisen.risk.RiskManager`、`data.symbol_names.get_name`、`data.lake_reader.DataLakeReader`、`config.LAKE_CONFIG`。
- Produces: `caisen.facade.CaisenFacade`（10 公开方法 + 4 内部辅助），方法体与 `caisen_service.py` 现有函数**逻辑零改动**（原样搬入），仅 import 位置从 service 层移到 facade 内部。

**facade 方法签名契约（与 caisen_service 10 函数 1:1）：**
```python
class CaisenFacade:
    def scan(self, req: ScanRequest) -> List[CandidatePlan]
    def list_plans(self, status: Optional[str] = None) -> List[CandidatePlan]
    def approve_plan(self, plan_id: str, review: PlanReview) -> CandidatePlan
    def activate_plan(self, plan_id: str) -> CandidatePlan
    def get_plan(self, plan_id: str) -> Optional[CandidatePlan]
    def replay(self, req: ReplayRequest) -> ReplayReportResponse
    def replay_async(self, req) -> str
    def list_replay_runs(self) -> List[ReplayRunSummary]
    def get_replay_run(self, run_id: str) -> Optional[ReplayRunDetail]
    def delete_replay_run(self, run_id: str) -> bool
    # 内部辅助（私有）：
    #   _load_price_data / _merge_cfg / _plan_to_candidate / _empty_replay_report
```

> **异常透传契约（design §6.3 钉死）**：`scan`/`replay` 内 try 块的 `except (ValidationError, ValueError, KeyError): raise` 必须原样保留——这三个异常透传路由层转 422/404，**facade 不得在封装层吞**。算法/IO 异常的降级（返空列表/零统计 + warning）原样保留。

- [ ] **Step 1: 建 `caisen/facade.py`，方法体从 caisen_service.py 原样搬入**

Create `caisen/facade.py`。结构：
1. 模块 docstring（说明 facade 是模型层唯一对外契约，内部重组对 server 不可见）。
2. import：把 `caisen_service.py` 头部的 8 处 caisen 内部 import + `data.symbol_names` + `config.LAKE_CONFIG` + pydantic/pandas 等**原样搬入 facade**。
3. `class CaisenFacade:` 内，将 caisen_service.py 的 `_load_price_data`/`_merge_cfg`/`_plan_to_candidate`/`_empty_replay_report` 作为**私有方法**（加 `self`，方法体零改动），将 10 个公开函数作为**公开方法**（函数名 `run_scan→scan` 等按签名契约映射，加 `self`，方法体零改动——其中对模块级函数的内部调用如 `run_scan` 调 `_merge_cfg` 改为 `self._merge_cfg`）。

搬运对照（caisen_service.py 行号 → facade 方法）：
| caisen_service 函数 | 行号 | facade 方法 |
|---|---|---|
| `_load_price_data` | 77-136 | `CaisenFacade._load_price_data` |
| `_merge_cfg` | 139-169 | `CaisenFacade._merge_cfg` |
| `_plan_to_candidate` | 172-207 | `CaisenFacade._plan_to_candidate` |
| `run_scan` | 213-296 | `CaisenFacade.scan` |
| `list_plans` | 299-309 | `CaisenFacade.list_plans` |
| `approve_plan` | 312-353 | `CaisenFacade.approve_plan` |
| `activate_plan` | 356-378 | `CaisenFacade.activate_plan` |
| `get_plan` | 381-393 | `CaisenFacade.get_plan` |
| `run_replay` | 396-482 | `CaisenFacade.replay` |
| `_empty_replay_report` | 485-507 | `CaisenFacade._empty_replay_report` |
| `run_replay_async` | 513-527 | `CaisenFacade.replay_async` |
| `list_replay_runs` | 533-550 | `CaisenFacade.list_replay_runs` |
| `get_replay_run` | 553-573 | `CaisenFacade.get_replay_run` |
| `delete_replay_run` | 576-582 | `CaisenFacade.delete_replay_run` |

> 搬运纪律：方法体**逐行原样**，只做两类机械改写——① `def f(args):` → `def f(self, args):`；② 函数内对同级辅助的调用 `_merge_cfg(...)` → `self._merge_cfg(...)`、`_load_price_data(...)` → `self._load_price_data(...)`、`_plan_to_candidate(...)` → `self._plan_to_candidate(...)`、`_empty_replay_report()` → `self._empty_replay_report()`。模块级常量 `_DEFAULT_AUM` 作为类属性 `self._DEFAULT_AUM` 或 facade 顶模块常量（保持值 `1_000_000.0` 不变）。logger 用 `logging.getLogger("caisen.facade")`。

- [ ] **Step 2: 追加 facade 契约测试**

在 `tests/test_layering_compat.py` 追加：
```python
# ============================================================================
# Step 2 契约：facade 10 用例齐备 + service 不再穿透 caisen 内部
# ============================================================================
def test_facade_exposes_ten_use_cases():
    """CaisenFacade 封装 10 个对外用例，签名与 caisen_service 对齐。"""
    from caisen.facade import CaisenFacade
    methods = ["scan", "list_plans", "approve_plan", "activate_plan", "get_plan",
               "replay", "replay_async", "list_replay_runs", "get_replay_run",
               "delete_replay_run"]
    for m in methods:
        assert callable(getattr(CaisenFacade, m)), f"facade 缺方法 {m}"


def test_caisen_service_no_longer_penetrates_caisen_internals():
    """caisen_service 改造后不再直接 import caisen.{plan,storage,backtest_replay,
    replay_runs,replay_tasks_db,patterns,risk,config}（design §6.3 验证项）。"""
    import re, pathlib
    src = pathlib.Path("server/services/caisen_service.py").read_text(encoding="utf-8")
    # 仅校验顶层 import 区（剔除字符串/注释里的字面量）
    forbidden = [
        "from caisen import plan", "from caisen import backtest_replay",
        "from caisen import replay_runs", "from caisen import replay_tasks_db",
        "from caisen import storage", "from caisen.config import",
        "from caisen.patterns.screener import", "from caisen.risk import",
    ]
    hits = [f for f in forbidden if f in src]
    assert not hits, f"caisen_service 仍穿透 caisen 内部: {hits}"
```
> 注：`test_caisen_service_no_longer_penetrates` 在 Task 2.2 改造 service 后才通过；Task 2.1 此步它会 FAIL（service 尚未改），属预期——可临时 `pytest -k "not no_longer_penetrates"` 跳过，Task 2.2 完成后转绿。

- [ ] **Step 3: 跑 facade 契约（暂跳穿透断言）+ Commit**

Run: `pytest tests/test_layering_compat.py -v -k "not no_longer_penetrates"`
Expected: `test_facade_exposes_ten_use_cases` passed。
```bash
git add caisen/facade.py tests/test_layering_compat.py
git commit -m "feat(caisen): Step2.1 建 facade.CaisenFacade 收口 8 处穿透(10用例逻辑原样搬入)"
```

---

### Task 2.2：`caisen_service.py` 降级为薄壳（调 facade，删穿透 import）

**Files:**
- Modify: `server/services/caisen_service.py`（删 8 处 caisen 内部 import + 4 内部辅助 + 函数体，改为单例转发薄壳）

**Interfaces:**
- Consumes: `caisen.facade.CaisenFacade`
- Produces: `caisen_service` 仍导出 10 个同名模块级函数（`run_scan/list_plans/...`），`server/api/v1/caisen.py` **零改动**。

- [ ] **Step 1: 重写 `caisen_service.py` 为薄壳转发**

整文件替换为（保留原模块 docstring 精要，说明已降级为 facade 薄壳）：
```python
# -*- coding: utf-8 -*-
"""蔡森形态学流水线 server 层编排服务（Phase 3 · Step2 降级为 facade 薄壳）。

Step2 重构后：本模块不再穿透 caisen 内部 8 个子模块（plan/storage/
backtest_replay/replay_runs/replay_tasks_db/patterns/risk/config），
改为持有 CaisenFacade 单例并转发 10 个用例。server/api/v1/caisen.py
调用点零改动（模块级函数名与签名不变）。

异常契约不变：ValidationError/ValueError/KeyError 透传路由层转 422/404
（facade 已保证不吞，本薄壳仅转发，不额外捕获）。
"""
from __future__ import annotations

from typing import List, Optional

from caisen.facade import CaisenFacade
from server.schemas.caisen import (
    CandidatePlan, PlanReview, ReplayReportResponse, ReplayRequest,
    ReplayRunDetail, ReplayRunSummary, ScanRequest,
)

# facade 单例：模型层唯一对外契约，内部重组对本薄壳不可见
_facade = CaisenFacade()


def run_scan(req: ScanRequest) -> List[CandidatePlan]:
    return _facade.scan(req)


def list_plans(status: Optional[str] = None) -> List[CandidatePlan]:
    return _facade.list_plans(status)


def approve_plan(plan_id: str, review: PlanReview) -> CandidatePlan:
    return _facade.approve_plan(plan_id, review)


def activate_plan(plan_id: str) -> CandidatePlan:
    return _facade.activate_plan(plan_id)


def get_plan(plan_id: str) -> Optional[CandidatePlan]:
    return _facade.get_plan(plan_id)


def run_replay(req: ReplayRequest) -> ReplayReportResponse:
    return _facade.replay(req)


def run_replay_async(req) -> str:
    return _facade.replay_async(req)


def list_replay_runs() -> List[ReplayRunSummary]:
    return _facade.list_replay_runs()


def get_replay_run(run_id: str) -> Optional[ReplayRunDetail]:
    return _facade.get_replay_run(run_id)


def delete_replay_run(run_id: str) -> bool:
    return _facade.delete_replay_run(run_id)
```

- [ ] **Step 2: 跑全部兼容契约（穿透断言此时应转绿）**

Run: `pytest tests/test_layering_compat.py -v`
Expected: 全绿（含 `test_caisen_service_no_longer_penetrates_caisen_internals`）。

- [ ] **Step 3: 全量 pytest（caisen 相关测试是关键安全网）**

Run: `pytest -q tests/test_caisen_service.py tests/caisen/ tests/test_layering_compat.py 2>&1 | tail -5`
Expected: 全绿（facade 逻辑 = 原 service 逻辑，行为不变）。再跑全量 `pytest -q 2>&1 | tail -3` 确认不退化。

- [ ] **Step 4: Commit**

```bash
git add server/services/caisen_service.py
git commit -m "refactor(caisen): Step2.2 caisen_service 降级 facade 薄壳(删8处穿透import,路由零改动)"
```

---

### Task 2.3：Step 2 收尾验证

- [ ] **Step 1: 穿透清零终检**

Run: `grep -nE "from caisen\.(plan|storage|backtest_replay|replay_runs|replay_tasks_db|patterns|risk|config)" server/services/caisen_service.py || echo "✅ 零穿透"`
Expected: `✅ 零穿透`。

- [ ] **Step 2: 全量 pytest + 可中断点**

Run: `pytest -q 2>&1 | tail -5`（Expected: 不退化）。Step 2 完成：facade 已立，server 只依赖门面，caisen 内部此后任意重组对 server 不可见。可停手合并。

---

## Phase 3 · Step 3 拆 `caisen/` 上帝包

> **子包归属（基于实际 24 文件 · spec 未列全的已补）：**
> - **engines/**（策略本体·纯逻辑无 IO）：`patterns/`(整子包)、`plan.py`、`risk.py`、`config.py` + Step1 已迁的 `factors/atr` 概念上同层（factors 暂留顶层，design §7.1 注明 factors 归 engines，但物理迁移留到确认无循环依赖后，本 plan Step3 不强行下移 factors，避免波及面失控）。
> - **optimize/**（参数优化·可异步可重跑）：`training_analyzer.py`、`training_loops_db.py`、`training_loop.py`、`training_dingtalk.py`。
> - **infra/**（待迁·Step4 移出 caisen）：`storage.py`、`execution.py`、`backtest_replay.py`、`replay_runs.py`、`replay_tasks_db.py`、`replay_scheduler.py`、`replay_worker.py`、`viz_static.py`、`viz_interactive.py`。
> - **advisor/**（AI 决策·预留空包）。

### Task 3.1：建四子包骨架 + re-export（新旧路径并存，文件暂不动）

**Files:**
- Create: `caisen/engines/__init__.py`、`caisen/optimize/__init__.py`、`caisen/infra/__init__.py`、`caisen/advisor/__init__.py`

**Interfaces:**
- Produces: 新路径 `from caisen.engines.plan import ...`、`from caisen.optimize.training_analyzer import ...`、`from caisen.infra.storage import ...` 可用；旧路径 `from caisen.plan import ...` 仍可用（文件未动）。

- [ ] **Step 1: 建 engines 子包 re-export 垫片**

Create `caisen/engines/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""engines/ 策略本体（纯逻辑·无 IO）—— 单向依赖红线：optimize/advisor/infra 依赖本包，本包绝不反向 import 它们。"""
# Step3a 阶段：文件仍在 caisen/ 顶层，此处 re-export 让新路径可用；3b 物理迁移后改指向。
from caisen.plan import *  # noqa: F401,F403
from caisen.risk import *  # noqa: F401,F403
from caisen.config import StrategyConfig  # noqa: F401
from caisen.patterns.screener import PatternScreener  # noqa: F401
# patterns 整子包新路径（3a 垫片，3b 物理移入 engines/patterns/）
from caisen.patterns import (  # noqa: F401
    w_bottom, head_shoulder, triangle_bottom, neckline, zigzag_causal, registry,
)
```
> 执行时若某 patterns 子模块名不存在或 import 报错，按实际 `caisen/patterns/__init__.py` 的导出调整。3a 目标仅是「新路径可 import」，不追求导出全集。

- [ ] **Step 2: 建 optimize / infra / advisor 子包垫片**

Create `caisen/optimize/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""optimize/ 参数优化（可异步·可重跑）—— 单向依赖 engines，绝不反向。"""
from caisen.training_analyzer import *  # noqa: F401,F403
from caisen.training_loops_db import *  # noqa: F401,F403
```
Create `caisen/infra/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""infra/ 待迁项（Step4 移出 caisen 包）—— 单向依赖 engines。含 storage/execution/replay/viz。"""
# 3a 仅占位 + 声明，子模块 re-export 在 3b 物理迁移时补（避免 3a 一次性 import 太多触发潜在循环）。
```
Create `caisen/advisor/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""advisor/ AI 决策（预留占位）—— 单向依赖 engines，绝不反向。caisen-ai-training-loop 落地处。"""
```

- [ ] **Step 3: 追加 3a 兼容契约 + 跑测试**

在 `tests/test_layering_compat.py` 追加：
```python
# ============================================================================
# Step 3a 契约：四子包可 import，旧路径仍可用（新旧并存）
# ============================================================================
def test_caisen_subpackages_scaffold():
    import caisen.engines, caisen.optimize, caisen.infra, caisen.advisor  # noqa: F401
    from caisen.engines import StrategyConfig, PatternScreener  # 新路径
    from caisen.config import StrategyConfig as SC_old  # 旧路径仍可用
    from caisen.patterns.screener import PatternScreener as PS_old
    assert StrategyConfig is SC_old
    assert PatternScreener is PS_old
```
Run: `pytest tests/test_layering_compat.py::test_caisen_subpackages_scaffold -v`
Expected: passed。

- [ ] **Step 4: 全量 pytest + Commit**

Run: `pytest -q 2>&1 | tail -3`（Expected: 不退化）
```bash
git add caisen/engines/ caisen/optimize/ caisen/infra/ caisen/advisor/ tests/test_layering_compat.py
git commit -m "refactor(caisen): Step3.1 建 engines/optimize/infra/advisor 四子包骨架+re-export(新旧并存)"
```

---

### Task 3.2：物理迁移 engines（patterns/ + plan + risk + config）

**Files:**
- `git mv`: `caisen/patterns/` → `caisen/engines/patterns/`（整子包）、`caisen/plan.py` → `caisen/engines/plan.py`、`caisen/risk.py` → `caisen/engines/risk.py`、`caisen/config.py` → `caisen/engines/config.py`
- Modify: `caisen/engines/__init__.py`（re-export 改指新位置）、`caisen/__init__.py`（加旧路径 re-export 转发）、内部 import 调整

**Interfaces:** 旧路径 `from caisen.plan import generate` / `from caisen.patterns.screener import PatternScreener` 经 `caisen/__init__.py` 与保留的转发文件仍可用；新路径 `from caisen.engines.plan import generate` 直接可用。

> **关键纪律（贯穿 3b）**：每移一类文件，立即跑 `pytest -q` 全绿再移下一类。caisen 内部模块间 `from caisen.X import` 的引用，迁移后通过 `caisen/__init__.py` 的顶层 re-export 兜底仍可用（3a 已让 `caisen.X` 名字存在）。若某内部 import 写死 `from caisen.plan import` 且 plan 已移走，则靠 `caisen/__init__.py` 的 `from caisen.engines.plan import *` 兜底——故 **Task 3.2 第一步先建 `caisen/__init__.py` 顶层 re-export**。

- [ ] **Step 1: 先建 `caisen/__init__.py` 顶层 re-export（兜底旧路径）**

Modify `caisen/__init__.py`（原仅 1 行 docstring）：
```python
"""蔡森多空转折形态学流水线（纯多头）。

Step3 分包后：策略本体在 engines/、参数优化在 optimize/、执行/存储/回放在 infra/、
AI 决策预留 advisor/。本 __init__ re-export 旧顶层路径，保证 `from caisen.plan import ...`
等历史用法零改动（strangler 铁律①）。
"""
# 旧顶层路径 re-export（物理文件已迁入 engines/，此处转发）
from caisen.engines.plan import *  # noqa: F401,F403
from caisen.engines.risk import *  # noqa: F401,F403
from caisen.engines.config import StrategyConfig  # noqa: F401
```

- [ ] **Step 2: 迁移 plan/risk/config（先迁无子依赖的叶子）**

Run:
```bash
git mv caisen/plan.py caisen/engines/plan.py
git mv caisen/risk.py caisen/engines/risk.py
git mv caisen/config.py caisen/engines/config.py
```
更新 `caisen/engines/__init__.py` 的 re-export 指向新位置（`from .plan import *` / `from .risk import *` / `from .config import StrategyConfig`）。

- [ ] **Step 3: 迁移 patterns 整子包**

Run: `git mv caisen/patterns caisen/engines/patterns`
更新 `caisen/engines/__init__.py`：`from .patterns.screener import PatternScreener`。保留旧路径兼容：在 `caisen/__init__.py` 追加 `from caisen.engines import patterns  # noqa` 让 `caisen.patterns` 名字仍存在；若调用方 `from caisen.patterns.screener import X`，因 patterns 已是 engines 子包，需补 `caisen/patterns.py` 转发垫片或确认 `caisen.engines.patterns` 经 `caisen.patterns` 可达——**执行时以 `pytest` + grep 实测为准**，缺哪个补哪个转发。

- [ ] **Step 4: 全量 pytest + Commit**

Run: `pytest -q 2>&1 | tail -5`（Expected: 不退化；caisen 内部测试 + facade 测试全绿）
```bash
git add caisen/
git commit -m "refactor(caisen): Step3.2 物理迁移 engines(patterns+plan+risk+config)+旧路径re-export兜底"
```

---

### Task 3.3：物理迁移 optimize（training_*）

**Files:**
- `git mv`: `caisen/training_analyzer.py`、`caisen/training_loops_db.py`、`caisen/training_loop.py`、`caisen/training_dingtalk.py` → `caisen/optimize/`
- Modify: `caisen/optimize/__init__.py`（re-export 改指新位置）、`caisen/__init__.py`（旧路径兜底，若被外部 import）

> `training_dingtalk.py` 内部 `from core.notifier import DingTalkChannel`（Task1 已垫片转发），迁移不受影响。

- [ ] **Step 1: 迁移 + 更新 re-export**

Run:
```bash
git mv caisen/training_analyzer.py caisen/optimize/training_analyzer.py
git mv caisen/training_loops_db.py caisen/optimize/training_loops_db.py
git mv caisen/training_loop.py caisen/optimize/training_loop.py
git mv caisen/training_dingtalk.py caisen/optimize/training_dingtalk.py
```
更新 `caisen/optimize/__init__.py` 指向 `.training_analyzer` / `.training_loops_db` / `.training_loop` / `.training_dingtalk`。grep 确认外部引用点（`from caisen.training_* import`），在 `caisen/__init__.py` 或保留转发模块兜底。

- [ ] **Step 2: 全量 pytest + Commit**

Run: `pytest -q 2>&1 | tail -5`（Expected: 不退化；training loop 相关测试全绿）
```bash
git add caisen/
git commit -m "refactor(caisen): Step3.3 物理迁移 optimize(training_analyzer/loops_db/loop/dingtalk)"
```

---

### Task 3.4：物理迁移 infra（storage/execution/replay_*/viz_*）

**Files:**
- `git mv`: `storage.py`、`execution.py`、`backtest_replay.py`、`replay_runs.py`、`replay_tasks_db.py`、`replay_scheduler.py`、`replay_worker.py`、`viz_static.py`、`viz_interactive.py` → `caisen/infra/`
- Modify: `caisen/infra/__init__.py`（re-export 各模块）、`caisen/__init__.py`（旧路径兜底）

> **infra 文件顶部加标注注释**（design §7.1）：每个迁入文件 docstring 加「（待迁·Step4 移出 caisen 包至执行编排层/横切 viz）」。`facade.py` 内部对 `caisen.storage`/`caisen.backtest_replay`/`caisen.replay_runs`/`caisen.replay_tasks_db` 的 import 经 `caisen/__init__.py` 顶层 re-export 兜底仍可用——**迁移后必须确认 facade 仍绿**。

- [ ] **Step 1: 分批迁移（每 2-3 个文件跑一次 pytest）**

按「storage+execution → replay_*（4个） → viz_*（2个）」三批 `git mv`，每批后：
- 更新 `caisen/infra/__init__.py` 加 `from .storage import *` 等
- 更新 `caisen/__init__.py` 加旧路径兜底 `from caisen.infra import storage, backtest_replay, replay_runs, replay_tasks_db  # noqa`（facade 依赖这 4 个名字）
- Run: `pytest -q 2>&1 | tail -3`

- [ ] **Step 2: 终检 facade + caisen 全绿**

Run: `pytest -q tests/caisen/ tests/test_layering_compat.py 2>&1 | tail -5`
Expected: 全绿。

- [ ] **Step 3: Commit**

```bash
git add caisen/
git commit -m "refactor(caisen): Step3.4 物理迁移 infra(storage/execution/replay_*/viz_*) 标注Step4迁出"
```

---

### Task 3.5：`__main__.py`（623 行 CLI）最后迁移 + 冒烟

**Files:**
- `git mv`: `caisen/__main__.py` → `caisen/engines/__main__.py`（或保留 `caisen/__main__.py` 作 CLI 入口转发，取决于其对内部模块的 import 方式）
- Modify: 视 import 方式调整

> **design §7.3 风控拷问**：`__main__.py` 引用几乎所有内部模块，是 3b 最大波及面。必须**最后**移，移完单独跑 `python -m caisen` 冒烟 + `scripts/smoke_caisen.py`（若存在）。

- [ ] **Step 1: 分析 `__main__.py` import 方式**

Run: `grep -nE "from caisen\.|import caisen" caisen/__main__.py | head -40`
判断：若全用 `from caisen.X import`（顶层路径），经 3.2-3.4 的 `caisen/__init__.py` re-export 兜底，**`__main__.py` 可原地不动**（最稳，CLI 入口 `python -m caisen` 零改动）。

- [ ] **Step 2: 决策迁移与否（以零回归为准）**

- 若 Step 1 显示 `__main__.py` 全靠顶层 re-export 可达 → **不迁移**，仅在其 docstring 标注「Step3 后内部模块已分包子包，本 CLI 经 caisen/__init__ re-export 仍可用」。
- 若有写死的 `from caisen.engines.plan` 之外无法兜底的路径 → 最小改动修正 import，仍保留 `caisen/__main__.py` 入口位置（保持 `python -m caisen` 不变）。

- [ ] **Step 3: 冒烟 + 全量 pytest**

Run:
```bash
python -m caisen --help 2>&1 | tail -5    # CLI 入口可达（若 __main__ 支持 --help）
pytest -q 2>&1 | tail -5                   # 全量不退化
```
Expected: CLI 可达（或 graceful 报已知缺参数，非 ImportError）；pytest 不退化。

- [ ] **Step 4: Commit**

```bash
git add caisen/__main__.py
git commit -m "refactor(caisen): Step3.5 __main__.py CLI 入口确认经re-export可达(最大波及面,最后处理)"
```

---

### Task 3.6：Step 3 收尾验证 + 单向依赖红线终检

- [ ] **Step 1: 单向依赖红线终检（design §7.2 灵魂）**

Run:
```bash
echo "=== engines 是否反向 import optimize/advisor/infra（应零）==="
grep -rnE "from caisen\.(optimize|advisor|infra)|import caisen\.(optimize|advisor|infra)" caisen/engines/ || echo "✅ engines 零反向"
echo "=== 新旧路径并存抽检 ==="
python -c "from caisen.plan import generate; from caisen.engines.plan import generate as g2; assert generate is g2; print('✅ 新旧路径同源')"
```
Expected: `✅ engines 零反向` + `✅ 新旧路径同源`。

- [ ] **Step 2: 全量 pytest + strangler 红线形态终检**

Run:
```bash
pytest -q 2>&1 | tail -5
echo "=== git diff master 形态（应几乎只有 rename + 垫片，无算法 diff）==="
git diff master --stat | tail -20
```
Expected: 通过数 ≥ Task 0 基线；diff stat 以文件 rename + `__init__.py` 新增为主。

- [ ] **Step 3: 三步完成标记**

Step 1/2/3 全部完成。后端已收敛为：`config/` 按层分包（替代 857 行上帝文件）、`caisen/facade.py` 唯一对外契约（server 零穿透）、`caisen/{engines,optimize,infra,advisor}/` 四子包（单向依赖）。执行编排层（trading + 双 risk 合并）留待第 4 步。

---

## Self-Review（plan 自检 · 已执行）

**1. Spec 覆盖：**
- §5.1 config 拆分 → Task 1.1 ✅（含 spec 漏项 AKSHARE_CONFIG/get_credential/LAKE_CONFIG 跨段/dotenv 订正）
- §5.2 data vs data_lake 边界 → Task 1.4 ✅
- §5.3 core 解散（indicator→factors、notifier→infra、macro_regime 暂留）→ Task 1.2/1.3/1.4 ✅（订正：保留同名模块转发垫片，不止改 __init__）
- §6 facade 10 用例 + 异常透传 → Task 2.1/2.2 ✅
- §7.1/7.2/7.3 caisen 四子包 + 单向红线 + __main__ 最后移 → Task 3.1–3.5 ✅
- §8 验证回退 → 每 Task 内嵌 pytest + commit 可回退 ✅
- §9 范围边界 → Global Constraints 钉死 ✅

**2. 占位符扫描：** 无 TBD/TODO/"implement later"；config/core 搬运给精确行段坐标（非占位符——执行者照坐标剪切）；垫片/facade 薄壳/兼容测试给完整代码。

**3. 类型一致性：** facade 10 方法名（scan/list_plans/approve_plan/activate_plan/get_plan/replay/replay_async/list_replay_runs/get_replay_run/delete_replay_run）在 Task 2.1 定义、2.2 转发、test_layering_compat 断言中一致；caisen_service 薄壳函数名（run_scan/list_plans/...）与 server/api 现有调用一致。

**已知风险（执行时关注）：**
- Task 1.1 `config/data.py` 的 LAKE_CONFIG 跨段拼接顺序——颠倒即 NameError，Step 4 兼容测试会捕获。
- Task 3.2-3.4 caisen 内部模块相互 import 经 `caisen/__init__.py` re-export 兜底——若出现循环 import，回退该批迁移，改用保留转发模块（`caisen/plan.py` 一行 `from caisen.engines.plan import *`）替代 __init__ 兜底。
- Task 3.5 `__main__.py` 是最大波及面，若冒烟失败，优先选择「不迁移 + 标注」而非强改其 import。

---

## 回退矩阵

| 步骤 | 回退方式 |
|---|---|
| Task 1.1 | `git revert <commit>`（config.py 由 git 历史恢复） |
| Task 1.2/1.3 | revert 对应 commit（core/indicator.py、core/notifier.py 由垫片恢复为实现） |
| Task 2.x | `git revert` facade/service 两个 commit，service 还原穿透 import |
| Task 3.x | 每个 Task 独立 commit，按需 `git revert` 单步；3a 骨架可单独保留（无害） |

每个 commit 都是独立可中断点，任一步 revert 后系统仍完整可用（strangler 铁律②）。
