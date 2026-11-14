"""Real-ESRGAN（realesrgan-ncnn-vulkan）后端。

官方 Release：https://github.com/xinntao/Real-ESRGAN/releases
注意：该引擎的 -s 只接受 2/3/4，没有 1 倍。
"""

from __future__ import annotations

import os

from .base import Backend, TaskOptions


class RealESRGANBackend(Backend):
    name = "realesrgan"
    label = "Real-ESRGAN"
    supported_scales = (2, 3, 4)
    model_choices = (
        "realesrgan-x4plus-anime",   # 二次元专用，体积小、速度快
        "realesrgan-x4plus",         # 通用照片/插画
        "realesr-animevideov3",      # 动画视频帧，最快
        "realesrnet-x4plus",         # 无 GAN，线条保守
    )
    # 本机实测：分块即错位、不分块写不出完整图，暂不可用
    warning = "2022 版在这台机器上 tile 分块错位，出图不可用；换 realcugan 或 waifu2x"

    def exe_path(self) -> str:
        return self.cfg.get("realesrgan_bin", "")

    def build_cmd(self, src: str, dst: str, opts: TaskOptions) -> list[str]:
        cmd = [
            self.exe_path(),
            "-i", src,
            "-o", dst,
            "-s", str(opts.scale),
            "-n", opts.model or "realesrgan-x4plus-anime",
            "-g", str(opts.gpu),
            "-t", str(opts.tile),
            "-j", opts.thread,
        ]
        models_dir = self.cfg.get("realesrgan_models", "")
        if models_dir:
            cmd += ["-m", self.models_arg(models_dir)]
        if opts.tta:
            cmd.append("-x")
        if opts.fmt:
            cmd += ["-f", opts.fmt]
        if opts.extra:
            cmd += list(opts.extra)
        return cmd
