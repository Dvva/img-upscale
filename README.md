# img-upscale

命令行 AI 图片放大，配置驱动，**按目录 / 文件名给不同文件套不同参数**，
支持**增量处理**与**中断后继续**。面向二次元插画，底层 ncnn + Vulkan 推理
（AMD / Intel / NVIDIA 都能跑，不需要 CUDA、不需要 PyTorch、不需要 pip install）。

```
python upscale.py "G:\媒体\图片\__AI 放大\原图-人物"              # 1 倍：只降噪重绘
python upscale.py "G:\媒体\图片\__AI 放大\原图-人物" -s 2 --engine realcugan   # 放大 2 倍
python upscale.py "G:\图\a.png" -o "D:\out" --suffix _x1
python upscale.py "G:\媒体\图片\__AI 放大" -r --dry-run
```

## 引擎怎么选（本机实测，2026-11-14，RX 7800 XT 16G）

| 引擎 | 1 倍 | 2/3/4 倍 | 本机状态 | 适合 |
|---|---|---|---|---|
| **waifu2x** | ✔ | ✔(2) | **正常** | 1 倍降噪重绘（默认可选） |
| **Real-CUGAN** | ✘ 无 1x 模型 | ✔ | **正常，最快** | 放大首选，二次元最强 |
| Real-ESRGAN | ✘ | ✔ | **✘ tile 分块错位** | 不推荐（见下） |

> **Real-ESRGAN 的坑**：2022 版（`realesrgan-ncnn-vulkan-20220424-windows`）在本机
> RX 7800 XT 上，只要图像被拆成多个 tile 处理，输出就会整块错位（内容拼错）；
> `-t` 调到不分块又只写出 0.1MB 的残缺图。小图、大图、不同 tile/线程参数都复现，
> 换 realcugan / waifu2x 同图同参数完全正常 —— 是那版引擎自身的问题，不是参数配置。
> 所以默认引擎不用它。

默认配置是 **waifu2x + 1 倍**（只降噪重绘、尺寸不变），要放大就加 `-s 2 --engine realcugan`。

## 特性

- **多引擎可热切**：waifu2x / Real-CUGAN / Real-ESRGAN / 任意自定义命令，接口在 `lib/backends/`
- **规则驱动**：`[[rule]]` 按目录 + 文件名 glob 匹配，命中的文件用独立参数，甚至换引擎
- **增量**：处理成功即记录，下次同一批图自动跳过
- **断点续跑**：Ctrl+C / 断电中断，重跑同一命令接着做，不重复
- **写盘安全**：先写 `.part` 再原子改名，输出文件出现即完整，绝不会把半张图当成品
- **零第三方依赖**：Python 3.11 标准库（`tomllib`）
- **可先看后做**：`--dry-run` 打印每张图用哪条规则、什么参数、输出到哪

## 安装

1. 下载引擎（解压即用，无需安装）：

   | 引擎 | 地址 |
   |---|---|
   | Real-CUGAN | `github.com/nihui/realcugan-ncnn-vulkan/releases` |
   | waifu2x | `github.com/nihui/waifu2x-ncnn-vulkan/releases` |

   解压到 `D:\__\realcugan-ncnn-vulkan\`、`D:\__\waifu2x-ncnn-vulkan\`。
   国内直连慢可以套镜像：`https://ghfast.top/https://github.com/...`

2. 复制配置（不复制也能用内置默认值）：

   ```powershell
   copy config.example.toml "$env:USERPROFILE\.config\img-upscale.toml"
   ```

3. 检查环境：

   ```powershell
   python upscale.py --list-backends
   python upscale.py --check-config
   python upscale.py --print-config
   ```

## 用法

```
upscale.py INPUT [INPUT...] [选项]

输入
  文件或目录，可给多个；目录默认只处理当前层，加 -r 递归。

核心
  -s, --scale N      倍数：1=只重绘，2/3/4=放大
  -m, --model NAME   模型名（仅 realesrgan 使用）
  -t, --tile N       分块大小，显存不足时设 256
  --noise N          降噪档 -1/0/1/2/3（realcugan、waifu2x）
  -o, --output DIR   统一输出目录（目录输入时保留子目录结构）
  -c, --config PATH  直接用指定的配置文件运行（不读 ~/.config 那份）
  --suffix TEXT      覆盖输出后缀，如 _x1
  -r, --recursive    递归子目录
  -j, --jobs N       并发进程数
  --engine NAME      waifu2x / realcugan / realesrgan / custom
  -n, --dry-run      只打印计划
  --on-exists MODE   skip / overwrite / rename

增量与续跑
  --force            忽略记录和已有输出，全部重跑
  --no-state         不读不写完成记录
  --state-file PATH  指定记录文件
  --show-state       查看已完成统计
  --clear-state      清空记录
  --verify-hash      对已完成项再做 SHA-256 复核（慢）

诊断
  --print-config     打印生效配置及来源
  --check-config     只校验，退出码 0 通过 / 1 有错
  --list-backends    列出引擎及可用性
```

## 增量是怎么实现的（要不要记哈希）

