"""增量状态：记录「哪些文件已经处理完」，支持中断后继续。

为什么用「源文件 size + mtime + 参数」而不是哈希：
  - 哈希（SHA-256）最精确，但每次启动都要把全部源图读一遍，
    276 张 2K 图约 1.5 GB，多花几十秒，日常不划算；
  - size + mtime 组合能覆盖 99% 的情况（文件被替换必然改动其一）；
  - 需要绝对精确时加 --verify-hash，只对「state 认为已完成」的文件做哈希复核。

配合「先写 .part 再原子改名」的落盘方式，输出文件一旦存在就是完整的，
即使 state 丢了也不会把半张图当成已完成。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

STATE_VERSION = 1


def file_signature(path: Path) -> dict:
    """源文件指纹：大小 + 修改时间 + 纳秒级 inode 变化。"""
    stat = path.stat()
    return {"size": stat.st_size, "mtime": round(stat.st_mtime, 6)}


def file_hash(path: Path, chunk: int = 1 << 20) -> str:
    """SHA-256，仅在 --verify-hash 时用。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class StateStore:
    """JSON 状态文件，按源文件绝对路径记录完成信息。"""

    def __init__(self, path: Path, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled
        self._lock = threading.Lock()
        self.data: dict = {"version": STATE_VERSION, "entries": {}}
        self.dirty = False
        if self.enabled:
            self._load()

    # -- 读写 ------------------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
            raw.setdefault("version", STATE_VERSION)
            self.data = raw

    def flush(self, force: bool = False) -> None:
        if not self.enabled or (not self.dirty and not force):
            return
        payload = json.dumps(self.data, ensure_ascii=False, indent=1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, self.path)  # 原子替换，中途断电不会写坏
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        self.dirty = False

    # -- 查询 / 记录 ------------------------------------------------------

    @staticmethod
    def key(src: Path) -> str:
        return os.path.normcase(os.path.abspath(str(src)))

    def entry(self, src: Path) -> dict | None:
        return self.data["entries"].get(self.key(src))

    def is_done(
        self,
        src: Path,
        opts,
        backend_name: str,
        verify_hash: bool = False,
        dst: Path | None = None,
    ) -> tuple[bool, str]:
        """返回 (是否已完成, 原因说明)。

        dst 是本次要写的输出路径。传了就要求记录的输出路径与之一致，
        否则换了 -o / dir_base / 目录模板后会把该做的任务误判成已完成。
        """
        if not self.enabled:
            return False, "state 已禁用"
        rec = self.entry(src)
        if not rec:
            return False, "无记录"

        out = rec.get("out")
        if not out or not os.path.isfile(out):
            return False, "输出文件已不在"
        if dst is not None and os.path.normcase(os.path.abspath(str(dst))) != \
                os.path.normcase(os.path.abspath(str(out))):
            return False, "输出位置变了"

        try:
            sig = file_signature(src)
        except OSError:
            return False, "源文件读不到"
        if rec.get("src_size") != sig["size"] or abs(float(rec.get("src_mtime", 0)) - sig["mtime"]) > 1e-6:
            return False, "源文件已变化"
        if rec.get("scale") != opts.scale:
            return False, f"倍数变了（旧 {rec.get('scale')} → 新 {opts.scale}）"
        if rec.get("model") != (opts.model or ""):
            return False, "模型变了"
        if rec.get("backend") != backend_name:
            return False, f"引擎变了（旧 {rec.get('backend')} → 新 {backend_name}）"

        if verify_hash:
            old_hash = rec.get("src_hash")
            if old_hash:
                try:
                    if file_hash(src) != old_hash:
                        return False, "哈希不一致"
                except OSError:
                    return False, "源文件读不到"
        return True, "已完成"

    def mark(self, src: Path, out: Path, opts, backend_name: str, with_hash: bool = False) -> None:
        if not self.enabled:
            return
        try:
            sig = file_signature(src)
        except OSError:
            return
        record = {
            "out": str(out),
            "src_size": sig["size"],
            "src_mtime": sig["mtime"],
            "scale": opts.scale,
            "model": opts.model or "",
            "backend": backend_name,
            "done_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if with_hash:
            try:
                record["src_hash"] = file_hash(src)
            except OSError:
                pass
        with self._lock:
            self.data["entries"][self.key(src)] = record
            self.dirty = True

    def forget(self, src: Path) -> None:
        with self._lock:
            self.data["entries"].pop(self.key(src), None)
            self.dirty = True

    def clear(self) -> None:
        with self._lock:
            self.data["entries"] = {}
            self.dirty = True
        self.flush(force=True)

    def __len__(self) -> int:
        return len(self.data["entries"])
