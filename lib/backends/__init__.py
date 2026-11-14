"""后端注册表：新增引擎只要在这里登记一行，并在本目录加一个实现文件。"""

from __future__ import annotations

from .base import Backend, TaskOptions
from .custom import CustomBackend
from .realcugan import RealCUGANBackend
from .realesrgan import RealESRGANBackend
from .waifu2x import Waifu2xBackend

# 注册顺序决定 --list-backends 的显示顺序，第一项不是默认值（默认值看配置）
BACKENDS: dict[str, type[Backend]] = {
    RealESRGANBackend.name: RealESRGANBackend,
    RealCUGANBackend.name: RealCUGANBackend,
    Waifu2xBackend.name: Waifu2xBackend,
    CustomBackend.name: CustomBackend,
}


def create_backend(name: str, engine_cfg: dict) -> Backend:
    """按名字创建后端实例。"""
    cls = BACKENDS.get(name)
    if cls is None:
        allow = "、".join(BACKENDS)
        raise ValueError(f"未知引擎 {name!r}，可选：{allow}")
    return cls(engine_cfg)


__all__ = ["BACKENDS", "Backend", "TaskOptions", "create_backend"]
