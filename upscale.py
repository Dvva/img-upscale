#!/usr/bin/env python3
"""img-upscale —— 命令行 AI 图片放大。

用法示例：
    python upscale.py "G:\\媒体\\图片\\__AI 放大\\1x-壁纸"          # 默认 1 倍重绘
    python upscale.py "G:\\媒体\\图片\\__AI 放大\\1x-壁纸" -s 2     # 放大 2 倍
    python upscale.py "G:\\图\\a.png" -o "D:\\out" --suffix _x1
    python upscale.py "G:\\媒体\\图片\\__AI 放大" -r --dry-run
    python upscale.py --print-config / --check-config / --list-backends
    python upscale.py "G:\\某目录" --force                          # 忽略记录全部重跑
    python upscale.py --clear-state                                 # 清空已完成记录
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tempfile
from pathlib import Path

# 让脚本能从任何工作目录 import lib\
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.backends import BACKENDS, create_backend  # noqa: E402
from lib.config import expand_path, load_config  # noqa: E402
from lib.runner import collect_tasks, run_tasks  # noqa: E402
from lib.state import StateStore  # noqa: E402

LOCK_PATH = os.path.join(tempfile.gettempdir(), "img-upscale.lock")


def _setup_console_encoding() -> None:
    """被管道 / 重定向捕获时统一输出 UTF-8。

    Windows 在真实终端里 Python 走宽字符 API，编码无关；只有输出被重定向时
    才按 locale（常见 cp936）编码，导致中文乱码，所以这里只在非 TTY 时改。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty() and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------- 参数解析


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="upscale.py",
        description="命令行 AI 图片放大：配置驱动，按目录/文件名匹配独立参数，支持增量与断点续跑。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "配置优先级：命令行 > 环境变量 IMG_UPSCALE_CONFIG > ~/.config/img-upscale.toml > 内置默认值\n"
            "默认 1 倍（只降噪重绘、尺寸不变）；要放大请加 -s 2 / 3 / 4。"
        ),
    )
    p.add_argument("inputs", nargs="*", help="图片文件或目录，可给多个")
    p.add_argument("-o", "--output", metavar="DIR", help="统一输出到该目录")
    p.add_argument("-s", "--scale", type=int, help="倍数：1=只重绘不放大，2/3/4=放大")
    p.add_argument("-m", "--model", help="模型名（realesrgan 用，如 realesrgan-x4plus-anime）")
    p.add_argument("-t", "--tile", type=int, help="分块大小，显存不足时设 256；0=自动")
    p.add_argument("--noise", type=int, help="降噪档位 -1/0/1/2/3（realcugan、waifu2x 用）")
    p.add_argument("--tta", action="store_true", help="开启 TTA 增强（慢 8 倍左右）")
    p.add_argument("--engine", choices=sorted(BACKENDS), help="放大引擎")
    p.add_argument("--suffix", metavar="TEXT", help='输出后缀覆盖，如 _x1（默认随倍数 _x{scale}）')
    p.add_argument("-r", "--recursive", action="store_true", help="递归处理子目录")
    p.add_argument("-j", "--jobs", type=int, help="并发任务数（外部引擎进程，默认 1）")
    p.add_argument("-c", "--config", metavar="PATH", help="指定配置文件")
    p.add_argument(
        "--on-exists",
        choices=["skip", "overwrite", "rename"],
        help="输出已存在时的处理方式",
    )
    p.add_argument("-n", "--dry-run", action="store_true", help="只打印计划，不实际处理")
    p.add_argument("--no-progress", action="store_true", help="关闭进度条")

    g = p.add_argument_group("增量与续跑")
    g.add_argument("--force", action="store_true", help="忽略完成记录与已有输出，全部重跑")
    g.add_argument("--no-state", action="store_true", help="不读也不写完成记录")
    g.add_argument("--state-file", metavar="PATH", help="完成记录文件位置")
    g.add_argument("--clear-state", action="store_true", help="清空完成记录后退出（可配合输入一起用）")
    g.add_argument("--verify-hash", action="store_true", help="对已完成项再做 SHA-256 校验（慢）")
    g.add_argument("--show-state", action="store_true", help="查看完成记录统计")

    p.add_argument("--print-config", action="store_true", help="打印生效配置及来源后退出")
    p.add_argument("--check-config", action="store_true", help="只校验配置，退出码 0/1")
    p.add_argument("--list-backends", action="store_true", help="列出所有引擎及可用性")
    p.add_argument("--allow-concurrent", action="store_true", help="允许同时运行多份实例")
    p.add_argument("--timeout", type=int, default=0, help="单张超时秒数，0=不限")
    return p


