"""自定义后端：把任意命令行程序接进来。

配置示例（config.toml）：

    [engine.custom]
    bin  = "D:\\\\tools\\\\my-upscaler.exe"
    args = ["-i", "{input}", "-o", "{output}", "-scale", "{scale}"]

可用占位符：{input} {output} {scale} {model} {tile} {gpu} {noise} {format}
"""

from __future__ import annotations

from .base import Backend, TaskOptions


class CustomBackend(Backend):
    name = "custom"
    label = "自定义命令"
    supported_scales = (1, 2, 3, 4, 8)

    def _sub(self) -> dict:
        return self.cfg.get("custom", {}) or {}

    def exe_path(self) -> str:
        return self._sub().get("bin", "")

    def build_cmd(self, src: str, dst: str, opts: TaskOptions) -> list[str]:
        sub = self._sub()
        values = {
            "{input}": src,
            "{output}": dst,
            "{scale}": str(opts.scale),
            "{model}": opts.model,
            "{tile}": str(opts.tile),
            "{gpu}": str(opts.gpu),
            "{noise}": str(opts.noise),
            "{format}": opts.fmt,
            "{thread}": opts.thread,
        }
        exe = sub.get("bin", "")
        args = []
        for token in sub.get("args", []) or []:
            for key, val in values.items():
                token = token.replace(key, val)
            args.append(token)
        return [exe] + args
