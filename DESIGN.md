# bl_cache_to_mp4 设计与算法文档

## 1. 架构概览

```
bl_cache_to_mp4.py
│
├── 数据类 ─────────── CacheItem / CacheGroup / MergeTask
│
├── 文件工具 ────────── sanitize_filename / get_available_path(线程安全) / change_ext
│
├── JSON 解析 ───────── try_parse_json（容错解析）
│
├── 缓存扫描器（os.scandir + 多线程并行，SMB/NAS 友好）
│   ├── _scan_quality_dir()          ← 单次 scandir 扫描画质目录
│   ├── _scan_android_episode()      ← 单次 scandir 扫描分集（文件+子目录一次收集）
│   ├── _scan_android_group()        ← 扫描合集下所有分集
│   ├── scan_android_cache()         ← Android 缓存（8线程并行扫描合集）
│   ├── _scan_windows_episode()      ← 单次 scandir 扫描分集
│   ├── scan_windows_cache()         ← Windows/Mac 缓存（8线程并行扫描分集）
│   ├── auto_detect_platform()       ← 自动检测缓存格式（快速检查前10个目录）
│   └── _group_items()               ← 按合集分组 + 排序
│
├── 解密模块 ────────── decrypt_pc_m4s()（Windows/Mac m4s 头部处理）
│
├── FFmpeg 合并（自动嵌入封面 + 元数据）
│   ├── merge_audio_video()    ← 音频 + 视频 → MP4
│   └── merge_blv_videos()     ← 多段 BLV → MP4
│
├── 任务引擎 ────────── TaskEngine
│   ├── 重复检测（跳过/覆盖/副本）
│   ├── 多线程调度（ThreadPoolExecutor）
│   ├── 后处理：导出 sidecar 文件（弹幕/封面/info.json）
│   ├── 后处理：移动到完成目录 或 删除源缓存文件夹
│   └── 删除源时自动清理空的合集目录
│
├── 日志模块 ────────── _setup_logger()（写入 ./log/，按时间命名）
│
├── 进度显示 ────────── ProgressDisplay（终端实时刷新，GBK 安全）
│
└── CLI 入口 ────────── main()（argparse + 互斥校验 + 重复检测交互）
```

## 2. 数据流

```
用户输入缓存目录路径
        │
        ▼
┌─────────────────────┐
│    自动检测格式       │ ← auto_detect_platform()
│  Android / Windows   │   查找 entry.json 或 .videoInfo
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│    扫描缓存目录       │ ← scan_android_cache() / scan_windows_cache()
│  8线程并行扫描        │   ThreadPoolExecutor(max_workers=8)
│  os.scandir 单次遍历  │   每个目录只扫一次，同时收集文件和子目录
│  解析 JSON 元数据     │   parse_android_json() / parse_windows_json()
│  选择最高画质目录     │   _scan_quality_dir() → 按画质ID降序取最高
│  识别音视频文件       │   classify_m4s_files()
│  收集弹幕/封面路径    │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│    分组 + 排序        │ ← _group_items()
│  按 group_id 聚合     │
│  组内按 p(分P) 排序   │
│  可选添加序号前缀     │
└─────────┬───────────┘
          │
          ▼
    显示扫描结果
    用户确认 (y/N)
          │
          ▼
┌─────────────────────┐
│   检测重复文件        │ ← _detect_duplicates()
│   提示：跳过/覆盖/副本│
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│    构建任务列表       │ ← TaskEngine.build_tasks()
│  每个 CacheItem      │
│  → 一个 MergeTask    │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────────────────────────┐
│         多线程执行（ThreadPoolExecutor）   │
│                                         │
│  线程1: ┌───────────────────────┐       │
│         │ Windows/Mac?          │       │
│         │  → 解密 m4s → 临时文件 │       │
│         │  → ffmpeg 合并        │       │
│         │  → 删除临时文件       │       │
│         │                       │       │
│         │ Android?              │       │
│         │  → ffmpeg 直接合并    │       │
│         │                       │       │
│         │ BLV?                  │       │
│         │  → ffmpeg concat 拼接 │       │
│         │                       │       │
│         │ → 导出 sidecar 文件     │       │
│         │   (弹幕/封面/info.json)│       │
│         │ → 移动到完成目录（可选）│       │
│         │ → 删除源文件夹（可选） │       │
│         │   → 清理空合集目录     │       │
│         └───────────────────────┘       │
│                                         │
│  线程2: （同上，处理另一个任务）          │
│  ...                                    │
│                                         │
│  ProgressDisplay: 每 0.5s 刷新终端状态    │
└─────────────────┬───────────────────────┘
                  │
                  ▼
           打印最终汇总
      成功数 / 失败数 / 耗时
         列出失败详情
      写入日志汇总到 ./log/
```

