# bl_cache_to_mp4 v1.0.0

### 把 Bilibili 缓存视频导出为 MP4

将 B站客户端缓存的音视频文件合并导出为 MP4 格式，支持 Android、Windows、Mac 缓存。

基于 [hlbmerge_flutter](https://github.com/molihuan/hlbmerge_flutter) 的核心逻辑，使用 Python 重写为命令行工具。

## 特性

- [x] 支持 Android 客户端缓存（国内版、概念版、谷歌版、HD版）
- [x] 支持 Windows / Mac 客户端缓存
- [x] 自动选择最高画质（多画质缓存时）
- [x] 自动嵌入视频元数据（标题、UP主、封面、合集名、BV号）
- [x] 多线程并发转换
- [x] 实时终端进度显示
- [x] 重复文件检测（跳过/覆盖/副本三选一）
- [x] 可选导出弹幕、封面、原始元数据
- [x] 可选转换后删除源缓存 / 移动到完成目录
- [x] 零外部依赖（仅需 Python 标准库 + ffmpeg）
- [x] 跨平台（Windows / Linux / macOS）

## 注意 !!!

- 缓存目录、输出目录、脚本所在目录的路径**不能有空格或特殊字符**
- `--delete-source` 会**永久删除**源缓存文件夹，不可恢复，请先确认转换质量
- 它只依赖本地缓存文件，不需要网络，只要有缓存就能导出（即使视频已下架）
- 详细的风险说明和使用指南请阅读 [USAGE.md](USAGE.md)

## 环境要求

- **Python 3.10+**
- 无需 `pip install`，全部使用标准库
- **Windows**：ffmpeg 已包含在本文件夹中，开箱即用
- **Linux/macOS**：需系统安装 ffmpeg（`apt install ffmpeg` 或 `brew install ffmpeg`）

## 快速开始

```sh
# 最简用法（自动检测格式，2线程）
python bl_cache_to_mp4.py -i D:\bilibili_cache -o D:\output

# 完整归档（4线程 + 弹幕 + 元数据导出）
python bl_cache_to_mp4.py -i E:\videos -o E:\output -p android -j 4 --danmaku --export-info

# 转换后清理源缓存
python bl_cache_to_mp4.py -i E:\videos -o E:\output -p android -j 4 --danmaku --delete-source
```

更多场景和完整参数说明请阅读 [USAGE.md](USAGE.md)。

## 文件清单

| 文件 | 说明 |
|------|------|
| `bl_cache_to_mp4.py` | 主脚本 |
| `ffmpeg.exe` + `*.dll` | ffmpeg 引擎及依赖（Windows，v4.3.1） |
| [USAGE.md](USAGE.md) | 详细使用指南（参数、场景、风险提示、FAQ） |
| [DESIGN.md](DESIGN.md) | 设计与算法文档 |
| [LICENSE.txt](LICENSE.txt) | CC BY-NC-SA 4.0 开源协议 |

## 软件原理

读取 B站客户端的本地缓存文件 → 解析元数据（标题、UP主等） → 解密 m4s 文件（Win/Mac） → 使用 ffmpeg 合并音视频 → 嵌入封面和元数据标签 → 输出 MP4 文件。

## 许可证

本项目采用 [CC BY-NC-SA 4.0](LICENSE.txt) 协议（与原项目一致）。

- 不得用于商业目的
- 不得移除原作者信息
- 修改后须使用同样的协议
- 可以自由使用、修改和分发

## 特别鸣谢

- [hlbmerge_flutter](https://github.com/molihuan/hlbmerge_flutter) — 原项目，作者 [molihuan](https://github.com/molihuan)
- [bilibili-convert](https://gitee.com/l2063610646/bilibili-convert)
- [ffmpeg_kit_flutter](https://github.com/sk3llo/ffmpeg_kit_flutter)
- [bili-down-out](https://github.com/10miaomiao/bili-down-out)
- [FFmpeg](https://ffmpeg.org/) — 音视频处理引擎