三层保险，**默认不哈希**：

1. **完成记录**（`~/.config/img-upscale.state.json`）：每成功一张记一条
   `源路径 → 输出路径 + 源文件大小 + 修改时间 + 倍数 + 模型 + 引擎`。
   下次比对这几项，全都一致才跳过。源文件被替换、改了倍数或换了引擎，都会自动重跑。
2. **输出文件存在性**：`on_exists = skip` 时，输出已存在也跳过。
   因为写盘用了 `.part` 临时文件 + 原子改名，**输出文件出现就一定完整**。
3. **可选哈希**：`--verify-hash` 或配置 `verify_hash = true`，对「记录为完成」的文件
   再算一次 SHA-256 复核。默认关闭，因为每次启动要全量读源图（276 张 2K 约 1.5GB）。

> 用大小 + 时间而不是默认哈希：**成本差几十倍，收益几乎为零**。
> 文件内容变了必然改动 mtime 或 size；真要绝对精确再开 `--verify-hash`。

中断后续跑：

```powershell
python upscale.py "G:\媒体\图片\__AI 放大\原图-人物" -s 2 --engine realcugan
# Ctrl+C 打断
# 再跑同一条命令 → 已完成的跳过，只剩没做的
python upscale.py "G:\媒体\图片\__AI 放大\原图-人物" -s 2 --engine realcugan
```

## 输出命名

| 场景 | 结果 |
|---|---|
| 单文件，无 `-o` | `a.png` → 同目录 `a_x1.png` |
| 目录，无 `-o` | `...\原图-人物\a.png` → **源目录内部** `...\原图-人物\原图-人物_x1\a_x1.png` |
| 指定 `-o D:\out` | `D:\out\a_x1.png`；目录输入时保留子目录层级 |

目录模式下输出默认落在**源文件夹内部**的子目录里（`output.dir_base = "inside"`），
想改成与源文件夹同级（`...\原图-人物_x1\`），把 `dir_base` 改成 `"parent"`。
递归扫描时会自动跳过这个输出子目录，不会把自己的产物再放大一遍。

后缀由 `[output] suffix = "_x{scale}"` 决定，`{scale}` 是实际倍数。
想让名字固定带 `_x1`，把 `suffix` 改成 `"_x1"`，或运行时加 `--suffix _x1`。

## 按目录设独立参数

编辑 `~/.config/img-upscale.toml`（模板见 `config.example.toml`）：

```toml
# 壁纸放大 2 倍，噪点少用低降噪
[[rule]]
dir = "G:/媒体/图片/__AI 放大/1x-壁纸"
match = "*.png"
scale = 2
noise = 0

# 人物图 1 倍重绘，只处理 2025 年起的文件
[[rule]]
dir = "G:/媒体/图片/__AI 放大/1x-人物"
match = ["*2025*.png", "*2026*.png"]
scale = 1
noise = 1

# 某目录直接换引擎
[[rule]]
dir = "G:/媒体/图片/__AI 放大/新建文件夹/**"
match = "*.png"
engine = "waifu2x"
noise = 3
scale = 2
```

匹配语义：

- `dir` 匹配**文件所在目录**，纯路径含所有子目录，支持 `*` 单层、`**` 跨层；
- `match` 匹配**文件名**，支持通配符或数组；
- 两者都写则须同时命中；**多条命中时写在后面的优先**；
- 都没命中走 `[default]`；
- 命令行 `-s / -m / -t / --noise` 会覆盖规则里的值。

## 加一个新引擎

1. `lib/backends/` 新建文件，继承 `Backend`，实现 `exe_path()` 与 `build_cmd()`；
2. `lib/backends/__init__.py` 的 `BACKENDS` 登记一行；
3. `config.example.toml` 的 `[engine]` 加可执行文件路径。

不想写代码就用 `custom` 后端：

```toml
[engine]
backend = "custom"

[engine.custom]
bin = "D:\\tools\\my-upscaler.exe"
args = ["-i", "{input}", "-o", "{output}", "-s", "{scale}"]
```

## 性能参考（本机实测，2560×1440 输入）

| 任务 | 引擎 | 单张耗时 | 输出体积 |
|---|---|---|---|
| 1 倍降噪重绘 | waifu2x | ~1.3 s | 4.6–5.4 MB |
| 2 倍放大 | Real-CUGAN | ~3.3 s | 13–16 MB |

276 张 2 倍全量约 15 分钟。`-j 2` 起并发可再快些，但显存吃紧时保持 1。

## 目录结构

```
upscale.py              命令行入口
lib/config.py           配置加载 / 合并 / 校验 / 打印
lib/matcher.py          目录 + 文件名 glob 规则匹配
lib/naming.py           输出路径与后缀模板
lib/state.py            增量记录（原子写 JSON）
lib/runner.py           任务收集、并发执行、进度、汇总
lib/backends/           各引擎实现与注册表
config.example.toml     配置模板（含全部说明）
tests/smoke_test.py     冒烟测试（56 项断言，无需 pytest）
```

## 许可证

MIT