## 3. 数据模型

### CacheItem（单个视频）

| 字段 | 类型 | 说明 |
|------|------|------|
| path | str | 缓存项目录路径 |
| parent_path | str | 父目录路径 |
| json_path | str | 元数据文件路径（entry.json 或 .videoInfo） |
| audio_path | str | 音频文件路径 |
| video_path | str | 视频文件路径 |
| blv_paths | list[str] | BLV 分段文件路径列表 |
| danmaku_path | str | 弹幕文件路径（.xml 或 .dm1） |
| cover_path | str | 封面图片路径 |
| title | str | 视频标题 |
| group_id | str | 合集 ID（用于分组） |
| group_title | str | 合集标题 |
| p | int \| None | 分P编号 |
| av_id / bv_id / c_id | str | B站视频标识符 |
| owner_name | str | UP主名 |

可合并条件（`can_merge()`）：`(audio_path AND video_path) OR blv_paths 非空`

### CacheGroup（合集）

| 字段 | 类型 | 说明 |
|------|------|------|
| group_id | str | 合集唯一标识 |
| title | str | 合集标题 |
| path | str | 合集目录路径 |
| items | list[CacheItem] | 组内视频列表（已按 p 排序） |

### MergeTask（合并任务）

| 字段 | 类型 | 说明 |
|------|------|------|
| item | CacheItem | 要合并的视频 |
| group_title | str | 所属合集标题（用于输出子目录名） |
| platform | str | 缓存来源平台 |
| status | str | 任务状态：pending → running → completed/failed/skipped |
| error | str | 失败时的错误信息 |
| output_path | str | 输出文件路径 |
| source_size | int | 源文件总大小（字节，音频+视频或BLV合计） |
| output_size | int | 输出 MP4 文件大小（字节） |
| start_time | float | 任务开始时间戳 |
| end_time | float | 任务结束时间戳 |

## 4. 缓存格式解析

### 4.1 Android 缓存

**目录结构：**

```
根目录/
├── {group_id_1}/                    ← 第一层：合集 ID（数字目录名）
│   ├── c_{episode_id_1}/           ← 第二层：分集目录
│   │   ├── entry.json              ← 元数据（标题、ID、分P号）
│   │   ├── danmaku.xml             ← 弹幕文件
│   │   ├── cover.jpg               ← 封面图
│   │   ├── 64/                     ← 画质目录（720P）
│   │   │   ├── audio.m4s
│   │   │   ├── video.m4s
│   │   │   └── index.json
│   │   └── 80/                     ← 画质目录（1080P），可能同时存在多个
│   │       ├── audio.m4s
│   │       ├── video.m4s
│   │       └── index.json
│   └── c_{episode_id_2}/
│       └── ...
└── {group_id_2}/
    └── ...
```

**多画质选择算法：**

同一分集可能缓存了多个画质。`_scan_android_episode()` 在单次 `os.scandir` 中同时收集顶层文件和子目录列表，然后对数字命名的子目录调用 `_scan_quality_dir()` 选择最佳画质：
1. 单次 scandir 分集目录，同时收集文件（entry.json 等）和子目录列表
2. 对数字命名的子目录，各调一次 `_scan_quality_dir()` 获取音视频文件路径
3. 过滤掉不完整的目录（缺少 audio.m4s + video.m4s 或 .blv 文件）
4. 选择画质ID最大的目录（数字越大画质越高）
5. 兜底：如果没有数字目录，扫描非数字子目录查找音视频文件（找到即停）

画质ID对照：6=240P, 16=360P, 32=480P, 64=720P, 80=1080P, 112=1080P+, 116=1080P60, 120=4K, 125=HDR, 126=杜比视界, 127=8K

**也可能是 BLV 格式（旧版缓存）：**

