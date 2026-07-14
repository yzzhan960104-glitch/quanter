# 参数瘦身 + 分层展示 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: 用 superpowers:subagent-driven-development 逐任务实现。步骤用 checkbox（`- [ ]`）跟踪。

**Goal:** 删 `StrategyConfig` 6 个死字段（39→33），`paramMeta.ts` 同步 + 加形态核心/高级分层，/lab 参数表单形态核心默认展开、计划·执行·风控折叠在「显示高级参数」开关后。

**Architecture:** 后端 config 删字段 + paramMeta 同步删（双向 sync 守护强制）+ confirm_bars 重分蔡森方法学组 + 加 CORE_GROUPS；前端 NewReplayDrawer/ParamLabView 按 group→tier 分层展示，开关仅控可见性、保留全调参能力。

**Tech Stack:** pydantic（config）/ vitest + @vue/test-utils（前端）/ pytest（sync 守护）。

## Global Constraints

- **全中文**：注释 What+Why。
- **删字段安全性**：6 个待删字段全仓库零属性访问、零测试构造引用（除 registry.py:45 注释 + paramMeta.ts 数据条目）；删前 grep 复核。
- **sync 守护**：`tests/test_param_meta_sync.py` 双向匹配 config.model_fields ↔ PARAM_META 键集——config 与 paramMeta 必须同任务内同步删，否则 orphan/missing 报错。
- **design token**：`--qt-*`，禁裸 hex。
- **YAGNI**：不删 33 个活字段任何一个；不引入后端"生效集"语义；不动既有 boolean 开关。
- **Python**：`.venv310`；前端：`cd web && npm run typecheck && npm run test`。
- **TDD + 频繁提交**：commit message 末尾加 `Co-Authored-By: Claude <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 改动 | 任务 |
|---|---|---|
| `caisen/config.py` | 删 6 Field 定义 | T1 |
| `caisen/patterns/registry.py` | 清 :45「未实现」注释 | T1 |
| `web/src/components/lab/paramMeta.ts` | 删 6 条 + confirm_bars 重分组 + 加 CORE_GROUPS/isCoreGroup | T1 |
| `web/src/components/lab/NewReplayDrawer.vue` | showAdvanced 开关 + core/advanced 分组渲染 | T2 |
| `web/src/views/ParamLabView.vue` | 参数详情面板 showAdvanced 分层 | T2 |
| `web/src/components/lab/NewReplayDrawer.spec.ts` | 加分层用例 | T2 |

---

## Task 1: 删 6 死字段 + paramMeta 同步 + 分层常量

**Files:**
- Modify: `caisen/config.py`（删 6 Field）、`caisen/patterns/registry.py`（清注释）、`web/src/components/lab/paramMeta.ts`（删 6 条 + 重分组 + 加常量）
- Test: `tests/test_param_meta_sync.py`（双向守护，删后须重新匹配）

**Interfaces:**
- Consumes: `caisen.config.StrategyConfig.model_fields`（sync 真相源）
- Produces: `StrategyConfig` 33 字段；`PARAM_META` 33 键；新增 `CORE_GROUPS: ParamGroup[]`、`isCoreGroup(g): boolean`

**待删 6 字段**：`symmetry_tolerance`、`pattern_width_bonus`、`false_breakout_threshold`、`false_breakout_window`、`enable_pot_breakout`、`enable_bottom_flip`（全仓库零消费，registry.py:45 自承未实现）。

- [ ] **Step 1: grep 复核 6 字段零引用（删前安全确认）**

Run:
```bash
cd /c/Users/yzzhan/Desktop/quanter
for f in symmetry_tolerance pattern_width_bonus false_breakout_threshold false_breakout_window enable_pot_breakout enable_bottom_flip; do
  echo "--- $f ---"; grep -rEn "\b${f}\b" caisen/ server/ scripts/ tests/ web/src/ --include='*.py' --include='*.ts' --include='*.vue' 2>/dev/null | grep -v __pycache__ | grep -v 'config.py' | grep -v 'paramMeta.ts' | grep -v 'registry.py:45'
done
```
Expected: 全空（除 config.py 定义、paramMeta.ts 条目、registry.py:45 注释外无引用）。若有意外引用 → STOP 报告。

- [ ] **Step 2: 删 config.py 6 个 Field 定义**

`caisen/config.py`：
- 时间跨度组：删 `symmetry_tolerance`（左右结构时间对称容忍度）整段 Field。
- 蔡森方法学组：删 `pattern_width_bonus`、`enable_pot_breakout`、`enable_bottom_flip`、`false_breakout_threshold`、`false_breakout_window` 五段 Field。

删后该两组仅保留活字段（时间跨度：min_pattern_bars/max_pattern_bars；蔡森方法学：neckline_height_multiple/abc_wave_detect/right_above_left/ma26w_filter/ma26w_window/pattern_tension_ratio/enable_triangle_bottom/triangle_max_pattern_depth/triangle_breakout_min/triangle_breakout_max）。

- [ ] **Step 3: 清 registry.py:45 注释**

`caisen/patterns/registry.py` 第 45 行那条 `# 未实现的 enable_pot_breakout/enable_bottom_flip/false_breakout_* 开关待对应形态…` 注释整行删除（开关已从 config 移除，注释失效）。

