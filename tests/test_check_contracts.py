# -*- coding: utf-8 -*-
"""前后端契约一致性护栏 check_contracts 的单元测试（TDD：先写测试）。

Why 本护栏存在：前后端契约此前仅靠 web/src/api/*.ts 头注释人工对齐，端点路径/方法/
参数名漂移只能靠运行时 404/422 暴露（曾潜伏鉴权 token 缺口这类生产必崩点）。本脚本
把 FastAPI 的 /openapi.json（权威端点集）与前端 api/*.ts 的 apiClient.<method>('<path>')
调用做静态比对，漂移即阻断，与 check_ports.py 同为 preflight/CI 静态护栏家族。

Why TDD：先钉死纯函数行为（路径参数归一 / TS 调用提取 / openapi 端点提取 / 三态 exit），
再写最小实现。设计上拆成纯函数 + main(backend_spec, ts_files)，测试喂假 openapi dict +
tmp_path 造假 api/*.ts，不依赖 subprocess、不 import 真实 server.main（重依赖隔离，
与 test_check_ports.py 不 import server.core.config 同哲学）。
"""
import sys
from pathlib import Path

# scripts/ 无 __init__.py（namespace 包），直接把该目录加 sys.path 最稳（与
# test_check_ports.py 同款），规避 pytest rootdir 推导歧义。
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_contracts  # noqa: E402


# ============ _norm_path：路径参数占位归一 ============
# 前端 TS 模板字符串写 /plans/${planId}，后端 openapi 写 /plans/{plan_id}，
# 必须归一为同一占位 /plans/{} 才能比对，否则参数名差异会误报漂移。

def test_norm_path_rears_frontend_template_param():
    # ${planId} → {}
    assert check_contracts._norm_path("/api/v1/caisen/plans/${planId}") == "/api/v1/caisen/plans/{}"


def test_norm_path_rears_openapi_param():
    # {plan_id} → {}
    assert check_contracts._norm_path("/api/v1/caisen/plans/{plan_id}") == "/api/v1/caisen/plans/{}"


def test_norm_path_keeps_static_path():
    # 无参数路径原样返回
    assert check_contracts._norm_path("/api/v1/caisen/plans") == "/api/v1/caisen/plans"


# ============ parse_ts_calls：从 TS 文本抽 apiClient.<method>('<path>') ============

def test_parse_ts_calls_single_quote_get():
    # 单引号 + 静态路径（caisen.ts listPlans 实际写法）
    text = "return apiClient.get('/api/v1/caisen/plans', {params: {}})\n"
    assert check_contracts.parse_ts_calls(text) == {("GET", "/api/v1/caisen/plans")}


def test_parse_ts_calls_template_string_with_param():
    # 反引号模板字符串 + ${...}（caisen.ts getPlan 实际写法），参数归一为 {}
    text = "return apiClient.get(`/api/v1/caisen/plans/${encodeURIComponent(planId)}`, {timeout: 10000})\n"
    assert check_contracts.parse_ts_calls(text) == {("GET", "/api/v1/caisen/plans/{}")}


def test_parse_ts_calls_multiple_methods():
    # post/patch/delete 均识别，method 转大写
    text = """
    apiClient.post('/api/v1/caisen/scan', body, {timeout: 30000})
    apiClient.patch('/api/v1/caisen/plans/x', body)
    apiClient.delete('/api/v1/data/y')
    """
    assert check_contracts.parse_ts_calls(text) == {
        ("POST", "/api/v1/caisen/scan"),
        ("PATCH", "/api/v1/caisen/plans/x"),
        ("DELETE", "/api/v1/data/y"),
    }


def test_parse_ts_calls_empty_when_no_apiclient():
    # 无 apiClient 调用（纯类型/工具文件）→ 空集，不误报
    text = "export interface Foo { x: number }\n"
    assert check_contracts.parse_ts_calls(text) == set()


# ============ parse_openapi_endpoints：从 openapi dict 抽 (method, path) ============

