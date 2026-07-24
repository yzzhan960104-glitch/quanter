# -*- coding: utf-8 -*-
"""resolve_symbols 的 concept universe 分支测试（Plan Task 4）。

物理意图：concept_detail 按概念 id 分页（pro.concept_detail(id=...)），标的池 =
concept.parquet 的 code 列表。resolve_symbols 新增 universe='concept' 分支从湖读 id。
"""
import os
import pandas as pd

import data.tushare_sync as tsync


def test_universe_concept_从概念湖读id(tmp_path, monkeypatch):
    """universe='concept' 应从 data_lake/concept.parquet 读 code 列表返 id 列表。"""
    lake_dir = str(tmp_path / "data_lake")
    os.makedirs(lake_dir, exist_ok=True)
    concept_df = pd.DataFrame({"code": ["TS1", "TS2", "TS3"], "name": ["概念A", "概念B", "概念C"]})
    concept_df.to_parquet(os.path.join(lake_dir, "concept.parquet"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_concept_detail",
                        {"api": "concept_detail", "by": "symbol", "universe": "concept"})
    syms = tsync.resolve_symbols("_test_concept_detail")
    assert syms == ["TS1", "TS2", "TS3"]


def test_universe_concept_概念湖不存在返空(tmp_path, monkeypatch):
    """concept.parquet 不存在时返空列表（不抛，让 sync_dataset 自然 skip）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_concept_detail",
                        {"api": "concept_detail", "by": "symbol", "universe": "concept"})
    syms = tsync.resolve_symbols("_test_concept_detail")
    assert syms == []


def test_universe_concept_支持limit(tmp_path, monkeypatch):
    """universe='concept' + limit 切前 N（编排子集验证用）。"""
    lake_dir = str(tmp_path / "data_lake")
    os.makedirs(lake_dir, exist_ok=True)
    concept_df = pd.DataFrame({"code": ["TS1", "TS2", "TS3"]})
    concept_df.to_parquet(os.path.join(lake_dir, "concept.parquet"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(tsync.TUSHARE_DATASETS, "_test_concept_detail",
                        {"api": "concept_detail", "by": "symbol", "universe": "concept"})
    syms = tsync.resolve_symbols("_test_concept_detail", limit=2)
    assert syms == ["TS1", "TS2"]