- [ ] **Step 4: 改 paramMeta.ts —— 删 6 条 + confirm_bars 重分组 + 加分层常量**

`web/src/components/lab/paramMeta.ts`：
- 删 PARAM_META 中 6 条：`symmetry_tolerance`、`pattern_width_bonus`、`false_breakout_threshold`、`false_breakout_window`、`enable_pot_breakout`、`enable_bottom_flip`。
- `confirm_bars` 条目 group 由 `'风控'` 改为 `'蔡森方法学'`（它被 zigzag_causal/screener 消费，是形态核心；原误归风控会让分层把它误分到高级）。
- 文件末尾追加分层常量：
  ```ts
  /**
   * 参数分层（/lab 表单分层展示用，spec 2026-07-14-param-slim）：
   * 形态核心组默认展开；交易执行/时间止损/风控为高级，折叠在「显示高级参数」开关后。
   * 分层按 group 映射——故 confirm_bars 须在蔡森方法学组（形态核心）而非风控组。
   */
  export const CORE_GROUPS: ParamGroup[] = ['时间跨度', '空间高度', '量价配合', '蔡森方法学']
  export function isCoreGroup(g: ParamGroup): boolean {
    return CORE_GROUPS.includes(g)
  }
  ```

- [ ] **Step 5: 跑 sync 守护 + 全量后端 + 前端 typecheck**

Run:
```bash
.venv310/Scripts/python.exe -m pytest tests/test_param_meta_sync.py -q
.venv310/Scripts/python.exe -m pytest -q
cd web && npm run typecheck
```
Expected: sync 守护 PASS（config 33 字段 == PARAM_META 33 键）；全量 pytest 不回归（700+，6 字段零引用）；typecheck 净（前端泛型迭代 PARAM_META 不受条目数影响）。

- [ ] **Step 6: 提交**

