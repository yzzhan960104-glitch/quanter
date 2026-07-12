#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
scripts/dingtalk_claude_bridge.py
================================
钉钉桥 thin 入口（项目 scripts/ 惯例），功能等价 `python -m bridge`。

Why 单独存在一个 scripts/ 入口：
  - 与项目其它可执行脚本（emt_smoke / sync_data_lake 等）保持一致的调用形式
    `python scripts/xxx.py`；
  - 直接 `python -m bridge` 已可用，本文件只是等价的便捷别名，不重复业务逻辑。
"""
import sys
from pathlib import Path

# 把项目根加 sys.path，使 `python scripts/dingtalk_claude_bridge.py` 在任意
# cwd 下都能 import bridge.* （否则需先 cd 到项目根或配 PYTHONPATH）。
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.__main__ import main  # noqa: E402  (sys.path 注入后再 import)


if __name__ == "__main__":
    main()
