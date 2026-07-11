# -*- coding: utf-8 -*-
"""
端口一致性护栏（preflight 脚本）。

Why 存在：前后端端口约定此前无任何机器校验——`web/vite.config.ts` 的 proxy target
与后端 `uvicorn --port` 各写各的，漂移只能靠运行时 `ECONNREFUSED` 暴露（曾出现
vite 误写成 8001、后端实为 8000 的事故）。本脚本在 `npm run dev` 的 predev 钩子
里前置比对两侧端口，不一致即 `sys.exit(1)` 阻断 dev server，并以中文指明两处真值。

设计（反黑盒 / 极简）：
- 纯函数 parse_* + main(文件路径)，CLI 仅薄封装，便于单测喂字符串 / tmp_path。
- 零第三方依赖（仅标准库），且刻意 **不 import server.core.config**——避免 import 链
  拉起 fastapi/uvicorn 重依赖，保证脚本启动快、CI 友好、venv 未装全也能跑。
- 端口用正则从源码文本提取：后端真相源是 `config.py` 的 API_PORT（os.getenv 可覆盖），
  前端真相源是 `vite.config.ts` 的 proxy target。双写 + 比对，而非跨语言物理单源。
"""
import os
import re
import sys
from pathlib import Path
from typing import Optional

# 项目根锚定：scripts/check_ports.py → scripts/ → 项目根（与运行 cwd 无关）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "server" / "core" / "config.py"
DEFAULT_VITE_PATH = PROJECT_ROOT / "web" / "vite.config.ts"

# config.py 实际写法：API_PORT: int = int(os.getenv("API_PORT", "8000"))
# `[^=\n]*` 吃掉 API_PORT 与 = 之间可选的类型注解（: int），兼容带/不带注解两种写法；
# 抓 os.getenv 第二个字符串字面量（默认值）；兼容单/双引号与任意空格。
_API_PORT_RE = re.compile(
    r'API_PORT\b[^=\n]*=\s*int\(\s*os\.getenv\(\s*["\']API_PORT["\']\s*,\s*["\'](\d+)["\']\s*\)\s*\)'
)
# vite.config.ts 实际写法：target: 'http://127.0.0.1:8000' 或 "http://localhost:8421"
# 抓 URL 端口；[^:/]+ 吃掉 host（127.0.0.1 / localhost 均可），单/双引号兼容。
_VITE_PORT_RE = re.compile(r"target\s*:\s*['\"]https?://[^:/]+:(\d+)")


def parse_api_port(text: str) -> Optional[int]:
    """从 config.py 源码文本提取 API_PORT 默认值；未定义返回 None。"""
    m = _API_PORT_RE.search(text)
    return int(m.group(1)) if m else None


def parse_vite_port(text: str) -> Optional[int]:
    """从 vite.config.ts 源码文本提取 proxy target 端口；未匹配返回 None。"""
    m = _VITE_PORT_RE.search(text)
    return int(m.group(1)) if m else None


def main(
    config_path: Path,
    vite_path: Path,
    *,
    env_port: Optional[str] = None,
) -> int:
    """比对后端端口与 vite 代理端口，返回 exit code。

    真相端口优先取 env_port（对应运行时 os.getenv("API_PORT")），未提供或为空
    则回落到 config.py 的 API_PORT 默认值。

    返回码：
      0 —— 一致，静默放行
      1 —— 不一致（端口漂移），stderr 打印中文修复指引
      2 —— 解析失败（文件读不到 / 正则无匹配 / env 非法），与「不一致」区分以便定位
    """
    # 读两侧配置源文件；缺文件归入解析失败（exit 2），不与漂移混淆
    try:
        cfg_text = Path(config_path).read_text(encoding="utf-8")
        vite_text = Path(vite_path).read_text(encoding="utf-8")
    except OSError as e:
        print(f"[端口护栏] 无法读取配置文件：{e}", file=sys.stderr)
        return 2

    backend_port = parse_api_port(cfg_text)
    vite_port = parse_vite_port(vite_text)

    # 任一侧解析不到端口 → 文件格式异常（如有人改了 config 写法），单独 exit 2
    if backend_port is None or vite_port is None:
        print(
            f"[端口护栏] 无法解析端口：backend={backend_port}, vite={vite_port}。"
            f"请检查 {config_path} 含 `API_PORT = int(os.getenv(...))` 定义、"
            f"{vite_path} 含 proxy `target: 'http://host:port'`。",
            file=sys.stderr,
        )
        return 2

    # 环境变量覆盖优先：以显式 API_PORT 为后端真相端口（与运行时语义一致）
    if env_port is not None and env_port.strip() != "":
        try:
            backend_port = int(env_port)
        except ValueError:
            print(
                f"[端口护栏] 环境变量 API_PORT 非法（非数字）：{env_port!r}",
                file=sys.stderr,
            )
            return 2

    if backend_port != vite_port:
        print(
            f"[端口护栏] 端口不一致：后端 API_PORT={backend_port}，"
            f"vite proxy target={vite_port}。\n"
            f"  修复：将 web/vite.config.ts 的 proxy target 改为 "
            f"http://127.0.0.1:{backend_port}，或调整后端端口使两者对齐。\n"
            f"  （若以 API_PORT 环境变量覆盖了后端端口，须同步 vite.config.ts。）",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    # CLI 入口：无参，锚定项目内默认两文件；env_port 取运行时 API_PORT 环境变量
    sys.exit(
        main(
            DEFAULT_CONFIG_PATH,
            DEFAULT_VITE_PATH,
            env_port=os.getenv("API_PORT"),
        )
    )
