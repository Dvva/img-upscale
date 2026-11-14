"""规则匹配：把「目录 + 文件名」glob 规则套到具体文件上。

规则语义：
  - rule.dir   匹配【文件所在目录】，纯路径按前缀（含子目录）匹配，
               含通配符则把 * 视为单层、** 视为跨层。
  - rule.match 匹配【文件名】，支持字符串或字符串数组。
  - 两者都写时须同时命中。
  - 多条规则命中时，**声明在后面的覆盖前面的**（后者优先）。
"""

from __future__ import annotations

import fnmatch
import os
import re

from .config import expand_path


def glob_to_regex(pattern: str) -> str:
    """把简化 glob 转成正则：** 跨目录，* 不跨目录，? 单字符。"""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        two = pattern[i : i + 2]
        if two == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append(r"[^/\\]*")
            i += 1
        elif pattern[i] == "?":
            out.append(r"[^/\\]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def dir_matches(pattern: str, folder: str) -> bool:
    """folder 是否等于 pattern 指定的目录，或位于其下。"""
    pattern = expand_path(str(pattern))
    if not pattern:
        return False
    has_wildcard = any(ch in pattern for ch in "*?[")
    if not has_wildcard:
        p = _norm(pattern).rstrip("\\/")
        f = _norm(folder).rstrip("\\/")
        return f == p or f.startswith(p + os.sep)
    rx = re.compile(
        "^" + glob_to_regex(_norm(pattern).rstrip("\\/")) + r"(?:[\\/].*)?$", re.IGNORECASE
    )
    return bool(rx.match(_norm(folder).rstrip("\\/")))


def name_matches(patterns, name: str) -> bool:
    """文件名 glob 匹配，支持单个字符串或列表。"""
    if not patterns:
        return True
    if isinstance(patterns, str):
        patterns = [patterns]
    lowered = name.lower()
    for pat in patterns:
        if not isinstance(pat, str):
            continue
        if fnmatch.fnmatch(lowered, pat.lower()):
            return True
    return False


def rule_hit(rule: dict, file_path: str) -> bool:
    """单条规则是否命中某个文件。"""
    folder = os.path.dirname(os.path.abspath(file_path))
    name = os.path.basename(file_path)
    has_dir = bool(rule.get("dir"))
    has_match = bool(rule.get("match"))
    if not has_dir and not has_match:
        return False
    if has_dir and not dir_matches(rule["dir"], folder):
        return False
    if has_match and not name_matches(rule["match"], name):
        return False
    return True


def match_rule(rules: list[dict], file_path: str) -> int | None:
    """返回命中的最后一条规则下标（后者覆盖前者），没命中返回 None。"""
    hit = None
    for idx, rule in enumerate(rules):
        if rule_hit(rule, file_path):
            hit = idx
    return hit
