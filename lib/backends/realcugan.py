"""Real-CUGAN（realcugan-ncnn-vulkan）后端。

B 站二次元超分模型，降噪分档，对 JPG 压缩噪点比 Real-ESRGAN 更干净。
官方 Release：https://github.com/nihui/realcugan-ncnn-vulkan/releases
特点：支持 -s 1（只降噪重绘、不放大），对应「x1」场景。
"""

from __future__ import annotations

import os

from .base import Backend, TaskOptions


class RealCUGANBackend(Backend):
    name = "realcugan"
    label = "Real-CUGAN"
    # 模型包只有 up2x / up3x / up4x，没有 1 倍模型，所以不支持 scale=1
    supported_scales = (2, 3, 4)
    model_choices = ("models-se", "models-pro", "models-nose")

    def exe_path(self) -> str:
        return self.cfg.get("realcugan_bin", "")

    def build_cmd(self, src: str, dst: str, opts: TaskOptions) -> list[str]:
        cmd = [
            self.exe_path(),
            "-i", src,
            "-o", dst,
            "-s", str(opts.scale),
            "-n", str(opts.noise),
            "-g", str(opts.gpu),
            "-t", str(opts.tile),
            "-j", opts.thread,
        ]
        models_dir = self.cfg.get("realcugan_models", "")
        if models_dir:
            cmd += ["-m", self.models_arg(models_dir)]
        if opts.tta:
            cmd.append("-x")
        if opts.fmt:
            cmd += ["-f", opts.fmt]
        if opts.extra:
            cmd += list(opts.extra)
        return cmd
