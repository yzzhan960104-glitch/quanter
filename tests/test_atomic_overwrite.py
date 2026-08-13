# -*- coding: utf-8 -*-
"""G5：safe_overwrite 原子写入单测（T13-A write-side 守卫的原子化升级）。

物理意图（2026-08-13 g-wave-p0-guards · Task G5）：
    原 safe_overwrite 仅做行数守卫，落盘由调用方紧跟的 ``df.to_parquet(path)`` 直写目标——
    写入中途 OOM/断电/磁盘满会留半截损坏 parquet，下次 read_parquet 抛 EOFError/读不全，
    需全量回采（生产湖 1020 万行 × 5000 标的）才能恢复。本测试覆盖升级后的「tmp + fsync +
    os.replace」原子语义：

      ① test_atomic_overwrite_preserves_original_on_failure：
         to_parquet 抛异常 → 原文件字节不变 + .tmp 被清理（不留半截）；
      ② test_atomic_overwrite_normal_replaces：
         正常写 → 新内容原子替换旧文件（read_parquet 拿到新内容）；
      ③ test_atomic_overwrite_first_write_creates_file：
         首次写（无现有文件）→ tmp + os.replace 创建目标文件 + 无残留 tmp；
      ④ test_atomic_overwrite_guard_rejects_no_write：
         守卫拒写场景（骤降）→ 守卫在原子写之前抛错，未触 tmp、原文件不变
         （守卫硬阻断语义与升级前一致）。

RED 依据：当前 safe_overwrite 只验不写，``safe_overwrite(path, df)`` 后 read_parquet(path)
拿到的是旧内容（或首次写场景下文件根本不存在）→ ②/③ FAIL。
"""
from __future__ import annotations

import pandas as pd
import pytest

from data.integrity import WriteGuardError, existing_row_count, safe_overwrite


def test_atomic_overwrite_preserves_original_on_failure(tmp_path, monkeypatch):
    """写中途 to_parquet 抛异常 → 原文件字节不变 + .tmp 清理（不留半截）。

    物理意图：原子写的核心收益——异常时原文件不损。tmp 写失败抛错，os.replace 未执行，
    target 完整保留旧内容；finally/except 清理 tmp 防遗留干扰下次写。
    """
    target = tmp_path / "lake.parquet"
    # 旧文件：100 行（守卫基线）
    pd.DataFrame({"a": range(100)}).to_parquet(target, engine="pyarrow")
    old_bytes = target.read_bytes()

    # mock to_parquet 抛 RuntimeError（模拟 OOM/磁盘满/异常），触发原子写失败分支
    def _raise_on_write(self, path, **kw):
        raise RuntimeError("模拟写入失败（OOM/磁盘满）")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", _raise_on_write)

    # 用 50 行新 df 守卫会触发骤降拒写（100→50 < 100×0.9=90）——为了聚焦「原子写失败」
    # 而非「守卫拒写」，用 QUANTER_FORCE_WRITE=1 旁路守卫，让流程走到 to_parquet 抛错分支。
    monkeypatch.setenv("QUANTER_FORCE_WRITE", "1")

    new_df = pd.DataFrame({"a": range(50)})
    with pytest.raises(RuntimeError, match="模拟写入失败"):
        safe_overwrite(str(target), new_df)

    # 原文件字节不变（守卫通过但写失败，os.replace 未执行，target 完整保留）
    assert target.read_bytes() == old_bytes
    # .tmp 被清理（异常分支 finally 清理，不留半截文件干扰下次写）
    tmp_residue = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert not tmp_residue, f"safe_overwrite 异常后残留 tmp：{tmp_residue}"


def test_atomic_overwrite_normal_replaces(tmp_path):
    """正常写 → 新内容原子替换（read_parquet 拿到新内容，行数 = 新 df）。

    RED 依据：当前 safe_overwrite 只验不写，本测调用 safe_overwrite 后立即 read_parquet，
    拿到的仍是旧 100 行（升级前调用方需紧跟 to_parquet 才写盘）→ FAIL，证明原子写未实现。
    """
    target = tmp_path / "lake.parquet"
    pd.DataFrame({"a": range(100)}).to_parquet(target, engine="pyarrow")

    # 增长写（150 > 100，守卫放行）
    new_df = pd.DataFrame({"a": range(150)}
                          ) if False else pd.DataFrame({"a": range(150)})
    safe_overwrite(str(target), new_df)

    got = pd.read_parquet(target)
    assert len(got) == 150, "新内容应原子替换旧文件（read_parquet 拿到新 150 行）"
    # 无残留 tmp（os.replace 成功后 tmp 已重命名为 target）
    tmp_residue = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert not tmp_residue


def test_atomic_overwrite_first_write_creates_file(tmp_path):
    """首次写（无现有文件）→ tmp + os.replace 创建新文件 + 无残留 tmp。

    RED 依据：当前 safe_overwrite 只验不写，首次写场景下文件根本不会被创建 → FAIL。
    """
    target = tmp_path / "new.parquet"
    new_df = pd.DataFrame({"a": range(50)})

    safe_overwrite(str(target), new_df)

    assert target.exists(), "首次写应创建目标文件"
    got = pd.read_parquet(target)
    assert len(got) == 50
    # 无残留 tmp
    tmp_residue = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert not tmp_residue


def test_atomic_overwrite_guard_rejects_no_write(tmp_path):
    """守卫拒写场景（骤降）→ 守卫在原子写之前抛错，未触 tmp、原文件不变。

    物理意图：守卫硬阻断语义不变——拒写时不留任何写入痕迹（无 tmp、target 不动），
    这是与「原子写失败」不同的路径：守卫在 to_parquet 之前抛错，原子写根本未启动。
    """
    target = tmp_path / "lake.parquet"
    pd.DataFrame({"a": range(1000)}).to_parquet(target, engine="pyarrow")

    # 骤降（1000 → 10 < 900）→ 守卫拒写
    tiny = pd.DataFrame({"a": range(10)})
    with pytest.raises(WriteGuardError):
        safe_overwrite(str(target), tiny)

    # 原文件不变
    assert existing_row_count(str(target)) == 1000
    # 无 tmp（守卫在原子写之前抛错，根本未进 tmp 分支）
    tmp_residue = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
    assert not tmp_residue