```
{episode_dir}/
├── entry.json
└── {quality}/
    ├── 0.blv          ← 视频分段1
    ├── 1.blv          ← 视频分段2
    └── ...
```

**entry.json 字段提取：**

```python
# 优先级：page_data → ep → 根级字段
title = page_data.part          # 普通视频的分P标题
     or ep.index_title          # 番剧的集标题
     or data.title              # 视频总标题
     or bvid / avid / cid       # 最终回退

p     = page_data.page          # 普通视频的分P号
     or ep.index / ep.page      # 番剧的集号

group_title = data.title        # 合集标题
group_id    = 第一层目录路径     # 用目录路径作为分组依据
```

### 4.2 Windows/Mac 缓存

**目录结构：**

```
根目录/
├── {episode_id_1}/              ← 扁平结构，每集一个文件夹
│   ├── {cid}.videoInfo          ← 元数据 JSON
│   ├── 30280.m4s                ← 音频（固定文件名）
│   ├── {quality_id}.m4s         ← 视频
│   ├── {cid}.dm1                ← 弹幕文件
│   ├── image.jpg                ← 分集封面
│   └── group.jpg                ← 合集封面
├── {episode_id_2}/
│   └── ...
```

**.videoInfo 字段提取：**

```python
title = data.title or data.tabName or data.cid or data.bvid or data.aid
p     = data.p
group_id    = data.groupId       # JSON 中明确给出
group_title = data.groupTitle
```

### 4.3 m4s 音视频判定算法

当一个目录中有恰好 2 个 `.m4s` 文件时：

```
判定规则（按优先级）：
1. 文件名以 "30280.m4s" 结尾 → 该文件是音频
2. 否则，文件大小较小的 → 音频

返回：(音频路径, 视频路径)

如果不是恰好 2 个 m4s 文件 → 返回 None，该目录被跳过
```

> `30280` 是 B站音频流的固定 quality ID。视频流的 quality ID 随画质变化（如 80 = 1080P）。

## 5. 解密算法

### 适用范围

仅 Windows/Mac 缓存的 m4s 文件需要解密。Android 缓存的 m4s 文件可直接被 ffmpeg 读取。

### 原理

Bilibili 在 2024 年 3 月后的 Windows/Mac 客户端中，对缓存的 m4s 文件头部添加了 `"000000000"` 填充。这不是真正的加密，只是破坏了文件头部的 MPEG-4 格式标识，使 ffmpeg 无法直接识别。

### 算法步骤

```
输入：加密的 m4s 文件
输出：可被 ffmpeg 读取的 m4s 文件

1. 读取源文件的前 32 字节（文件头）
2. 将 32 字节按 ASCII 解码为字符串（不可解码字节用 ? 替代）
3. 从字符串中移除所有 "000000000"（9个零）
4. 将处理后的字符串编码回 ASCII 字节
5. 写入输出文件：处理后的头部 + 源文件剩余内容
```

### 伪代码

```python
def decrypt_pc_m4s(src_path, dst_path):
    with open(src_path, "rb") as fin:
        header = fin.read(32)                           # 步骤1
        header_str = header.decode("ascii", "replace")  # 步骤2
        header_str = header_str.replace("000000000", "")# 步骤3
        clean_header = header_str.encode("ascii", "replace") # 步骤4

        with open(dst_path, "wb") as fout:
            fout.write(clean_header)                    # 步骤5a
            while chunk := fin.read(256 * 1024 * 1024): # 步骤5b
                fout.write(chunk)
```

### 关键细节

- 头部替换后长度会**缩短**（原 32 字节 → 约 23 字节），输出文件比源文件短 9 字节
- `replaceAll` 语义：如果头部中有多个 `"000000000"` 序列，全部移除
- 后续字节不做任何处理，原样复制
- 分块复制使用 256MB 缓冲区，处理大文件不会撑爆内存

## 6. FFmpeg 命令

### 音视频合并（带封面 + 元数据）

当封面图存在时：

```bash
ffmpeg -y -i <音频> -i <视频> -i <封面.jpg> \
  -map 0:a -map 1:v -map 2:v \
  -c:a copy -c:v:0 copy -c:v:1 mjpeg \
  -disposition:v:1 attached_pic \
  -metadata title="标题" \
  -metadata artist="UP主" \
  -metadata album="合集名" \
  -metadata track="1" \
  -metadata comment="BV号" \
  <输出.mp4>
```

