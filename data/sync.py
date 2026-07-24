# -*- coding: utf-8 -*-
"""python -m data.sync 入口薄壳（转调 data.sync_cli.main）。

Why 独立 sync.py：python -m data.sync 解析 data/sync.py 模块执行其 __main__ 块。
sync_cli.py 含完整 CLI 逻辑（便于测试 import），sync.py 仅作 -m 入点。
"""
import sys

from data.sync_cli import main

if __name__ == "__main__":
    sys.exit(main())