```bash
git add caisen/config.py caisen/patterns/registry.py web/src/components/lab/paramMeta.ts
git commit -m "refactor(config): 删6死字段 + paramMeta分层常量（参数瘦身 Task 1）

删 symmetry_tolerance/pattern_width_bonus/false_breakout_*/enable_pot_breakout/
enable_bottom_flip（零消费）；confirm_bars 重分蔡森方法学组；加 CORE_GROUPS/isCoreGroup。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: /lab 参数表单分层展示（形态核心默认·高级开关）

**Files:**
- Modify: `web/src/components/lab/NewReplayDrawer.vue`、`web/src/views/ParamLabView.vue`
- Test: `web/src/components/lab/NewReplayDrawer.spec.ts`

**Interfaces:**
- Consumes: T1 的 `isCoreGroup(g)`、`PARAM_GROUPS`、`PARAM_META`
- Produces: drawer + 详情面板形态核心组默认展开、高级组在「显示高级参数」`el-switch` 后；开关仅控可见性

- [ ] **Step 1: 写失败测试（NewReplayDrawer.spec.ts 加分层用例）**

`web/src/components/lab/NewReplayDrawer.spec.ts` 追加（复用既有 SCHEMA fixture 与 mount 模式；SCHEMA.properties 须含一个 core 组字段 + 一个 advanced 组字段，便于断言分层）：

```ts
  it('默认仅渲染形态核心组（高级组隐藏）；开 showAdvanced 后高级组出现', async () => {
    // schema 含 core 组(min_rr_ratio∈交易执行=高级? 否——min_rr_ratio 是交易执行=高级) +
    //   core 组(confirm_bars∈蔡森方法学=核心)。构造一个 core 一个 advanced 字段。
    const schema = {
      properties: {
        confirm_bars:   { type: 'integer', default: 3, description: 'ZigZag确认窗' },  // 蔡森方法学=核心
        min_rr_ratio:   { type: 'number', default: 1.5, description: '盈亏比下限' },    // 交易执行=高级
        max_holding_bars: { type: 'integer', default: 15, description: '最大持仓' },    // 时间止损=高级
      },
    }
    const wrapper = mount(NewReplayDrawer, {
      props: { visible: true, configSchema: schema, prefill: null },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    // 默认：核心组「蔡森方法学」在；高级组「交易执行」「时间止损」不在
    expect(wrapper.text()).toContain('蔡森方法学')
    expect(wrapper.text()).not.toContain('交易执行')
    expect(wrapper.text()).not.toContain('时间止损')
    // 开 showAdvanced
    await wrapper.get('input[type="checkbox"]').setValue(true)   // el-switch 渲染为 checkbox
    await flushPromises()
    expect(wrapper.text()).toContain('交易执行')
    expect(wrapper.text()).toContain('时间止损')
  })
```

> selector 备注：el-switch 在 jsdom 下渲染为 `<input type="checkbox" role="switch">`；若 `input[type="checkbox"]` 不命中，改 `wrapper.findAll('.el-switch input')` 或 `getByRole`。断言须真验证（开前高级组不在、开后在），非恒真。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npm run test -- NewReplayDrawer`
Expected: FAIL（新用例——当前 drawer 渲染全部组，高级组默认就在，「not.toContain 交易执行」失败）。

- [ ] **Step 3: 改 NewReplayDrawer.vue —— showAdvanced 分层**

`web/src/components/lab/NewReplayDrawer.vue`：
- `<script setup>` 顶部加：
  ```ts
  import { PARAM_GROUPS, PARAM_META, isCoreGroup } from './paramMeta'
  const showAdvanced = ref(false)
  ```
- `groupedFields` computed 之后再加可见性过滤（或直接在模板过滤）：
  ```ts
  const visibleGroups = computed(() =>
    groupedFields.value.filter((g) => isCoreGroup(g.group) || showAdvanced.value),
  )
  ```
- 模板：`v-for="g in groupedFields"` → `v-for="g in visibleGroups"`；在分组表单上方加开关：
  ```vue
  <div class="drawer-section">
    <el-switch v-model="showAdvanced" active-text="显示高级参数（计划/执行/风控）" />
  </div>
  ```
- `activeGroups`（折叠面板展开项）默认值改为只展开核心组：`const activeGroups = ref<string[]>([...CORE_GROUPS])`（import CORE_GROUPS）。高级组在 showAdvanced 打开后亦可折叠操作。

- [ ] **Step 4: 改 ParamLabView.vue 参数详情面板 —— 同分层**

`web/src/views/ParamLabView.vue`：
- import 加 `isCoreGroup`（PARAM_META/PARAM_GROUPS 已 import）。
- 加 `const showAdvanced = ref(false)`。
- `groupedParamValues` computed 后加 `visibleParamGroups`：
  ```ts
  const visibleParamGroups = computed(() =>
    groupedParamValues.value.filter((g) => isCoreGroup(g.group) || showAdvanced.value),
  )
  ```
- 模板参数详情卡：`v-for="g in groupedParamValues"` → `v-for="g in visibleParamGroups"`；卡内「参数详情」标题旁加 `<el-switch v-model="showAdvanced" size="small" active-text="高级" />`。

- [ ] **Step 5: 跑测试 + typecheck**

Run: `cd web && npm run test -- NewReplayDrawer && npm run typecheck`
Expected: 新分层用例 PASS（+ 既有 NewReplayDrawer 3 用例不回归）；typecheck 净。

- [ ] **Step 6: 全量 vitest 不回归**

Run: `cd web && npm run test`
Expected: 全绿（ParamLabView 冒烟 + caisen facade + DatasetTable + NewReplayDrawer 全过）。

- [ ] **Step 7: 提交**

```bash
git add web/src/components/lab/NewReplayDrawer.vue web/src/components/lab/NewReplayDrawer.spec.ts web/src/views/ParamLabView.vue
git commit -m "feat(lab): 参数表单形态核心/高级分层展示（参数瘦身 Task 2）

NewReplayDrawer + ParamLabView 详情面板：形态核心4组默认展开，交易执行/时间止损/
风控折叠在「显示高级参数」开关后；开关仅控可见性，保留全调参能力。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec 覆盖**：删 6 死字段 → T1 ✓；paramMeta 同步 + confirm_bars 重分组 + CORE_GROUPS → T1 ✓；drawer 分层 → T2 ✓；详情面板分层 → T2 ✓；分层测试 → T2 ✓；sync 守护 → T1 Step5 ✓。
**2. 占位符**：无 TBD；selector 备注给了兜底（el-switch jsdom 渲染）。
**3. 类型一致**：`isCoreGroup`/`CORE_GROUPS`（T1 定义）在 T2 消费处一致；`visibleGroups`/`visibleParamGroups` 命名各自文件内自洽。

---

## Execution

用户已授权连续 subagent 开发（免人工确认）。计划保存 `docs/superpowers/plans/2026-07-14-param-slim.md` → commit → 建 feature 分支 → subagent-driven 逐任务（implementer + reviewer）→ 终审 → 合回 master。