| 参数 | 说明 |
|------|------|
| `-map 0:a -map 1:v -map 2:v` | 分别映射音频、视频、封面图三个输入流 |
| `-c:v:0 copy` | 视频流直接复制（不重编码） |
| `-c:v:1 mjpeg` | 封面图用 mjpeg 编码 |
| `-disposition:v:1 attached_pic` | 标记封面为附加图片（播放器/资源管理器识别为缩略图） |
| `-metadata key=value` | 嵌入 MP4 标准元数据（title/artist/album/track/comment） |

无封面时回退：

```bash
ffmpeg -y -i <音频> -i <视频> -c copy -metadata title="标题" ... <输出.mp4>
```

封面嵌入失败时会自动回退到无封面命令。

### 元数据映射关系

| MP4 标签 | 数据来源 | 说明 |
|----------|---------|------|
| `title` | CacheItem.title | 视频标题 |
| `artist` | CacheItem.owner_name | UP主名（Android: entry.json → owner_name） |
| `album` | MergeTask.group_title | 合集标题 |
| `track` | CacheItem.p | 分P/集数编号 |
| `comment` | CacheItem.bv_id 或 av_id | B站视频标识符 |

### BLV 分段拼接

```bash
ffmpeg -y -i "concat:<1.blv>|<2.blv>|<3.blv>" -c copy \
  -metadata title="标题" ... <输出.mp4>
```

| 参数 | 说明 |
|------|------|
| `concat:` | ffmpeg 内置的拼接协议 |
| `\|` | 文件分隔符 |
| `-c copy` | 直接复制，不重新编码 |

BLV 文件在拼接前会按文件名中的数字排序：

```python
# 排序规则：提取文件名中 "(\d+)\.blv" 的数字部分
# 0.blv → 0, 1.blv → 1, 2.blv → 2, ...
sorted(blv_paths, key=lambda p: int(re.search(r'(\d+)\.blv$', p).group(1)))
```

## 7. 多线程模型

### 扫描阶段线程池

```python
ThreadPoolExecutor(max_workers=8)
```

- **Android**：每个合集目录（一级目录）作为独立任务并行扫描
- **Windows/Mac**：每个分集目录作为独立任务并行扫描
- 使用 `os.scandir` 代替 `Path.iterdir()`，减少系统调用（SMB 上每次 scandir 是一次网络往返，而 `iterdir()` + `is_file()` 需要 N+1 次）
- 每个分集目录只做一次 scandir，同时收集文件和子目录列表
- 单个任务异常不影响其他任务（`try/except` 包裹 `future.result()`）

### 合并阶段线程池

```python
ThreadPoolExecutor(max_workers=N)  # N 由 --jobs 参数指定，默认 2
```

- 每个 `MergeTask` 作为独立任务提交到线程池
- 线程间无共享文件操作（每个任务输出路径唯一）
- 任务状态字段（`status`、`error`）由各自线程写入，进度显示线程只读

### 线程安全

| 共享资源 | 保护方式 |
|----------|----------|
| 任务列表 | 构建阶段完成后只读，运行阶段不修改列表结构 |
| 任务状态字段 | 每个字段只被一个工作线程写入 |
| 进度显示 | `threading.Lock` 保护终端输出 |
| 文件系统 | `get_available_path()` 使用 `threading.Lock` + 占位文件确保路径唯一 |

### 进度显示

独立守护线程，每 0.5 秒刷新。使用 ANSI 转义序列覆盖输出，编码安全（GBK 降级）。

**终端布局（从上到下）：**

```
┌─ 滚动文件列表 ──────────────────────────────────────────┐
│  [合并] 第49集.标题xxx (245.3MB)  12.5s                  │
│  [合并] 第50集.标题xxx (198.7MB)  3.2s                   │
│  [等待] 第51集.标题xxx                                   │
│  ... 还有 68 个等待中                                    │
├─ 性能状态 ──────────────────────────────────────────────┤
│  扫描: 3.2s (120个目录) | 读取: 45.2MB/s | 写入: 38.7MB/s | 转换: 8.3s/集 │
│  当前: 第49集.标题 (245.3MB) | 已完成: 1.8GB / 48集 | 预计剩余: 9m24s      │
├─ 进度条 ────────────────────────────────────────────────┤
│  [################--------------] 50/120  运行:2 完成:48  │
└─────────────────────────────────────────────────────────┘
```

