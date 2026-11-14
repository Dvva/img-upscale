"""收集任务、批量执行、进度显示、结果汇总。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .backends import create_backend
from .backends.base import Backend, TaskOptions
from .config import IMAGE_EXTS, Config, expand_path
from .matcher import match_rule
from .naming import build_output, render, suffix_text
from .state import StateStore

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 规则里允许覆盖的键
OPTION_KEYS = ("scale", "model", "tile", "noise", "tta", "format", "fmt", "thread", "gpu")

# 每完成多少个任务落一次 state（其余靠输出文件存在性兜底）
STATE_FLUSH_EVERY = 5


@dataclass
class Task:
    src: Path
    dst: Path
    opts: TaskOptions
    rule_index: int | None
    backend: Backend | None = None
    status: str = "pending"   # pending / done / skip / ok / fail
    reason: str = ""
    error: str = ""

    @property
    def rule_label(self) -> str:
        return "默认" if self.rule_index is None else f"规则#{self.rule_index + 1}"


@dataclass
class Stats:
    total: int = 0
    ok: int = 0
    skipped: int = 0
    already: int = 0
    failed: int = 0
    seconds: float = 0.0
    interrupted: bool = False
    outputs: list[str] = None
    failures: list[tuple[str, str]] = None

    def __post_init__(self):
        self.outputs = []
        self.failures = []


# ------------------------------------------------------------------ 收集任务


def iter_inputs(inputs: list[str], recursive: bool, exclude_dirs: set[str]):
    """展开输入，产出 (文件, 是否目录模式, 根目录)。"""
    for raw in inputs:
        item = Path(expand_path(raw))
        if item.is_file():
            if item.suffix.lower() in IMAGE_EXTS:
                yield item, False, item.parent
            continue
        if not item.is_dir():
            yield item, None, item  # 不存在的输入，交给调用方报错
            continue
        stack = [item]
        while stack:
            folder = stack.pop()
            if os.path.normcase(str(folder)) in exclude_dirs:
                continue
            try:
                entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
            except OSError as exc:
                print(f"跳过无法读取的目录 {folder}：{exc}", file=sys.stderr)
                continue
            for entry in entries:
                if entry.is_dir():
                    if recursive:
                        stack.append(entry)
                elif entry.suffix.lower() in IMAGE_EXTS:
                    yield entry, True, item


def merge_options(cfg: Config, rule: dict | None, cli: dict) -> TaskOptions:
    """默认值 ← 命中规则 ← 命令行，后者覆盖前者。"""
    opts = TaskOptions(
        scale=int(cfg.get("default", "scale", 1)),
        model=str(cfg.get("default", "model", "") or ""),
        tile=int(cfg.get("default", "tile", 0) or 0),
        noise=int(cfg.get("default", "noise", -1)),
        fmt=str(cfg.get("default", "fmt") or cfg.get("default", "format", "png")),
        thread=str(cfg.get("engine", "thread", "1:2:2")),
        gpu=int(cfg.get("engine", "gpu_id", 0)),
        tta=bool(cfg.get("engine", "tta", False)),
    )
    layers = []
    if rule:
        layers.append({k: v for k, v in rule.items() if k in OPTION_KEYS and v is not None})
    layers.append({k: v for k, v in cli.items() if k in OPTION_KEYS and v is not None})
    for layer in layers:
        if "scale" in layer:
            opts.scale = int(layer["scale"])
        if "model" in layer:
            opts.model = str(layer["model"])
        if "tile" in layer:
            opts.tile = int(layer["tile"])
        if "noise" in layer:
            opts.noise = int(layer["noise"])
        if "tta" in layer:
            opts.tta = bool(layer["tta"])
        if "thread" in layer:
            opts.thread = str(layer["thread"])
        if "gpu" in layer:
            opts.gpu = int(layer["gpu"])
        if layer.get("format") or layer.get("fmt"):
            opts.fmt = str(layer.get("format") or layer.get("fmt"))
    return opts


def collect_tasks(
    inputs: list[str],
    cfg: Config,
    cli: dict,
    backend: Backend,
    state: StateStore | None = None,
) -> tuple[list[Task], list[str], list[str]]:
    """返回 (任务列表, 错误列表, 提示列表)。不做任何写操作。"""
    tasks: list[Task] = []
    errors: list[str] = []
    notes: list[str] = []

    cli_out = cli.get("output")
    out_cfg = dict(cfg.values.get("output", {}))
    if out_cfg.get("dir"):
        cli_out = cli_out or str(out_cfg["dir"])

    recursive = bool(cli.get("recursive", out_cfg.get("recursive", False)))
    on_exists = str(cli.get("on_exists") or out_cfg.get("on_exists", "skip"))
    force = bool(cli.get("force"))
    verify_hash = bool(cli.get("verify_hash"))
    exclude = set()
    for candidate in (out_cfg.get("dir"), cli_out):
        if candidate:
            exclude.add(os.path.normcase(os.path.abspath(expand_path(str(candidate)))))

    # 输出放在源目录内部时，递归扫描必须跳过这些输出目录，否则会把自己的产物再放大一遍
    if str(out_cfg.get("dir_base") or "inside") == "inside":
        scales = {int(cfg.get("default", "scale", 1) or 1)}
        for rule in cfg.rules:
            if rule.get("scale") is not None:
                try:
                    scales.add(int(rule["scale"]))
                except (TypeError, ValueError):
                    pass
        for raw in inputs:
            root = Path(expand_path(raw))
            if not root.is_dir():
                continue
            for scale in scales:
                dirname = render(
                    str(out_cfg.get("dir_template") or "{name}{suffix}"),
                    name=root.name,
                    suffix=suffix_text(out_cfg, scale),
                    scale=scale,
                )
                exclude.add(os.path.normcase(os.path.abspath(str(root / dirname))))

    seen: set[str] = set()
    for src, dir_mode, root in iter_inputs(inputs, recursive, exclude):
        if dir_mode is None:
            errors.append(f"路径不存在：{src}")
            continue
        key = os.path.normcase(os.path.abspath(str(src)))
        if key in seen:
            continue
        seen.add(key)

        if src.suffix.lower() not in IMAGE_EXTS:
            notes.append(f"跳过非图片：{src}")
            continue

        rule_index = match_rule(cfg.rules, str(src))
        rule = cfg.rules[rule_index] if rule_index is not None else None
        opts = merge_options(cfg, rule, cli)

        # 规则里可以换引擎（engine 字段），没写则用全局选中的
        task_backend = backend
        rule_engine = rule.get("engine") if rule else None
        if rule_engine and str(rule_engine) != backend.name:
            try:
                task_backend = create_backend(str(rule_engine), cfg.values.get("engine", {}))
            except ValueError as exc:
                errors.append(f"{src.name}：{exc}")
                continue

        ok_scale, msg_scale = task_backend.check_scale(opts.scale)
        if not ok_scale:
            errors.append(f"{src.name}：{msg_scale}")
            continue

        dst = build_output(
            src=src,
            src_root=root,
            dir_mode=bool(dir_mode),
            out_cfg=out_cfg,
            cli_out=cli_out,
            scale=opts.scale,
            fmt=opts.fmt,
        )
        if os.path.normcase(str(dst)) == key:
            errors.append(f"{src.name}：输出路径与源文件相同，检查 suffix/模板配置")
            continue

        task = Task(src=src, dst=dst, opts=opts, rule_index=rule_index, backend=task_backend)

        # 1) 增量：state 说已完成且源文件没变 → 直接跳过
        if state is not None and state.enabled and not force:
            done, why = state.is_done(src, opts, task_backend.name, verify_hash)
            if done:
                task.status, task.reason = "done", why
                tasks.append(task)
                continue

        # 2) 输出已存在
        if dst.exists() and not force:
            if on_exists == "skip":
                task.status, task.reason = "skip", "输出已存在"
            elif on_exists == "rename":
                task.dst = _unique_path(dst)
            # overwrite：原样覆盖
        tasks.append(task)
    return tasks, errors, notes


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    idx = 2
    while True:
        candidate = path.with_name(f"{stem}-{idx}{suffix}")
        if not candidate.exists():
            return candidate
        idx += 1


# ------------------------------------------------------------------ 执行任务


def run_one(task: Task, timeout: int) -> Task:
    """先写到 .part 临时文件，成功后再原子改名，保证输出文件出现即完整。"""
    backend = task.backend
    if backend is None:
        task.status, task.error = "fail", "内部错误：任务没有绑定引擎"
        return task

    tmp = task.dst.with_name(f"{task.dst.stem}.part{task.dst.suffix}")
    try:
        task.dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        task.status, task.error = "fail", f"创建输出目录失败：{exc}"
        return task

    cmd = backend.build_cmd(str(task.src), str(tmp), task.opts)
    try:
        proc = subprocess.run(
            cmd,
            cwd=backend.workdir(),  # 引擎按自身目录解析相对模型路径
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout or None,
        )
    except subprocess.TimeoutExpired:
        _cleanup(tmp)
        task.status, task.error = "fail", f"超时（>{timeout}s）"
        return task
    except OSError as exc:
        _cleanup(tmp)
        task.status, task.error = "fail", f"启动引擎失败：{exc}"
        return task
    except KeyboardInterrupt:
        _cleanup(tmp)
        raise

    if proc.returncode != 0:
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()
        _cleanup(tmp)
        task.status, task.error = "fail", f"引擎返回 {proc.returncode}：{detail[-1] if detail else ''}"
        return task
    if not tmp.exists():
        task.status, task.error = "fail", "命令返回 0 但没有生成输出文件"
        return task

    try:
        os.replace(tmp, task.dst)
    except OSError as exc:
        _cleanup(tmp)
        task.status, task.error = "fail", f"改名失败：{exc}"
        return task
    task.status = "ok"
    return task


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


class Progress:
    """自己实现的进度条；非 TTY 自动降级成每 10% 一行。"""

    def __init__(self, total: int, enabled: bool = True):
        self.total = max(total, 1)
        self.done = 0
        self.enabled = enabled
        self.tty = sys.stderr.isatty()
        self.start = time.time()
        self.last_bucket = -1

    def tick(self, name: str):
        self.done += 1
        if not self.enabled:
            return
        pct = int(self.done * 100 / self.total)
        if self.tty:
            width = 24
            filled = int(width * self.done / self.total)
            bar = "#" * filled + "-" * (width - filled)
            elapsed = time.time() - self.start
            eta = elapsed / self.done * (self.total - self.done)
            sys.stderr.write(
                f"\r[{bar}] {self.done}/{self.total} {pct:3d}%  "
                f"{elapsed:6.1f}s  剩余 {eta:5.1f}s  {name[:36]:<36}"
            )
            sys.stderr.flush()
        else:
            bucket = pct // 10
            if bucket != self.last_bucket:
                self.last_bucket = bucket
                print(f"[{self.done}/{self.total}] {pct}% {name}", file=sys.stderr, flush=True)

    def finish(self):
        if self.enabled and self.tty:
            sys.stderr.write("\n")
            sys.stderr.flush()


def run_tasks(
    tasks: list[Task],
    backend: Backend,
    jobs: int,
    timeout: int,
    show_progress: bool,
    state: StateStore | None = None,
    with_hash: bool = False,
) -> Stats:
    stats = Stats(total=len(tasks))
    stats.skipped = sum(1 for t in tasks if t.status == "skip")
    stats.already = sum(1 for t in tasks if t.status == "done")
    pending = [t for t in tasks if t.status == "pending"]
    progress = Progress(len(pending), enabled=show_progress)
    start = time.time()
    since_flush = 0

    def record(task: Task) -> None:
        nonlocal since_flush
        if task.status == "ok" and state is not None and task.backend is not None:
            state.mark(task.src, task.dst, task.opts, task.backend.name, with_hash=with_hash)
            since_flush += 1
            if since_flush >= STATE_FLUSH_EVERY:
                state.flush()
                since_flush = 0

    try:
        if jobs <= 1 or len(pending) <= 1:
            for task in pending:
                run_one(task, timeout)
                progress.tick(task.src.name)
                record(task)
        else:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futures = {pool.submit(run_one, t, timeout): t for t in pending}
                for future in futures:
                    task = future.result()
                    progress.tick(task.src.name)
                    record(task)
    except KeyboardInterrupt:
        stats.interrupted = True
        print("\n已中断。已完成的部分已记录，重新运行会从断点继续。", file=sys.stderr)
    finally:
        progress.finish()
        if state is not None:
            state.flush(force=True)

    for task in pending:
        if task.status == "ok":
            stats.ok += 1
            stats.outputs.append(str(task.dst))
        elif task.status != "pending":  # 中断后仍未处理的不算失败
            stats.failed += 1
            stats.failures.append((str(task.src), task.error))
    stats.seconds = time.time() - start
    return stats
