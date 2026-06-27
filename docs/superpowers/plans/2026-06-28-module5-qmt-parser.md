# 模块⑤ QMT 本地解析器 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 预留 `data/qmt_parser.py` 的 `QMTDataParser`，通过只读读取 miniQMT 本地 SQLite 行情库，清洗对齐为系统标准 OHLCV DataFrame（tz-aware Asia/Shanghai）。

**Architecture:** 实盘网络 API 有延迟且受限，QMT 在本地导出极速 SQLite 行情。QMTDataParser 与 `DataFetcher` 同构（同 fetch_ohlcv 接口），后续可作为 `CompositeDataFetcher` 的快车道 fetcher。表名/字段集中在类常量，便于按实际 miniQMT schema 调整。

**Tech Stack:** Python 3 标准库 `sqlite3`、pandas（`read_sql`）；无新依赖。

## Global Constraints

- 严禁 PyQt/GUI；全中文注释（含 Why）；扁平反黑盒
- 只读连接（`mode=ro` URI + `PRAGMA query_only=ON`），防误写 QMT 库
- time 字段单位 try-except 兼容（字符串/epoch 秒/毫秒）；非法价（≤0/NaN/Inf）过滤
- 返回标准 OHLCV：列 `['open','high','low','close','volume','amount']`，tz-aware DatetimeIndex
- 表名/字段为假设（集中类常量），需按实际 miniQMT schema 调整

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `data/qmt_parser.py` | `QMTDataParser`（SQLite 只读 + 标准化） | 新建 |
| `tests/test_data.py` | 已存在，追加 `TestQMTDataParser` | 修改 |

**依赖**：无（独立数据源模块）。

---

## Task 1: `QMTDataParser`（SQLite 只读 + 标准化 OHLCV）

**Files:**
- Create: `data/qmt_parser.py`
- Test: `tests/test_data.py`（追加 `TestQMTDataParser`）

**Interfaces:**
- Consumes: 无
- Produces: `QMTDataParser(db_path).fetch_ohlcv(symbol, start, end, freq) -> pd.DataFrame`

- [ ] **Step 1: 写失败测试**

在 `tests/test_data.py` 追加（顶部 import 区补）：
```python
import sqlite3
from datetime import datetime
from data.qmt_parser import QMTDataParser
```

```python
@pytest.fixture
def qmt_db(tmp_path):
    """构造内存样例 miniQMT SQLite 库（stockbar 表）"""
    db = tmp_path / "qmt.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE stockbar ("
        "code TEXT, time TEXT, open REAL, high REAL, low REAL, "
        "close REAL, volume REAL, amount REAL)"
    )
    rows = [
        ("600000.SH", "2023-01-03", 10.0, 10.5, 9.8, 10.2, 1e6, 1e7),
        ("600000.SH", "2023-01-04", 10.2, 10.8, 10.0, 10.6, 1.2e6, 1.2e7),
        ("600000.SH", "2023-01-05", 10.6, 10.7, 10.1, 10.3, 0.9e6, 0.9e7),
    ]
    conn.executemany("INSERT INTO stockbar VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return str(db)


class TestQMTDataParser:
    """测试 QMT 本地 SQLite 解析器"""

    def test_fetch_returns_standard_ohlcv_columns(self, qmt_db):
        """返回标准 OHLCV 列"""
        parser = QMTDataParser(qmt_db)
        df = parser.fetch_ohlcv("600000.SH",
                                datetime(2023, 1, 1), datetime(2023, 1, 31))
        assert list(df.columns) == ["open", "high", "low", "close", "volume", "amount"]

    def test_fetch_index_is_tz_aware(self, qmt_db):
        """索引为 tz-aware (Asia/Shanghai)"""
        parser = QMTDataParser(qmt_db)
        df = parser.fetch_ohlcv("600000.SH",
                                datetime(2023, 1, 1), datetime(2023, 1, 31))
        assert isinstance(df.index, pd.DatetimeIndex)
        assert str(df.index.tz) == "Asia/Shanghai"

    def test_fetch_filters_by_date_range(self, qmt_db):
        """按日期范围过滤"""
        parser = QMTDataParser(qmt_db)
        df = parser.fetch_ohlcv("600000.SH",
                                datetime(2023, 1, 4), datetime(2023, 1, 4))
        assert len(df) == 1

    def test_fetch_sorted_ascending(self, qmt_db):
        """时间正序"""
        parser = QMTDataParser(qmt_db)
        df = parser.fetch_ohlcv("600000.SH",
                                datetime(2023, 1, 1), datetime(2023, 1, 31))
        assert df.index.is_monotonic_increasing

    def test_fetch_other_symbol_empty(self, qmt_db):
        """其他标的返回空 DataFrame"""
        parser = QMTDataParser(qmt_db)
        df = parser.fetch_ohlcv("000001.SZ",
                                datetime(2023, 1, 1), datetime(2023, 1, 31))
        assert len(df) == 0

    def test_invalid_prices_filtered(self, tmp_path):
        """非法价（≤0）被过滤"""
        db = tmp_path / "bad.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE stockbar (code TEXT, time TEXT, open REAL, high REAL, "
            "low REAL, close REAL, volume REAL, amount REAL)"
        )
        conn.execute(
            "INSERT INTO stockbar VALUES ('600000.SH','2023-01-03',0,0,0,0,1e6,1e7)"
        )
        conn.commit(); conn.close()

        parser = QMTDataParser(str(db))
        df = parser.fetch_ohlcv("600000.SH",
                                datetime(2023, 1, 1), datetime(2023, 1, 31))
        assert len(df) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_data.py::TestQMTDataParser -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'data.qmt_parser'`