- **滚动文件列表**：只显示正在处理（running）和即将处理（pending）的任务，已完成的自动滚出。每个 running 任务显示源文件大小和已用时间
- **性能状态**：扫描耗时、读取速度（源文件→内存）、写入速度（内存→输出）、平均转换速度、当前文件信息、累计完成量、预计剩余时间
- **进度条**：固定在底部，格式不变。运行/完成/失败/等待计数

**速度计算方式：**

| 指标 | 计算方式 | 意义 |
|------|---------|------|
| 读取速度 | 已完成任务的源文件总大小 ÷ 已完成任务的处理时间总和 | 体现 SMB 读取性能 |
| 写入速度 | 已完成任务的输出文件总大小 ÷ 已完成任务的处理时间总和 | 体现 SMB 写入性能 |
| 转换速度 | 已完成任务的处理时间总和 ÷ 已完成任务数 | 平均每集耗时 |
| 预计剩余 | 平均每集耗时 × 剩余数 ÷ 有效并发数 | 有效并发数 = 任务处理时间总和 ÷ 墙钟时间 |

## 8. 日志系统

### 初始化

脚本启动时自动在脚本同目录的 `./log/` 文件夹下创建日志文件，文件名格式：`YYYY-MM-DD_HHMMSS.log`。使用 Python 标准库 `logging` 模块，仅写文件（`FileHandler`），不影响终端输出。

### 日志级别

| 级别 | 记录内容 |
|------|---------|
| DEBUG | FFmpeg 完整命令行、临时文件清理 |
| INFO | 任务开始/完成、转换成功、解密成功、删除/移动/导出操作 |
| ERROR | 转换失败、解密失败、删除失败、移动失败、FFmpeg 执行失败 |

### 记录的操作类型

| 标签 | 说明 |
|------|------|
| `[任务开始]` | 开始处理一个视频 |
| `[转换完成]` / `[转换失败]` | 单个视频的最终结果 |
| `[FFmpeg]` | FFmpeg 命令执行及结果 |
| `[解密]` | Windows/Mac m4s 文件解密 |
| `[删除]` | 源文件夹、空父目录、失败产物、临时文件 |
| `[移动]` | 文件移动到完成目录 |
| `[导出]` | 封面/元数据/弹幕导出 |
| `[跳过]` | 跳过已存在的文件 |
| `===== 任务汇总 =====` | 最终统计（成功/失败/跳过/耗时） |

### 日志格式

```
2024-03-28 14:30:52  INFO     [任务开始] 视频标题 (平台: windows)
2024-03-28 14:30:52  DEBUG    [FFmpeg] 执行命令: ffmpeg -y -i ...
2024-03-28 14:30:55  INFO     [FFmpeg] 成功  output/标题.mp4
2024-03-28 14:30:55  INFO     [转换完成] 视频标题 -> output/标题.mp4
```

## 9. 容错 JSON 解析

B站缓存的 JSON 文件偶尔存在格式问题（文件截断、多余逗号等）。解析策略：

```
第 1 次尝试：直接 json.loads()
    ↓ 失败
第 2 次尝试：修复后重试
    ├── 移除尾逗号：  ,} → }    ,] → ]
    ├── 移除首尾逗号
    ├── 移除控制字符：\x00-\x1f
    └── json.loads(fixed)
    ↓ 失败
第 3 次尝试：平衡括号后重试
    ├── { 多于 } → 末尾补 }
    ├── } 多于 { → 末尾删 }
    ├── [ 和 ] 同理
    └── json.loads(balanced)
    ↓ 失败
抛出异常，该缓存项被跳过
```

## 10. 文件名安全处理

### 非法字符替换

```python
# Windows 非法字符：替换为下划线
re.sub(r'[<>:"/\\|?*]', '_', filename)
```

### 首尾清理

```python
# 移除首尾空格和句点
re.sub(r'^[ .]+|[ .]+$', '_', filename)
```

### Windows 保留名

以下名称不能用作文件名，会自动加 `_` 前缀：

```
CON PRN AUX NUL
COM1-COM9
LPT1-LPT9
```

### 长度限制

文件名超过 200 字符时截断（保留足够空间给路径和后缀）。