def test_parse_openapi_endpoints_basic():
    spec = {"paths": {
        "/api/v1/caisen/plans": {"get": {}},
        "/api/v1/caisen/plans/{plan_id}": {"get": {}, "patch": {}},
    }}
    assert check_contracts.parse_openapi_endpoints(spec) == {
        ("GET", "/api/v1/caisen/plans"),
        ("GET", "/api/v1/caisen/plans/{}"),
        ("PATCH", "/api/v1/caisen/plans/{}"),
    }


def test_parse_openapi_endpoints_ignores_non_method_keys():
    # openapi 每个 path 下除 HTTP method 外还有 parameters 等键，必须只取 HTTP method
    spec = {"paths": {"/api/v1/caisen/scan": {"post": {"parameters": []}}}}
    assert check_contracts.parse_openapi_endpoints(spec) == {("POST", "/api/v1/caisen/scan")}


def test_parse_openapi_endpoints_empty_when_no_paths():
    # spec 缺 paths 或 paths 空 → 空集（main 据此判解析失败 exit 2）
    assert check_contracts.parse_openapi_endpoints({}) == set()
    assert check_contracts.parse_openapi_endpoints({"paths": {}}) == set()


# ============ main：三态 exit code（0 一致 / 1 漂移 / 2 解析失败）============

def _spec(*endpoints):
    """构造 openapi dict：endpoints 为 (path, [METHOD...]) 序列。"""
    paths = {}
    for path, methods in endpoints:
        paths[path] = {m.lower(): {} for m in methods}
    return {"paths": paths}


def _write_ts(tmp_path, name, text):
    """在 tmp_path 造假 api/*.ts，返回路径。"""
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


def test_main_consistent_returns_zero(tmp_path, capsys):
    # 前端调用 ⊆ 后端端点 → 一致放行，静默
    spec = _spec(("/api/v1/caisen/plans", ["GET"]))
    ts = _write_ts(tmp_path, "caisen.ts",
                   "apiClient.get('/api/v1/caisen/plans', {})\n")
    assert check_contracts.main(spec, [ts]) == 0
    assert capsys.readouterr().err == ""


def test_main_drift_returns_one_with_chinese(tmp_path, capsys):
    # 前端调用了后端不存在的端点 → 漂移 exit 1，中文指明漂移路径
    spec = _spec(("/api/v1/caisen/plans", ["GET"]))
    ts = _write_ts(tmp_path, "caisen.ts",
                   "apiClient.post('/api/v1/caisen/ghost', {})\n")
    rc = check_contracts.main(spec, [ts])
    assert rc == 1
    err = capsys.readouterr().err
    assert "契约" in err
    assert "/api/v1/caisen/ghost" in err


def test_main_parse_failure_when_no_paths(tmp_path, capsys):
    # openapi 无 paths → 解析失败 exit 2（与「漂移」明确区分，便于定位是后端异常而非契约不一致）
    ts = _write_ts(tmp_path, "caisen.ts", "apiClient.get('/x', {})\n")
    rc = check_contracts.main({}, [ts])
    assert rc == 2


def test_main_param_alignment_no_drift(tmp_path):
    # 前端 ${planId} 与后端 {plan_id} 归一后应匹配，不误报漂移（核心价值：参数名差异不触发误报）
    spec = _spec(
        ("/api/v1/caisen/plans/{plan_id}", ["GET", "PATCH"]),
        ("/api/v1/caisen/plans/{plan_id}/activate", ["POST"]),
    )
    ts = _write_ts(tmp_path, "caisen.ts", """
    apiClient.get(`/api/v1/caisen/plans/${planId}`)
    apiClient.patch(`/api/v1/caisen/plans/${planId}`, body)
    apiClient.post(`/api/v1/caisen/plans/${planId}/activate`, {})
    """)
    assert check_contracts.main(spec, [ts]) == 0


def test_main_aggregates_multiple_ts_files(tmp_path):
    # 多个 api/*.ts 合并比对（前端 6 个 facade 实际场景）
    spec = _spec(
        ("/api/v1/caisen/plans", ["GET"]),
        ("/api/v1/macro/regime", ["GET"]),
    )
    a = _write_ts(tmp_path, "caisen.ts", "apiClient.get('/api/v1/caisen/plans')\n")
    b = _write_ts(tmp_path, "macro.ts", "apiClient.get('/api/v1/macro/regime')\n")
    assert check_contracts.main(spec, [a, b]) == 0