# ---------------------------------------------------------------- 单实例锁


def acquire_lock() -> bool:
    """返回 True=拿到锁，False=已有实例在跑。"""
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as fh:
                pid = int((fh.read() or "0").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _pid_alive(pid):
            return False
        try:
            os.remove(LOCK_PATH)
        except OSError:
            return False
    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        return True  # 写不了锁文件就不锁，别因此挡路
    return True


def release_lock() -> None:
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


# ---------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    _setup_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    # 命令行覆盖配置
    if args.engine:
        cfg.set("engine", "backend", args.engine)
    if args.scale is not None:
        cfg.set("default", "scale", args.scale)
    if args.model:
        cfg.set("default", "model", args.model)
    if args.tile is not None:
        cfg.set("default", "tile", args.tile)
    if args.noise is not None:
        cfg.set("default", "noise", args.noise)
    if args.tta:
        cfg.set("engine", "tta", True)
    if args.suffix:
        cfg.set("output", "suffix", args.suffix)
    if args.recursive:
        cfg.set("output", "recursive", True)
    if args.jobs is not None:
        cfg.set("output", "jobs", args.jobs)
    if args.on_exists:
        cfg.set("output", "on_exists", args.on_exists)
    if args.state_file:
        cfg.set("output", "state_file", args.state_file)
    if args.no_state:
        cfg.set("output", "use_state", False)
    if args.verify_hash:
        cfg.set("output", "verify_hash", True)

    # 后端
    backend_name = str(cfg.get("engine", "backend", "realcugan"))
    try:
        backend = create_backend(backend_name, cfg.values.get("engine", {}))
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    if args.list_backends:
        print("可用引擎：")
        for name, cls in BACKENDS.items():
            inst = cls(cfg.values.get("engine", {}))
            mark = "*" if name == backend_name else " "
            print(f" {mark} {inst.describe()}")
        print("\n* = 当前配置选中的引擎")
        return 0

    if args.print_config:
        print(cfg.dump())
        ok, msg = backend.available()
        print(f"\n引擎可用性：{'可用' if ok else '不可用'}" + (f"（{msg}）" if msg else ""))
        return 0

    if args.check_config:
        problems = cfg.check(backend)
        if not problems:
            print(f"配置校验通过：{cfg.path}")
            return 0
        for level, msg in problems:
            print(f"[{'错误' if level == 'error' else '警告'}] {msg}")
        return 1 if any(lvl == "error" for lvl, _ in problems) else 0

    # 完成记录
    state_path = Path(expand_path(str(cfg.get("output", "state_file") or ""))) if \
        cfg.get("output", "state_file") else Path.home() / ".config" / "img-upscale.state.json"
    use_state = bool(cfg.get("output", "use_state", True))
    state = StateStore(state_path, enabled=use_state)

    if args.clear_state:
        state.clear()
        print(f"已清空完成记录：{state_path}")
        if not args.inputs:
            return 0

    if args.show_state:
        print(f"完成记录：{state_path}（{'启用' if use_state else '已禁用'}）")
        print(f"已记录 {len(state)} 条")
        by_backend: dict[str, int] = {}
        for rec in state.data["entries"].values():
            key = f"{rec.get('backend', '?')} x{rec.get('scale', '?')}"
            by_backend[key] = by_backend.get(key, 0) + 1
        for key in sorted(by_backend):
            print(f"  {key:<22} {by_backend[key]:>4} 张")
        if not args.inputs:
            return 0

    if not args.inputs:
        parser.error("没有给输入路径。给个图片或目录，或加 --print-config 查看配置")

    # 校验输入
    missing = [p for p in args.inputs if not os.path.exists(p)]
    if missing:
        for path in missing:
            print(f"错误：路径不存在 {path}", file=sys.stderr)
        return 2

    # 收集
    cli_override = {
        "scale": args.scale,
        "model": args.model,
        "tile": args.tile,
        "noise": args.noise,
        "output": args.output,
        "recursive": args.recursive or None,
        "on_exists": "overwrite" if args.force else args.on_exists,
        "force": args.force,
        "verify_hash": args.verify_hash,
    }
    tasks, errors, notes = collect_tasks(args.inputs, cfg, cli_override, backend, state)

    for note in notes:
        print(f"提示：{note}", file=sys.stderr)
    if errors:
        for err in errors:
            print(f"错误：{err}", file=sys.stderr)
        if not tasks:
            return 2

    if not tasks:
        print("没有需要处理的图片。")
        return 0

    if args.dry_run:
        already = [t for t in tasks if t.status == "done"]
        skipped = [t for t in tasks if t.status == "skip"]
        pending = [t for t in tasks if t.status == "pending"]
        print(f"计划处理 {len(tasks)} 张（未写入任何文件）：\n")
        for idx, task in enumerate(tasks, 1):
            if task.status == "done":
                state_note = f"（已完成：{task.reason}，跳过）"
            elif task.status == "skip":
                state_note = "（已存在，将跳过）"
            else:
                state_note = ""
            print(f"[{idx}/{len(tasks)}] {task.rule_label}  x{task.opts.scale}  "
                  f"引擎={task.backend.name if task.backend else '-'}{state_note}")
            print(f"      源：{task.src}")
            print(f"      出：{task.dst}")
        print(f"\n合计 {len(tasks)} 张：已完成 {len(already)}、已存在 {len(skipped)}、待处理 {len(pending)}。")
        return 0

    if not args.allow_concurrent and not acquire_lock():
        print("另一个 img-upscale 实例正在运行；确认无冲突可加 --allow-concurrent。", file=sys.stderr)
        return 3

    try:
        jobs = max(1, int(cfg.get("output", "jobs", 1) or 1))
        stats = run_tasks(
            tasks=tasks,
            backend=backend,
            jobs=jobs,
            timeout=args.timeout,
            show_progress=not args.no_progress,
            state=state,
            with_hash=bool(cfg.get("output", "verify_hash", False)),
        )
    finally:
        release_lock()

    # 汇总
    parts = [f"{stats.ok} 成功"]
    if stats.skipped:
        parts.append(f"{stats.skipped} 已存在跳过")
    if stats.already:
        parts.append(f"{stats.already} 此前已完成")
    parts.append(f"{stats.failed} 失败")
    print(f"\n{'中断' if stats.interrupted else '完成'}：{' / '.join(parts)}，"
          f"用时 {stats.seconds:.1f}s（引擎 {backend.label}，并发 {jobs}）")
    if stats.outputs:
        parents = {os.path.dirname(p) for p in stats.outputs}
        if len(parents) == 1:
            print(f"输出目录：{parents.pop()}")
        else:
            print(f"输出位置：{len(parents)} 个目录")
    if stats.interrupted:
        print(f"再次运行同一命令即可继续；当前记录文件：{state_path}")
    if stats.failures:
        print("\n失败明细：")
        for src, err in stats.failures[:20]:
            print(f"  {os.path.basename(src)}：{err}")
        if len(stats.failures) > 20:
            print(f"  …… 其余 {len(stats.failures) - 20} 条略")
    return 0 if stats.failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
