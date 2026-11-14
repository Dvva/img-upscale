"""冒烟测试：配置合并、规则匹配、输出命名、真实目录任务收集。

直接运行： python tests/smoke_test.py
不依赖 pytest，全部用断言；失败会打印具体项并返回非 0。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from lib.backends import create_backend  # noqa: E402
from lib.backends.base import TaskOptions  # noqa: E402
from lib.config import Config, DEFAULTS, load_config  # noqa: E402
from lib.matcher import dir_matches, match_rule, name_matches, rule_hit  # noqa: E402
from lib.naming import build_output  # noqa: E402
from lib.runner import collect_tasks  # noqa: E402
from lib.state import StateStore  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [失败] {name} {detail}")


# ------------------------------------------------------------------ 1. 配置


def test_config():
    print("[1] 配置合并")
    cfg = Config(Path("test.toml"), "default", None, False)
    check("默认 scale=1", cfg.get("default", "scale") == 1, cfg.get("default", "scale"))
    check("默认引擎=waifu2x", cfg.get("engine", "backend") == "waifu2x")
    check("默认后缀", cfg.get("output", "suffix") == "_x{scale}")
    check("默认启用增量记录", cfg.get("output", "use_state") is True)

    # 文件层覆盖 + 未知键保留
    cfg2 = Config(
        Path("test.toml"),
        "default",
        {
            "default": {"scale": 3, "future_key": "x"},
            "output": {"suffix": "_x1"},
            "rule": [{"dir": "D:/a", "scale": 4}],
        },
        True,
    )
    check("文件覆盖 scale", cfg2.get("default", "scale") == 3)
    check("文件覆盖 suffix", cfg2.get("output", "suffix") == "_x1")
    check("规则被读到", len(cfg2.rules) == 1)
    check("未知键保留", cfg2.get("default", "future_key") == "x")
    check("来源标注", cfg2.sources.get("default.scale") == "file")

    # 校验
    cfg3 = Config(Path("t.toml"), "default", {"output": {"on_exists": "bad"}}, True)
    problems = cfg3.check(None)
    check("非法 on_exists 被拦", any("on_exists" in m for _, m in problems))


# ------------------------------------------------------------------ 2. 匹配


def test_matcher():
    print("[2] 规则匹配")
    check("纯路径自身", dir_matches(r"D:\pics\a", r"D:\pics\a"))
    check("纯路径子目录", dir_matches(r"D:\pics\a", r"D:\pics\a\sub"))
    check("纯路径不误配", not dir_matches(r"D:\pics\a", r"D:\pics\ab"))
    check("** 跨层", dir_matches(r"D:\pics\**", r"D:\pics\a\b\c"))
    check("* 单层", dir_matches(r"D:\pics\*", r"D:\pics\a"))
    check("大小写无关", dir_matches(r"D:\PICS\A", r"d:\pics\a"))

    check("文件名通配", name_matches("*.png", "a.PNG"))
    check("文件名数组", name_matches(["*.jpg", "*.png"], "x.png"))
    check("文件名不匹配", not name_matches("*.jpg", "x.png"))
    check("match 为空视为全通过", name_matches(None, "x.png"))

    rules = [
        {"dir": r"D:\pics\a"},
        {"dir": r"D:\pics\a", "match": "*.png", "scale": 3},
    ]
    idx = match_rule(rules, r"D:\pics\a\b.png")
    check("后者覆盖前者", idx == 1, idx)
    idx2 = match_rule(rules, r"D:\pics\a\b.jpg")
    check("未命中更具体规则时退回第一条", idx2 == 0, idx2)
    check("都不命中返回 None", match_rule(rules, r"D:\other\b.png") is None)
    check("空规则不命中", not rule_hit({}, r"D:\a\b.png"))


# ------------------------------------------------------------------ 3. 命名


def test_naming():
    print("[3] 输出命名")
    out_cfg = dict(DEFAULTS["output"])

    # 单文件
    p = build_output(
        Path(r"D:\pics\a.png"), Path(r"D:\pics"), False, out_cfg, None, 2, "png"
    )
    check("单文件同目录", str(p) == r"D:\pics\a_x2.png", p)

    # 目录输入（默认放在源目录内部）
    p = build_output(
        Path(r"D:\pics\set\a.png"), Path(r"D:\pics\set"), True, out_cfg, None, 2, "png"
    )
    check("目录输入放在源目录内部", str(p) == r"D:\pics\set\set_x2\a_x2.png", p)

    # 目录输入（改成与源目录同级）
    p = build_output(
        Path(r"D:\pics\set\a.png"), Path(r"D:\pics\set"), True,
        {**out_cfg, "dir_base": "parent"}, None, 2, "png",
    )
    check("dir_base=parent 时同级", str(p) == r"D:\pics\set_x2\a_x2.png", p)

    # 指定 -o
    p = build_output(
        Path(r"D:\pics\set\a.png"), Path(r"D:\pics\set"), True, out_cfg, r"E:\out", 3, "png"
    )
    check("指定输出保留子结构", str(p) == r"E:\out\a_x3.png", p)

    # 固定 _x1 后缀
    p = build_output(
        Path(r"D:\pics\a.png"), Path(r"D:\pics"), False,
        {**out_cfg, "suffix": "_x1"}, None, 4, "png",
    )
    check("固定 _x1 后缀", str(p) == r"D:\pics\a_x1.png", p)

    # 无下划线
    p = build_output(
        Path(r"D:\pics\a.png"), Path(r"D:\pics"), False,
        {**out_cfg, "name_template": "{stem}x{scale}"}, None, 2, "png",
    )
    check("无下划线命名", str(p) == r"D:\pics\ax2.png", p)


# ------------------------------------------------------------------ 4. 后端


def test_backends():
    print("[4] 后端")
    engine_cfg = DEFAULTS["engine"]
    rg = create_backend("realesrgan", engine_cfg)
    check("realesrgan 不支持 1 倍", not rg.check_scale(1)[0])
    check("realesrgan 支持 2 倍", rg.check_scale(2)[0])
    check("realesrgan 可用", rg.available()[0])

    cu = create_backend("realcugan", engine_cfg)
    check("realcugan 不支持 1 倍（无 1x 模型）", not cu.check_scale(1)[0])
    check("realcugan 支持 2 倍", cu.check_scale(2)[0])
    check("realcugan 可用", cu.available()[0])
    cmd = cu.build_cmd("in.png", "out.png", TaskOptions(scale=2, noise=2))
    check("realcugan 命令含 -s 2", "-s" in cmd and cmd[cmd.index("-s") + 1] == "2")
    check("realcugan 命令含 -n 2", "-n" in cmd and cmd[cmd.index("-n") + 1] == "2")
    check("realcugan 模型目录存在", os.path.isdir(cu.cfg.get("realcugan_models", "")))
    check("模型路径转成相对", cu.models_arg(cu.cfg["realcugan_models"]) == "models-se",
          cu.models_arg(cu.cfg["realcugan_models"]))

    w2 = create_backend("waifu2x", engine_cfg)
    check("waifu2x 支持 1 倍", w2.check_scale(1)[0])
    check("waifu2x 可用", w2.available()[0])
    cmd2 = w2.build_cmd("in.png", "out.png", TaskOptions(scale=1, noise=-1))
    check("waifu2x -1 档映射为 1", "-n" in cmd2 and cmd2[cmd2.index("-n") + 1] == "1")

    try:
        create_backend("nope", engine_cfg)
        check("未知引擎报错", False)
    except ValueError:
        check("未知引擎报错", True)


# ------------------------------------------------------------------ 5. 真实目录

REAL_DIR = r"G:\媒体\图片\__AI 放大\1x-人物"


def test_real_dir():
    print("[5] 真实目录收集")
    if not os.path.isdir(REAL_DIR):
        print(f"  跳过：{REAL_DIR} 不存在")
        return
    cfg = load_config(None)
    backend = create_backend(str(cfg.get("engine", "backend")), cfg.values["engine"])
    tasks, errors, _ = collect_tasks([REAL_DIR], cfg, {}, backend)
    check("收集到任务", len(tasks) > 0, len(tasks))
    check("无错误", not errors, errors)
    check("默认走 1 倍", all(t.opts.scale == 1 for t in tasks))
    if tasks:
        first = tasks[0]
        check("输出目录在源目录内部", "_x1" in str(first.dst) and str(first.src.parent) in str(first.dst),
              first.dst)
        check("输出文件名带 _x1", first.dst.stem.endswith("_x1"), first.dst.name)
        check("输出不与源同路径", first.dst != first.src)
        print(f"  样例：{first.src.name}")
        print(f"     -> {first.dst}")


def test_rules_real():
    """用临时配置验证「规则覆盖默认值 + 规则换引擎」。"""
    print("[6] 规则覆盖（真实目录）")
    if not os.path.isdir(REAL_DIR):
        print(f"  跳过：{REAL_DIR} 不存在")
        return
    tmp = Path(tempfile.gettempdir()) / "img-upscale-test.toml"
    tmp.write_text(
        "[default]\nscale = 2\n\n"
        '[[rule]]\n'
        f'dir = "{REAL_DIR.replace(chr(92), "/")}"\n'
        'match = "*2025*.png"\n'
        "scale = 3\n"
        "model = \"realesrgan-x4plus\"\n",
        encoding="utf-8",
    )
    cfg = load_config(str(tmp))
    backend = create_backend(str(cfg.get("engine", "backend")), cfg.values["engine"])
    tasks, errors, _ = collect_tasks([REAL_DIR], cfg, {}, backend)
    check("带规则无错误", not errors, errors)
    rule_hit_tasks = [t for t in tasks if t.rule_index is not None]
    check("有文件命中了规则", len(rule_hit_tasks) > 0, len(rule_hit_tasks))
    if rule_hit_tasks:
        t = rule_hit_tasks[0]
        check("规则 scale 生效", t.opts.scale == 3, t.opts.scale)
        check("规则 model 生效", t.opts.model == "realesrgan-x4plus", t.opts.model)
        check("规则文件名都含 2025", "2025" in t.src.name, t.src.name)
    others = [t for t in tasks if t.rule_index is None]
    if others:
        check("其余走默认 2 倍", others[0].opts.scale == 2, others[0].opts.scale)
    tmp.unlink(missing_ok=True)


def test_state():
    print("[7] 增量记录")
    tmpdir = Path(tempfile.mkdtemp(prefix="imgstate-"))
    try:
        src = tmpdir / "a.png"
        src.write_bytes(b"x" * 100)
        out = tmpdir / "a_x1.png"
        state_path = tmpdir / "state.json"
        st = StateStore(state_path)
        opts1 = TaskOptions(scale=1, model="")

        check("初始未完成", not st.is_done(src, opts1, "realcugan")[0])
        out.write_bytes(b"y" * 200)
        st.mark(src, out, opts1, "realcugan")
        st.flush()
        check("标记后已完成", st.is_done(src, opts1, "realcugan")[0])
        check("倍数变了失效", not st.is_done(src, TaskOptions(scale=2), "realcugan")[0])
        check("引擎变了失效", not st.is_done(src, opts1, "waifu2x")[0])

        time.sleep(0.02)
        src.write_bytes(b"x" * 200)
        check("源文件改动后失效", not st.is_done(src, opts1, "realcugan")[0])

        st.mark(src, out, opts1, "realcugan")
        out.unlink()
        check("输出丢失后失效", not st.is_done(src, opts1, "realcugan")[0])

        st.mark(src, out, opts1, "realcugan")
        st.flush()
        st2 = StateStore(state_path)
        check("记录能持久化", len(st2) == 1, len(st2))
        check("重载后正确失效（输出不存在）", not st2.is_done(src, opts1, "realcugan")[0])

        st2.clear()
        check("清空后为空", len(st2) == 0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_collect_with_state():
    print("[8] 收集时按记录跳过")
    tmpdir = Path(tempfile.mkdtemp(prefix="imgstate2-"))
    try:
        srcdir = tmpdir / "src"
        srcdir.mkdir()
        for i in range(3):
            (srcdir / f"a{i}.png").write_bytes(b"x" * 50)

        cfg = load_config(None)
        backend = create_backend(str(cfg.get("engine", "backend")), cfg.values["engine"])
        state = StateStore(tmpdir / "s.json")

        # 第一轮：全部待处理
        tasks, errors, _ = collect_tasks([str(srcdir)], cfg, {}, backend, state)
        check("收集到 3 张", len(tasks) == 3, len(tasks))
        check("首轮全部待处理", all(t.status == "pending" for t in tasks), [t.status for t in tasks])

        # 伪造一张已完成：造一个输出文件并登记
        t0 = tasks[0]
        t0.dst.parent.mkdir(parents=True, exist_ok=True)
        t0.dst.write_bytes(b"fake")
        state.mark(t0.src, t0.dst, t0.opts, t0.backend.name)
        state.flush()

        # 第二轮：应有一条变成 done
        tasks2, _, _ = collect_tasks([str(srcdir)], cfg, {}, backend, state)
        dones = [t for t in tasks2 if t.status == "done"]
        check("有记录的被跳过", len(dones) == 1, len(dones))

        # --force：全部重新待处理
        tasks3, _, _ = collect_tasks([str(srcdir)], cfg, {"force": True, "on_exists": "overwrite"},
                                     backend, state)
        check("force 后全部重跑", all(t.status == "pending" for t in tasks3), len(tasks3))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main() -> int:
    test_config()
    test_matcher()
    test_naming()
    test_backends()
    test_real_dir()
    test_rules_real()
    test_state()
    test_collect_with_state()
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print(f"全部通过（{PASSED} 项断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
