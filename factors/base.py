# -*- coding: utf-8 -*-
"""因子元数据、装饰器注册表与扫描器（层级二·决策② = @register_factor 装饰器）。

设计哲学（Karpathy 极简 + 反黑盒）：
- 因子是【函数】（非类）。用装饰器在函数对象上附 __factor_meta__ 并写入全局注册表，
  既有函数签名零改造，只在定义处加一行装饰器注入元数据。
- FactorLoader 镜像 strategies/loader.py 的 importlib 平铺扫描：启动期 import 全部
  factors 子模块触发装饰器副作用注册，运行期只读 _FACTOR_REGISTRY。
- status 三态机：training（训练/调研中）/ live（实盘服役）/ deprecated（退役），
  供 FactorManagerView 按状态分类展示因子矩阵，明确区分服役与训练资产。

输入契约（input_kind）—— 声明因子如何被计算/评估，供 explorer/factor_service 调度：
- returns_panel: (returns: DataFrame[date,symbol], window) → DataFrame 同形状（横截面可秩相关）
- ohlcv_panel:   (returns, high, low, close, ...) → DataFrame 同形状（需 OHLC 面板）
- lake_series:   直接读 DataLakeReader 单序列（北向资金），非横截面，不参与 IC 网格
- cross_section: 逐日截面（估值），非时序面板
- set:           集合型（龙虎榜上榜），非数值，仅做过滤
grid_computable 仅 returns_panel/ohlcv_panel 为 True（可纳入 IC 网格评估）。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal

logger = logging.getLogger(__name__)

# 因子状态三态（与前端 FactorManagerView 矩阵分类同源）
FactorStatus = Literal["training", "live", "deprecated"]
# 因子输入契约（决定是否可纳入 IC 网格评估）
FactorInputKind = Literal["returns_panel", "ohlcv_panel", "lake_series", "cross_section", "set"]

# 扫描时跳过的非因子基础设施模块（仅工具/评估器，不含 @register_factor）
_INFRA_MODULES = {"base", "registry", "analyzer"}


@dataclass
class FactorMeta:
    """因子元数据（资产化的核心载体）。

    所有字段经 /api/v1/factors/registry 反射到前端，前端零硬编码因子名。
    """
    name: str                                   # 唯一标识（与函数名对齐，前端 value）
    label: str                                  # 中文展示名（前端 label）
    category: str                               # 分类：动量/估值/资金流/情绪/技术形态/宏观
    author: str = "系统"
    status: FactorStatus = "training"
    input_kind: FactorInputKind = "returns_panel"
    dataset: str = "daily"                      # 关联数据集 lake key（drill-down 展示数据来源）
    description: str = ""
    # 默认计算参数（如 window=20）；IC 衰减/网格评估时注入，避免调用方猜测签名
    default_params: Dict[str, Any] = field(default_factory=dict)

    @property
    def grid_computable(self) -> bool:
        """是否可纳入 IC 网格评估（仅面板型因子可逐日横截面秩相关）。"""
        return self.input_kind in ("returns_panel", "ohlcv_panel")


@dataclass
class FactorEntry:
    """注册表条目：元数据 + 底层可调用函数。"""
    meta: FactorMeta
    func: Callable[..., Any]


# 全局注册表（装饰器副作用写入；FactorLoader.scan() 触发各模块 import 完成注册）
_FACTOR_REGISTRY: Dict[str, FactorEntry] = {}


def register_factor(meta: FactorMeta):
    """因子注册装饰器：把函数挂入 _FACTOR_REGISTRY，并在函数对象上附 __factor_meta__。

    用法：
        @register_factor(FactorMeta(
            name="cross_sectional_momentum", label="横截面动量",
            category="动量", status="live", input_kind="returns_panel", ...))
        def cross_sectional_momentum(returns, window=20): ...

    重复注册保护：同名因子后者覆盖前者并记 warning（防多模块误注册同名）。
    """
    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        if meta.name in _FACTOR_REGISTRY:
            logger.warning("因子重复注册覆盖: %s", meta.name)
        fn.__factor_meta__ = meta   # 挂函数属性，便于 inspect 反查
        _FACTOR_REGISTRY[meta.name] = FactorEntry(meta=meta, func=fn)
        return fn
    return _wrap


def clear_registry() -> None:
    """清空注册表（仅测试用：隔离用例间的注册污染）。"""
    _FACTOR_REGISTRY.clear()


class FactorLoader:
    """因子注册中心（启动时扫描一次，后续只读）。镜像 strategies/loader.py。

    线程安全：lifespan 启动单线程 scan；运行期 API 只读 _FACTOR_REGISTRY，无并发写。
    """

    def __init__(self, package_name: str = "factors"):
        self._package = package_name

    def scan(self) -> None:
        """import factors 包下全部子模块，触发 @register_factor 副作用注册。

        不收集类/函数（装饰器已写入全局表），只需保证模块被 import。
        单模块 import 失败不阻断整体扫描（如可选重依赖缺失）。
        """
        pkg = importlib.import_module(self._package)
        for _, modname, _ in pkgutil.iter_modules(pkg.__path__):
            if modname in _INFRA_MODULES:
                continue
            try:
                importlib.import_module(f"{self._package}.{modname}")
            except Exception as exc:
                # 单模块失败（如可选依赖缺失）不阻断其它因子注册
                logger.warning("因子模块导入失败 %s.%s: %s", self._package, modname, exc)

    def list(self) -> List[Dict[str, Any]]:
        """反射全部因子元数据（供 GET /factors/registry，前端矩阵数据源）。"""
        out: List[Dict[str, Any]] = []
        for entry in _FACTOR_REGISTRY.values():
            m = entry.meta
            out.append({
                "name": m.name,
                "label": m.label,
                "category": m.category,
                "author": m.author,
                "status": m.status,
                "input_kind": m.input_kind,
                "dataset": m.dataset,
                "description": m.description,
                "grid_computable": m.grid_computable,
                "default_params": dict(m.default_params),
            })
        return out

    def get(self, name: str) -> FactorEntry:
        """按 name 取因子条目（不存在抛 KeyError，路由层转 404）。"""
        if name not in _FACTOR_REGISTRY:
            raise KeyError(f"未注册的因子: {name}，可用: {list(_FACTOR_REGISTRY.keys())}")
        return _FACTOR_REGISTRY[name]

    def list_names(self) -> List[str]:
        """已注册因子名列表。"""
        return list(_FACTOR_REGISTRY.keys())
