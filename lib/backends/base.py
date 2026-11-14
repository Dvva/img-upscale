"""后端抽象层。

每个 AI 放大引擎实现一个 Backend 子类，主流程只调用统一接口，
新增引擎时不需要改动调度、配置、命名等逻辑。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field


@dataclass
class TaskOptions:
    """单个文件的放大参数（由「默认值 + 命中规则 + 命令行」合并得到）。"""

    scale: int = 2
    model: str = ""
    tile: int = 0
    noise: int = -1
    thread: str = "1:2:2"
    gpu: int = 0
    tta: bool = False
    fmt: str = "png"
    extra: list[str] = field(default_factory=list)


class Backend:
    """放大引擎基类。"""

    #: 配置里 engine.backend 的取值
    name: str = "base"
    #: 给人看的名字
    label: str = "基础"
    #: 该引擎原生支持的放大倍数
    supported_scales: tuple[int, ...] = (2, 3, 4)
    #: 该引擎可用的模型名（仅用于提示）
    model_choices: tuple[str, ...] = ()
    #: 已知问题提示（会出现在 --list-backends 里）
    warning: str = ""

    def __init__(self, engine_cfg: dict):
        self.cfg = engine_cfg or {}

    # ---------- 子类实现 ----------

    def exe_path(self) -> str:
        raise NotImplementedError

    def build_cmd(self, src: str, dst: str, opts: TaskOptions) -> list[str]:
        raise NotImplementedError

    # ---------- 通用能力 ----------

    def check_scale(self, scale: int) -> tuple[bool, str]:
        if scale not in self.supported_scales:
            allow = "/".join(str(s) for s in self.supported_scales)
            return False, f"{self.label} 只支持 {allow} 倍，收到 {scale}"
        return True, ""

    def available(self) -> tuple[bool, str]:
        exe = self.exe_path()
        if not exe:
            return False, f"{self.label}：配置里没有填写可执行文件路径"
        # 允许直接写命令名（走 PATH）
        if os.path.sep not in exe and "/" not in exe:
            if shutil.which(exe):
                return True, ""
            return False, f"{self.label}：PATH 里找不到 {exe}"
        if not os.path.isfile(exe):
            return False, f"{self.label}：文件不存在 {exe}"
        return True, ""

    def describe(self) -> str:
        ok, msg = self.available()
        state = "可用" if ok else "不可用"
        text = f"{self.name:<12} {self.label:<14} {state}  {msg}".rstrip()
        if self.warning:
            text += f"  [注意] {self.warning}"
        return text

    def workdir(self) -> str | None:
        """引擎以自身所在目录为基准解析相对路径，子进程要在这个目录下跑。"""
        exe = self.exe_path()
        if not exe:
            return None
        if os.path.sep not in exe and "/" not in exe:
            found = shutil.which(exe)
            return os.path.dirname(os.path.abspath(found)) if found else None
        return os.path.dirname(os.path.abspath(exe))

    def models_arg(self, path: str) -> str:
        """把模型目录转成引擎能认的相对路径。

        ncnn-vulkan 系列会把 -m 的值拼在 exe 目录后面再找模型文件，
        传绝对路径会变成 exe目录\\abs\\... 直接失败，所以这里转相对。
        """
        if not path:
            return path
        workdir = self.workdir()
        if not workdir or not os.path.isabs(path):
            return path
        try:
            rel = os.path.relpath(os.path.abspath(path), workdir)
        except ValueError:
            return path
        return path if rel.startswith("..") else rel
