# -*- coding: utf-8 -*-
"""
端口一致性护栏 check_ports 的单元测试。

Why TDD：先写测试钉死行为（端口解析 + 三态 exit code），再写最小实现。
设计上把脚本拆成纯函数（parse_*）+ main(文件路径) —— 测试可喂字符串 / tmp_path，
不依赖 subprocess，也不受项目 package 结构（scripts/ 无 __init__.py）影响。
"""
import sys
from pathlib import Path

# scripts/ 无 __init__.py（namespace 包），直接把该目录加 sys.path 最稳，
# 规避 pytest rootdir 推导在不同机器上的歧义。
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_ports  # noqa: E402


# ============ parse_api_port：从 config.py 文本抽 API_PORT 默认值 ============

def test_parse_api_port_extracts_default_with_type_annotation():
    # 形如 API_PORT: int = int(os.getenv("API_PORT", "8000")) —— 与 config.py 实际写法对齐
    # （config.py 所有常量均带类型注解，此处必须反映真实写法，否则测试绿但真实文件解析失败）
    text = 'API_PORT: int = int(os.getenv("API_PORT", "8000"))\n'
    assert check_ports.parse_api_port(text) == 8000


def test_parse_api_port_extracts_without_type_annotation():
    # 无类型注解的赋值写法也必须兼容（防正则放宽后回归）
    text = 'API_PORT = int(os.getenv("API_PORT", "7777"))\n'
    assert check_ports.parse_api_port(text) == 7777


def test_parse_api_port_missing_returns_none():
    # 文本里没有 API_PORT 定义 → None（main 据此判解析失败，exit 2）
    text = 'LOG_CONFIG = {"level": "INFO"}\n'
    assert check_ports.parse_api_port(text) is None


# ============ parse_vite_port：从 vite.config.ts 抽 proxy target 端口 ============

def test_parse_vite_port_ipv4_single_quote():
    # 127.0.0.1 + 单引号（当前仓库修复后的写法）
    text = "        target: 'http://127.0.0.1:8000',\n"
    assert check_ports.parse_vite_port(text) == 8000


def test_parse_vite_port_localhost_double_quote():
    # localhost + 双引号（曾经的错配写法也必须抽得准，防止漏网）
    text = '        target: "http://localhost:8421",\n'
    assert check_ports.parse_vite_port(text) == 8421


# ============ main：三态 exit code（0 一致 / 1 不一致 / 2 解析失败）============

def _write_pair(tmp_path, config_text, vite_text):
    """在 tmp_path 造假的 config.py 与 vite.config.ts，返回各自路径。"""
    cfg = tmp_path / "config.py"
    cfg.write_text(config_text, encoding="utf-8")
    vite = tmp_path / "vite.config.ts"
    vite.write_text(vite_text, encoding="utf-8")
    return cfg, vite


def test_main_consistent_returns_zero(tmp_path, capsys):
    cfg, vite = _write_pair(
        tmp_path,
        'API_PORT = int(os.getenv("API_PORT", "8000"))\n',
        "        target: 'http://127.0.0.1:8000',\n",
    )
    rc = check_ports.main(cfg, vite)
    assert rc == 0
    # 一致：静默放行，stderr 不应有任何输出
    assert capsys.readouterr().err == ""


def test_main_mismatch_returns_one_with_chinese(tmp_path, capsys):
    # 漂移场景：后端 8000 vs vite 8001（正是本次事故的真值）
    cfg, vite = _write_pair(
        tmp_path,
        'API_PORT = int(os.getenv("API_PORT", "8000"))\n',
        "        target: 'http://localhost:8001',\n",
    )
    rc = check_ports.main(cfg, vite)
    assert rc == 1
    err = capsys.readouterr().err
    assert "端口" in err                       # 必须是中文提示
    assert "8000" in err and "8001" in err     # 必须指明两处真值，便于定位


def test_main_parse_failure_returns_two(tmp_path, capsys):
    # config 缺 API_PORT 行 → 解析失败，与「不一致」明确区分（exit 2 vs 1）
    cfg, vite = _write_pair(
        tmp_path,
        'LOG_CONFIG = {"level": "INFO"}\n',
        "        target: 'http://127.0.0.1:8000',\n",
    )
    rc = check_ports.main(cfg, vite)
    assert rc == 2


def test_main_env_override_wins(tmp_path):
    # 决策 2：env API_PORT 覆盖 config 默认值——真相端口取 env，再与 vite 比对。
    # config 默认 8000，但 env=9000 且 vite 也是 9000 → 一致放行。
    cfg, vite = _write_pair(
        tmp_path,
        'API_PORT = int(os.getenv("API_PORT", "8000"))\n',
        "        target: 'http://127.0.0.1:9000',\n",
    )
    rc = check_ports.main(cfg, vite, env_port="9000")
    assert rc == 0
