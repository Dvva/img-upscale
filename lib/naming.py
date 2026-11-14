"""输出路径与命名。

默认规则（都可在配置里改）：
  单文件  a.png                  -> 同目录  a_x1.png
  目录    ...\\原图-人物\\a.png   -> 源目录内部 ...\\原图-人物\\原图-人物_x1\\a_x1.png
  指定 -o D:\\out                 -> D:\\out\\a_x1.png（目录输入时保留子目录结构）

目录模式默认放在源文件夹内部（output.dir_base = "inside"）；
想改成与源目录同级（...\\原图-人物_x1\\），把 dir_base 改成 "parent"。

其中 _x1 里的 1 是实际倍数，由 output.suffix = "_x{scale}" 渲染得到；
想让名字固定带 _x1，把 suffix 改成 "_x1" 或运行时加 --suffix _x1。
"""

from __future__ import annotations

from pathlib import Path


def render(template: str, **values) -> str:
    """极简占位符替换，不认其它花括号语法。"""
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def suffix_text(out_cfg: dict, scale: int) -> str:
    return render(str(out_cfg.get("suffix") or ""), scale=scale)


def build_output(
    src: Path,
    src_root: Path,
    dir_mode: bool,
    out_cfg: dict,
    cli_out: str | None,
    scale: int,
    fmt: str,
) -> Path:
    """算出某个源文件对应的输出路径。"""
    suffix = suffix_text(out_cfg, scale)
    ext = "." + fmt.lstrip(".") if fmt else src.suffix

    name = render(
        str(out_cfg.get("name_template") or "{stem}{suffix}"),
        stem=src.stem,
        suffix=suffix,
        scale=scale,
    )

    if cli_out:
        base = Path(cli_out)
        if dir_mode:
            try:
                rel_parent = src.relative_to(src_root).parent
            except ValueError:
                rel_parent = Path(".")
            parent = base / rel_parent
        else:
            parent = base
    elif dir_mode:
        dirname = render(
            str(out_cfg.get("dir_template") or "{name}{suffix}"),
            name=src_root.name,
            suffix=suffix,
            scale=scale,
        )
        # inside = 放在源目录内部当子目录（默认）；parent = 与源目录同级
        base = src_root if str(out_cfg.get("dir_base") or "inside") == "inside" else src_root.parent
        parent = base / dirname
    else:
        parent = src.parent

    return parent / (name + ext)
