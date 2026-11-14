"""waifu2x（waifu2x-ncnn-vulkan）后端。

经典二次元放大/降噪，线条干净但画面泛化弱于 Real-ESRGAN。
官方 Release：https://github.com/nihui/waifu2x-ncnn-vulkan/releases
特点：支持 -s 1 与 -s 2，降噪 0-3 档。
"""

from __future__ import annotations

import os

from .base import Backend, TaskOptions


class Waifu2xBackend(Backend):
    name = "waifu2x"
    label = "waifu2x"
    supported_scales = (1, 2)
    model_choices = ("models-cunet", "models-upconv_7_anime_style_art_rgb")

    def exe_path(self) -> str:
        return self.cfg.get("waifu2x_bin", "")

    def build_cmd(self, src: str, dst: str, opts: TaskOptions) -> list[str]:
        # -1 表示「用引擎默认」，waifu2x 默认给轻微降噪 1 档
        noise = opts.noise if opts.noise >= 0 else 1
        cmd = [
            self.exe_path(),
            "-i", src,
            "-o", dst,
            "-s", str(opts.scale),
            "-n", str(noise),
            "-g", str(opts.gpu),
            "-t", str(opts.tile),
            "-j", opts.thread,
        ]
        models_dir = self.cfg.get("waifu2x_models", "")
        if models_dir:
            cmd += ["-m", self.models_arg(models_dir)]
        if opts.tta:
            cmd.append("-x")
        if opts.fmt:
            cmd += ["-f", opts.fmt]
        if opts.extra:
            cmd += list(opts.extra)
        return cmd