- [ ] **Step 3: 写最小实现**

新建 `data/qmt_parser.py`：
```python
"""QMT 本地 SQLite 行情解析器（借鉴 OSkhQuant miniQMT_data_parser.py）

实盘网络 API 有延迟且受限，miniQMT 在本地导出极速 SQLite 行情库。
本解析器只读该库，清洗对齐为系统标准 OHLCV DataFrame。

设计：
- 只读连接（mode=ro URI + PRAGMA query_only），防误写 QMT 库
- 与 DataFetcher 同构（同 fetch_ohlcv 接口），可作 CompositeDataFetcher 快车道
- 表名/字段集中在类常量，按实际 miniQMT schema 调整
"""
import sqlite3
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


class QMTDataParser:
    """miniQMT 本地 SQLite 行情解析器"""

    # ====== schema 常量（按实际 miniQMT 库调整） ======
    TABLE = "stockbar"
    CODE_COL = "code"
    TIME_COL = "time"

    def __init__(self, db_path: str):
        # 只读 URI 连接，避免误写 QMT 库
        self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        self._conn.execute("PRAGMA query_only = ON")

    def fetch_ohlcv(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        freq: str = "1d",
    ) -> pd.DataFrame:
        """读取标的 OHLCV，返回标准 DataFrame（tz-aware Asia/Shanghai）"""
        sql = (
            f"SELECT {self.TIME_COL}, open, high, low, close, volume, amount "
            f"FROM {self.TABLE} "
            f"WHERE {self.CODE_COL} = ? AND {self.TIME_COL} BETWEEN ? AND ? "
            f"ORDER BY {self.TIME_COL}"
        )
        df = pd.read_sql(
            sql, self._conn,
            params=(symbol, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
        )
        return self._normalize(df)

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """清洗对齐：时间索引化 + tz + 非法价过滤 + 标准列"""
        if df.empty:
            # 空结果也要返回标准列结构
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "amount"],
                index=pd.DatetimeIndex([], tz="Asia/Shanghai"),
            )

        # time 字段单位兼容（字符串/epoch），coerce 容错
        df[self.TIME_COL] = pd.to_datetime(df[self.TIME_COL], errors="coerce")
        df = df.dropna(subset=[self.TIME_COL]).set_index(self.TIME_COL)

        # tz-aware：naive → localize，aware → convert
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Shanghai")
        else:
            df.index = df.index.tz_convert("Asia/Shanghai")

        # 非法价过滤（≤0 / NaN / Inf）
        for col in ("open", "high", "low", "close"):
            df = df[df[col].notna() & np.isfinite(df[col]) & (df[col] > 0)]

        return df[["open", "high", "low", "close", "volume", "amount"]]

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_data.py::TestQMTDataParser -v`
Expected: PASS — 6 个测试全绿

- [ ] **Step 5: 回归测试**

Run: `python -m pytest tests/ -v`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add data/qmt_parser.py tests/test_data.py
git commit -m "feat(data): 新增 QMTDataParser 本地 SQLite 行情解析器"
```

---

## 验收标准

- [ ] `data/qmt_parser.py` 含 `QMTDataParser`，只读连接，返回标准 tz-aware OHLCV
- [ ] 非法价过滤、日期范围过滤、时间正序、空标的返回空 DataFrame
- [ ] `python -m pytest tests/ -v` 全绿
- [ ] 1 个独立 commit

## 后续衔接

QMT 解析器就绪。后续可作为 `CompositeDataFetcher` 的快车道 fetcher 接入（YAGNI，本计划不含）。下一份（最后一份）plan 为模块④ 调度引擎。
