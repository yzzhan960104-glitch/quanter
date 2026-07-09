# Task 4 报告：颈线线性回归（蔡森形态学 Phase 2）

## 状态
GREEN — 3/3 测试通过，caisen 全量 11/11 通过。

## TDD 流程

### RED（确认失败）
```
tests/caisen/test_neckline.py → ImportError: cannot import name 'neckline'
from 'caisen.patterns' ... 1 error in 0.24s
```
符合预期：模块尚未实现。

### GREEN（实现后通过）
```
tests/caisen/test_neckline.py::test_horizontal_neckline PASSED
tests/caisen/test_neckline.py::test_rising_neckline    PASSED
tests/caisen/test_neckline.py::test_declining_slope_negative PASSED
3 passed in 0.14s
```
caisen 全量回归：`11 passed`（config 4 + neckline 3 + zigzag 4），未破坏 Task 3。

## 实现说明

**文件：**
- `caisen/patterns/neckline.py`（新建，54 行实质代码）
- `tests/caisen/test_neckline.py`（新建，3 测试）

**三个函数：**

| 函数 | 签名 | 物理意图 |
|------|------|----------|
| `slope(p1, p2)` | `(tuple, tuple) -> float` | 两点斜率；idx 相同返回 0.0 防除零 |
| `fit_line(points, at)` | `(list[tuple], int) -> float` | numpy polyfit degree=1，返回 x=at 处 y |
| `neckline_at(t, *pts)` | `(int, *float) -> float` | 颈线在 t 处价；pts 按 (idx,price) 两两分组 |

**设计选择：**
- 纯 numpy `polyfit(xs, ys, 1)` 显式实现，符合 CLAUDE.md「极简无黑盒」原则；
- 两点输入时 polyfit 退化为两点连线（解析解），多点时为最小二乘回归（多点颈线更稳，可平滑噪声极值）——为 Task 6/7 多点颈线预留扩展，但非过度抽象；
- `trendln` 装不上且为黑盒，留待 screener 层（Task 8）做交叉校验，核心回归保持可审计。

**风控边界：**
- `slope` 对 `p2[0]==p1[0]`（同 idx 垂直线）返回 0.0，避免 ZeroDivisionError；
- polyfit 对 xs 全相等会触发 RankWarning 返回 NaN，调用方需保证至少两个不同 idx（已在 docstring 标注，颈线由两个不同 pivot 连成天然满足）。

## 自审（CLAUDE.md Definition of Done）

- [x] **语言审查**：对话、注释、docstring 全中文，行内注释解释 What + Why。
- [x] **反魔法审查**：未引入 trendln 等黑盒；纯 numpy 一阶回归，数学直白。
- [x] **边界审查**：除零（同 idx）已显式 `if ... else 0.0` 处理；多点 NaN 风险在 docstring 标注（颈线输入天然不触发）。
- [x] **事实审查**：`np.polyfit(xs, ys, 1)` 返回 `[k, b]` 使 `y = k*x + b`，已验证（上倾测试 t=5 → 11.0 精确匹配）。

## 测试覆盖矩阵

| 测试 | 形态 | 输入 | 期望 | 验证点 |
|------|------|------|------|--------|
| `test_horizontal_neckline` | 水平 | (0,10),(10,10) t=5 | 10.0 | 等高 → 任一点=峰价，斜率 0 |
| `test_rising_neckline` | 上倾 | (0,10),(10,12) t=5 | 11.0 | 中点线性插值精确 |
| `test_declining_slope_negative` | 下倾 | (0,12),(10,10) | slope<0 | 斜率符号正确 |

## Concerns / Follow-up

1. **多点颈线尚未测试**：`fit_line` 支持多点最小二乘但本轮无测试覆盖（brief 仅要求两点）。建议 Task 6（W底）/ Task 7（头肩底）实现时，若用到多点颈线（如头肩底两肩低点 + 头底共三点拟合），补多点回归 + RankWarning 场景测试。
2. **NaN 传播未断言**：polyfit 对退化输入（xs 全相等）返回 NaN，目前依赖调用方保证输入合法。后续若颈线输入来自用户配置或外部 pivot，应在 `fit_line` 加 `xs.std() == 0` 前置校验并 raise 而非静默返回 NaN。
3. **无类型注解的 tuple 元素**：`p1: tuple` 未标 `Tuple[int, float]`，brief 原样保留；Python 3.10 下可读性可接受，后续若接 mypy 严格模式再细化。

## Commits
- `bfb56a8` feat(caisen): 颈线线性回归（numpy polyfit 显式实现）

## 报告路径
`C:\Users\yzzhan\Desktop\quanter\.superpowers\sdd\task-4-report.md`
