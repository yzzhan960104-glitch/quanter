# -*- coding: utf-8 -*-
"""paramMeta.ts 中文映射 ↔ StrategyConfig 字段集 跨层同步守护（Spec 2 Task 3）。

物理意图：paramMeta.ts 的 PARAM_META 是前端「中文标题+分组」单一真相源，必须覆盖
StrategyConfig 全部字段，否则 /lab 参数面板/抽屉会漏字段或留英文键。本测试读 paramMeta.ts
文本正则抽键，与 StrategyConfig.model_fields 双向比对——config.py 加字段时自动失败，
强制 paramMeta.ts 同步补条目（防漂移）。
"""
import re
from pathlib import Path

from caisen.config import StrategyConfig

PARAM_META_TS = Path(__file__).resolve().parents[1] / "web" / "src" / "components" / "lab" / "paramMeta.ts"


def _extract_param_meta_keys() -> set[str]:
    """从 paramMeta.ts 文本正则抽取 PARAM_META 的键（形如 `field_name: {`）。"""
    text = PARAM_META_TS.read_text(encoding="utf-8")
    # 匹配 PARAM_META 对象体内的 `  name: {` 行（缩进 + 合法标识符 + 冒号 + 空格 + {）
    block = text.split("PARAM_META", 1)[1]
    # 截到 PARAM_META 对象闭合（首个仅含 `}` 的行）
    block = block.split("}\n", 1)[0]
    return set(re.findall(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*):\s*\{", block, re.MULTILINE))


def test_param_meta_covers_all_strategy_fields():
    """PARAM_META 键集 == StrategyConfig 字段集（双向匹配，防漏/防孤儿）。"""
    assert PARAM_META_TS.exists(), f"paramMeta.ts 不存在：{PARAM_META_TS}"
    cfg_fields = set(StrategyConfig.model_fields.keys())
    meta_keys = _extract_param_meta_keys()
    missing = cfg_fields - meta_keys          # config 有、paramMeta 漏（漏字段→面板缺项）
    orphan = meta_keys - cfg_fields           # paramMeta 有、config 无（拼写错/已删字段）
    assert not missing, f"paramMeta.ts 漏字段（补中文标题）：{sorted(missing)}"
    assert not orphan, f"paramMeta.ts 孤儿键（拼写错或字段已删）：{sorted(orphan)}"
