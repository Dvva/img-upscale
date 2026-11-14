"""配置加载、合并、校验、打印。

优先级（高 → 低）：命令行参数 > 环境变量 > 配置文件 > 内置默认值。
配置文件默认位置：~/.config/img-upscale.toml，可用 --config 或环境变量
IMG_UPSCALE_CONFIG 覆盖。文件不存在时不报错，直接用内置默认值。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

APP_NAME = "img-upscale"
ENV_CONFIG = "IMG_UPSCALE_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / f"{APP_NAME}.toml"

# ---------------------------------------------------------------- 内置默认值

DEFAULTS: dict = {
    "engine": {
        # 本机实测：realcugan / waifu2x 正常；realesrgan 2022 版在 RX 7800 XT 上
        # tile 分块会错位，不可用。
        # 默认 waifu2x 是因为只有它支持 1 倍（默认 scale=1，只降噪重绘不放大）；
        # 要放大 2/3/4 倍建议 --engine realcugan（二次元效果最好）。
        "backend": "waifu2x",
        "realesrgan_bin": r"D:\__\realesrgan-ncnn-vulkan\realesrgan-ncnn-vulkan.exe",
        "realesrgan_models": r"D:\__\realesrgan-ncnn-vulkan\models",
        "realcugan_bin": r"D:\__\realcugan-ncnn-vulkan\realcugan-ncnn-vulkan.exe",
        "realcugan_models": r"D:\__\realcugan-ncnn-vulkan\models-se",
        "waifu2x_bin": r"D:\__\waifu2x-ncnn-vulkan\waifu2x-ncnn-vulkan.exe",
        "waifu2x_models": r"D:\__\waifu2x-ncnn-vulkan\models-cunet",
        "gpu_id": 0,
        "thread": "1:2:2",
        "tta": False,
        "custom": {"bin": "", "args": []},
    },
    "default": {
        "model": "",          # realcugan 不用模型名，靠 -n 噪声档；realesrgan 才填
        "scale": 1,           # 1 = 只做 AI 降噪重绘、尺寸不变；放大用 2/3/4
        "tile": 0,
        "noise": -1,          # -1 = 引擎默认；realcugan/waifu2x 可 0/1/2/3
        "format": "png",
    },
    "output": {
        "dir": "",
        "suffix": "_x{scale}",
        "name_template": "{stem}{suffix}",
        "dir_template": "{name}{suffix}",
        # parent = 与源目录同级（默认）；inside = 放进源目录内部当子目录
        "dir_base": "parent",
        "on_exists": "skip",
        "recursive": False,
        "jobs": 1,
        # 增量与断点续跑
        "use_state": True,
        "state_file": "~/.config/img-upscale.state.json",
        "verify_hash": False,
    },
    "hardware": {
        # 纯记录段，脚本不读取，方便换机器时对照
        "gpu": "AMD Radeon RX 7800 XT",
        "vram_gb": 16,
        "driver": "",
        "note": "realesrgan 2022 版 tile 分块在此卡上错位，禁用；realcugan/waifu2x 正常",
    },
    "rules": [],
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def expand_path(value: str) -> str:
    """展开 ~ 与 %VAR%，返回绝对路径字符串；空串原样返回。"""
    if not value:
        return value
    value = os.path.expanduser(value)
    value = os.path.expandvars(value)
    return value


def resolve_config_path(cli_path: str | None) -> tuple[Path, str]:
    """按优先级决定配置文件路径，并返回来源说明。"""
    if cli_path:
        return Path(expand_path(cli_path)), "cli"
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(expand_path(env)), "env"
    return DEFAULT_CONFIG_PATH, "default"


class Config:
    """合并后的生效配置，附带每项的来源标注。"""

    def __init__(self, path: Path, origin: str, file_data: dict | None, exists: bool):
        self.path = path
        self.origin = origin
        self.file_exists = exists
        self.file_data = file_data or {}
        self.sources: dict[str, str] = {}
        self.values: dict = self._merge()

    # -- 合并 ------------------------------------------------------------

    def _merge(self) -> dict:
        merged: dict = {}
        for section, default_val in DEFAULTS.items():
            if section == "rules":
                rules = self.file_data.get("rules") or self.file_data.get("rule") or []
                merged["rules"] = list(rules) if isinstance(rules, list) else []
                self.sources["rules"] = "file" if merged["rules"] else "default"
                continue
            if not isinstance(default_val, dict):
                merged[section] = default_val
                continue
            out = dict(default_val)
            file_section = self.file_data.get(section, {})
            if isinstance(file_section, dict):
                for key, val in file_section.items():
                    out[key] = val  # 未知键保留，保证老配置兼容
                    self.sources[f"{section}.{key}"] = "file"
            for key in default_val:
                self.sources.setdefault(f"{section}.{key}", "default")
            merged[section] = out
        return merged

    # -- 读取 ------------------------------------------------------------

    def get(self, section: str, key: str, fallback=None):
        sec = self.values.get(section, {})
        if isinstance(sec, dict) and key in sec:
            return sec[key]
        return fallback

    def set(self, section: str, key: str, value, source: str = "cli"):
        sec = self.values.setdefault(section, {})
        sec[key] = value
        self.sources[f"{section}.{key}"] = source

    @property
    def rules(self) -> list[dict]:
        return [r for r in self.values.get("rules", []) if isinstance(r, dict)]

    # -- 校验 ------------------------------------------------------------

    def check(self, backend=None) -> list[tuple[str, str]]:
        """返回 [(级别, 消息)]，级别为 error / warn。"""
        problems: list[tuple[str, str]] = []

        on_exists = self.get("output", "on_exists", "skip")
        if on_exists not in ("skip", "overwrite", "rename"):
            problems.append(
                ("error", f"output.on_exists = {on_exists!r}，只支持 skip / overwrite / rename")
            )

        jobs = self.get("output", "jobs", 1)
        if not isinstance(jobs, int) or jobs < 1:
            problems.append(("error", f"output.jobs = {jobs!r}，必须是 >= 1 的整数"))

        dir_base = self.get("output", "dir_base", "parent")
        if dir_base not in ("parent", "inside"):
            problems.append(
                ("error", f"output.dir_base = {dir_base!r}，只支持 parent / inside")
            )

        if backend is not None:
            ok, msg = backend.available()
            if not ok:
                problems.append(("error", msg))
            scale = self.get("default", "scale", 2)
            ok_s, msg_s = backend.check_scale(int(scale))
            if not ok_s:
                problems.append(("error", msg_s))

        for idx, rule in enumerate(self.rules, 1):
            if not rule.get("dir") and not rule.get("match"):
                problems.append(
                    ("error", f"第 {idx} 条 [[rule]] 既没有 dir 也没有 match，永远不会命中")
                )
            raw_dir = rule.get("dir")
            if raw_dir:
                expanded = expand_path(str(raw_dir))
                # 只有不含通配符的目录才能做存在性检查
                if not any(ch in expanded for ch in "*?["):
                    if not os.path.isdir(expanded):
                        problems.append(("warn", f"第 {idx} 条规则的 dir 不存在：{expanded}"))
        return problems

    # -- 打印 ------------------------------------------------------------

    def dump(self) -> str:
        lines = [f"配置文件：{self.path}（{self.origin}，"
                 f"{'已找到' if self.file_exists else '不存在，使用内置默认值'}）"]
        for section in ("engine", "default", "output", "hardware"):
            lines.append("")
            lines.append(f"[{section}]")
            sec = self.values.get(section, {})
            for key in sorted(sec):
                if key == "custom" and isinstance(sec[key], dict):
                    for sub_key in sorted(sec[key]):
                        src = self.sources.get(f"{section}.{key}.{sub_key}", "default")
                        lines.append(f"  {key}.{sub_key:<14} = {sec[key][sub_key]!r:<30} ({src})")
                    continue
                src = self.sources.get(f"{section}.{key}", "default")
                lines.append(f"  {key:<20} = {sec[key]!r:<34} ({src})")
        lines.append("")
        lines.append(f"[[rule]] 共 {len(self.rules)} 条（来源：{self.sources.get('rules')}）")
        for idx, rule in enumerate(self.rules, 1):
            lines.append(f"  #{idx} {rule}")
        return "\n".join(lines)


def load_config(cli_path: str | None = None) -> Config:
    """读取并合并配置。文件不存在不报错。"""
    path, origin = resolve_config_path(cli_path)
    exists = path.is_file()
    data = None
    if exists:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    return Config(path, origin, data, exists)
