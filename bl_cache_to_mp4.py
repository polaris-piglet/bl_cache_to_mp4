#!/usr/bin/env python3
"""
bl_cache_to_mp4 v1.0.0 — Bilibili 缓存视频合并工具

将 Bilibili 客户端缓存的音视频文件合并导出为 MP4。
支持 Android / Windows / Mac 三种缓存格式。
仅依赖 Python 标准库 + ffmpeg 可执行文件。

基于 hlbmerge_flutter (https://github.com/molihuan/hlbmerge_flutter) 的核心逻辑重写。
协议：CC BY-NC-SA 4.0，详见 LICENSE.txt。
"""

__version__ = "1.0.0"

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path


# ============================================================
# 日志配置
# ============================================================

def _setup_logger() -> logging.Logger:
    """初始化日志系统，日志文件写入 ./log/ 目录，按日期命名。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, "log")
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, time.strftime("%Y-%m-%d_%H%M%S") + ".log")

    logger = logging.getLogger("bl_cache_to_mp4")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    return logger


log = _setup_logger()


# ============================================================
# 数据类
# ============================================================

@dataclass
class CacheItem:
    """单个视频的缓存信息。对应缓存目录中一个分集的所有文件路径和元数据。"""
    path: str = ""              # 分集缓存目录路径（如 c_80020353/）
    parent_path: str = ""       # 父目录路径（合集目录，如 44855426/）
    json_path: str = ""         # 元数据文件路径（entry.json 或 .videoInfo）
    audio_path: str = ""        # 音频文件路径
    video_path: str = ""        # 视频文件路径
    blv_paths: list = field(default_factory=list)  # BLV 分段文件路径列表（旧版格式）
    danmaku_path: str = ""      # 弹幕文件路径（.xml 或 .dm1）
    cover_path: str = ""        # 封面图片路径
    title: str = ""             # 视频标题
    group_id: str = ""          # 合集 ID（用于分组）
    group_title: str = ""       # 合集标题
    group_cover_path: str = ""  # 合集封面路径
    p: int | None = None        # 分P编号（用于排序和序号）
    av_id: str = ""             # AV 号
    bv_id: str = ""             # BV 号
    c_id: str = ""              # CID
    owner_name: str = ""        # UP主名

    def can_merge(self) -> bool:
        """判断是否有足够的源文件可以合并。"""
        return (self.audio_path and self.video_path) or bool(self.blv_paths)


@dataclass
class CacheGroup:
    """视频合集，包含多个 CacheItem（分集），按 group_id 聚合。"""
    group_id: str = ""
    title: str = ""
    path: str = ""
    items: list = field(default_factory=list)


@dataclass
class MergeTask:
    """一个合并任务，跟踪单个视频从开始到完成的状态。"""
    item: CacheItem
    group_title: str = ""
    platform: str = "windows"   # android / windows / mac
    status: str = "pending"     # pending / running / completed / failed / skipped
    error: str = ""             # 失败时的错误信息
    output_path: str = ""       # 输出文件路径


# ============================================================
# 文件工具
# ============================================================

_WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_length: int = 200) -> str:
    """清理文件名中的非法字符（兼容 Windows）。不处理扩展名，只清理纯标题。"""
    if not name:
        return "_"
    s = _WINDOWS_ILLEGAL.sub("_", name)
    s = re.sub(r'^[ .]+|[ .]+$', '_', s)
    if s.upper() in _RESERVED_NAMES:
        s = f"_{s}"
    if len(s) > max_length:
        s = s[:max_length]
    return s or "_"


_path_lock = threading.Lock()  # 保护文件路径分配，防止多线程竞态


def get_available_path(filepath: str) -> str:
    """如果文件已存在，自动添加 (0), (1), ... 后缀。线程安全，通过占位文件防止竞态。"""
    with _path_lock:
        if not os.path.exists(filepath):
            # 创建占位空文件防止其他线程拿到同一路径
            Path(filepath).parent.mkdir(parents=True, exist_ok=True)
            Path(filepath).touch(exist_ok=True)
            return filepath
        base, ext = os.path.splitext(filepath)
        counter = 0
        while True:
            new_path = f"{base}({counter}){ext}"
            if not os.path.exists(new_path):
                Path(new_path).touch(exist_ok=True)
                return new_path
            counter += 1


def change_ext(filepath: str, new_ext: str) -> str:
    base, _ = os.path.splitext(filepath)
    return f"{base}.{new_ext}"


# ============================================================
# JSON 解析（容错）
# ============================================================

def try_parse_json(text: str) -> dict:
    """尝试解析 JSON，失败则修复后重试。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 修复常见问题
    fixed = text
    fixed = re.sub(r',\s*}', '}', fixed)
    fixed = re.sub(r',\s*]', ']', fixed)
    fixed = re.sub(r'^,', '', fixed)
    fixed = re.sub(r',\s*$', '', fixed)
    fixed = re.sub(r'[\x00-\x1f]', '', fixed)
    fixed = fixed.strip()
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    # 平衡括号
    open_c = fixed.count('{')
    close_c = fixed.count('}')
    if open_c > close_c:
        fixed += '}' * (open_c - close_c)
    elif close_c > open_c:
        fixed = fixed[:-(close_c - open_c)]
    open_s = fixed.count('[')
    close_s = fixed.count(']')
    if open_s > close_s:
        fixed += ']' * (open_s - close_s)
    elif close_s > open_s:
        fixed = fixed[:-(close_s - open_s)]
    return json.loads(fixed)


def _get_str(data: dict, key: str) -> str:
    v = data.get(key)
    if v is None:
        return ""
    return str(v)


def _get_int(data: dict, key: str) -> int | None:
    v = data.get(key)
    if v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v))
    except (ValueError, TypeError):
        return None


def _first_non_empty(*values: str) -> str:
    for v in values:
        if v and v.strip():
            return v
    return ""


def _first_non_none(*values):
    for v in values:
        if v is not None:
            return v
    return None


# ============================================================
# 缓存扫描
# ============================================================

def parse_android_json(json_path: str) -> dict:
    """解析 Android 缓存的 entry.json。"""
    text = Path(json_path).read_text(encoding="utf-8", errors="replace")
    data = try_parse_json(text)

    info = {
        "av_id": _get_str(data, "avid"),
        "bv_id": _get_str(data, "bvid"),
        "title": "",
        "p": None,
        "group_title": _get_str(data, "title"),
        "cover_url": _get_str(data, "cover"),
        "owner_name": _get_str(data, "owner_name"),
    }

    page_data = data.get("page_data")
    if page_data:
        info["title"] = _get_str(page_data, "part")
        info["c_id"] = _get_str(page_data, "cid")
        info["p"] = _get_int(page_data, "page")

    ep = data.get("ep")
    if ep:
        info["title"] = _first_non_empty(_get_str(ep, "index_title"))
        info["p"] = _first_non_none(
            _get_int(ep, "index"),
            _get_int(ep, "page"),
            _get_int(ep, "sort_index"),
        )

    info["title"] = _first_non_empty(
        info["title"],
        _get_str(data, "title"),
        info.get("bv_id", ""),
        info.get("av_id", ""),
        info.get("c_id", ""),
    )

    return info


def parse_windows_json(json_path: str) -> dict:
    """解析 Windows/Mac 缓存的 .videoInfo。"""
    text = Path(json_path).read_text(encoding="utf-8", errors="replace")
    data = try_parse_json(text)

    info = {
        "av_id": _get_str(data, "aid"),
        "bv_id": _get_str(data, "bvid"),
        "title": _first_non_empty(
            _get_str(data, "title"),
            _get_str(data, "tabName"),
            _get_str(data, "cid"),
            _get_str(data, "bvid"),
            _get_str(data, "aid"),
            _get_str(data, "itemId"),
            _get_str(data, "p"),
        ),
        "p": _get_int(data, "p"),
        "group_id": _get_str(data, "groupId"),
        "group_title": _get_str(data, "groupTitle"),
        "cover_path": _get_str(data, "coverPath"),
        "group_cover_path": _get_str(data, "groupCoverPath"),
        "owner_name": _get_str(data, "ownerName"),
    }
    return info


def classify_m4s_files(m4s_files: list[str]) -> tuple[str, str] | None:
    """从两个 m4s 文件中判断哪个是音频、哪个是视频。返回 (audio, video) 或 None。"""
    if len(m4s_files) != 2:
        return None
    f1, f2 = m4s_files

    # 按文件名判断：30280.m4s 是音频
    if f1.endswith("30280.m4s"):
        return (f1, f2)
    if f2.endswith("30280.m4s"):
        return (f2, f1)

    # 按文件大小判断：小的是音频
    s1 = os.path.getsize(f1)
    s2 = os.path.getsize(f2)
    if s1 < s2:
        return (f1, f2)
    else:
        return (f2, f1)


def _scan_quality_dir(quality_path: str) -> dict:
    """单次 scandir 扫描一个画质目录，返回文件信息。"""
    audio = video = ""
    blv_files = []
    try:
        with os.scandir(quality_path) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                if name == "audio.m4s":
                    audio = entry.path
                elif name == "video.m4s":
                    video = entry.path
                elif name.endswith(".blv"):
                    blv_files.append(entry.path)
    except (PermissionError, OSError):
        pass
    return {"audio": audio, "video": video, "blv": blv_files}


def _scan_android_episode(second_dir_path: str, first_dir_path: str) -> CacheItem | None:
    """扫描单个 Android 分集目录，返回 CacheItem 或 None。
    单次 os.scandir 同时收集文件和子目录，最小化系统调用。"""
    item = CacheItem()
    item.parent_path = first_dir_path
    item.path = second_dir_path

    # 单次 scandir：同时收集顶层文件和子目录列表
    subdirs = []  # (name, path) — 数字目录和非数字目录都收集
    try:
        with os.scandir(second_dir_path) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False):
                    name = entry.name
                    if name == "entry.json":
                        item.json_path = entry.path
                        try:
                            info = parse_android_json(entry.path)
                            item.av_id = info.get("av_id", "")
                            item.bv_id = info.get("bv_id", "")
                            item.title = info.get("title", "")
                            item.c_id = info.get("c_id", "")
                            item.p = info.get("p")
                            item.group_title = info.get("group_title", "")
                            item.owner_name = info.get("owner_name", "")
                        except Exception as e:
                            print(f"  [警告] 解析 {entry.path} 失败: {e}")
                    elif name == "danmaku.xml":
                        item.danmaku_path = entry.path
                    elif name == "cover.jpg":
                        item.cover_path = entry.path
                elif entry.is_dir(follow_symlinks=False):
                    subdirs.append((entry.name, entry.path))
    except (PermissionError, OSError):
        return None

    # 从子目录中选择最高画质（数字目录名 = 画质 ID）
    candidates = []
    fallback_dirs = []
    for name, path in subdirs:
        if name.isdigit():
            info = _scan_quality_dir(path)
            if (info["audio"] and info["video"]) or info["blv"]:
                candidates.append((int(name), info))
        else:
            fallback_dirs.append(path)

    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0][1]
        item.audio_path = best["audio"]
        item.video_path = best["video"]
        item.blv_paths = best["blv"]
    else:
        # 兜底：非数字目录中查找音视频文件（找到即停）
        for fb_path in fallback_dirs:
            fb = _scan_quality_dir(fb_path)
            if (fb["audio"] and fb["video"]) or fb["blv"]:
                item.audio_path = fb["audio"]
                item.video_path = fb["video"]
                item.blv_paths = fb["blv"]
                break

    if item.can_merge():
        item.group_id = first_dir_path
        item.group_cover_path = item.cover_path
        return item
    return None


def _scan_android_group(first_dir_path: str) -> list[CacheItem]:
    """扫描单个 Android 合集目录下的所有分集。"""
    items = []
    try:
        with os.scandir(first_dir_path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    result = _scan_android_episode(entry.path, first_dir_path)
                    if result:
                        items.append(result)
    except (PermissionError, OSError):
        pass
    return items


def scan_android_cache(root_dir: str, add_index: bool = True) -> list[CacheGroup]:
    """扫描 Android 缓存目录。
    目录结构：根目录/{合集ID}/{分集ID}/entry.json + {画质ID}/audio.m4s + video.m4s
    自动选择最高画质目录，按合集分组，组内按分P排序。
    使用 os.scandir 减少系统调用，多线程并行扫描各合集目录。"""
    if not os.path.isdir(root_dir):
        return []

    # 收集一级目录
    group_dirs = []
    try:
        with os.scandir(root_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    group_dirs.append(entry.path)
    except (PermissionError, OSError):
        return []

    # 多线程并行扫描各合集目录
    items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_scan_android_group, d): d for d in group_dirs}
        for future in as_completed(futures):
            try:
                batch = future.result()
                items.extend(batch)
            except Exception as e:
                print(f"  [警告] 扫描目录失败: {futures[future]}: {e}")

    return _group_items(items, add_index)


def _scan_windows_episode(first_dir_path: str, root_dir: str) -> CacheItem | None:
    """扫描单个 Windows/Mac 分集目录，返回 CacheItem 或 None。"""
    item = CacheItem()
    item.parent_path = root_dir
    item.path = first_dir_path
    m4s_files = []

    try:
        with os.scandir(first_dir_path) as it:
            for entry in it:
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                if name.endswith(".videoInfo"):
                    item.json_path = entry.path
                    try:
                        info = parse_windows_json(entry.path)
                        item.av_id = info.get("av_id", "")
                        item.bv_id = info.get("bv_id", "")
                        item.title = info.get("title", "")
                        item.p = info.get("p")
                        item.group_id = info.get("group_id", "")
                        item.group_title = info.get("group_title", "")
                        item.group_cover_path = info.get("group_cover_path", "")
                        item.owner_name = info.get("owner_name", "")
                    except Exception as e:
                        print(f"  [警告] 解析 {entry.path} 失败: {e}")
                elif name.endswith(".dm1"):
                    item.danmaku_path = entry.path
                elif name.endswith(".m4s"):
                    m4s_files.append(entry.path)
                elif name == "group.jpg":
                    item.group_cover_path = entry.path
                elif name == "image.jpg":
                    item.cover_path = entry.path
    except (PermissionError, OSError):
        return None

    av = classify_m4s_files(m4s_files)
    if av:
        item.audio_path, item.video_path = av
        return item
    return None


def scan_windows_cache(root_dir: str, add_index: bool = True) -> list[CacheGroup]:
    """扫描 Windows/Mac 缓存目录。
    目录结构：根目录/{分集ID}/.videoInfo + 两个.m4s文件
    按 groupId 分组，组内按分P排序。
    使用 os.scandir 减少系统调用，多线程并行扫描各分集目录。"""
    if not os.path.isdir(root_dir):
        return []

    # 收集一级目录
    episode_dirs = []
    try:
        with os.scandir(root_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    episode_dirs.append(entry.path)
    except (PermissionError, OSError):
        return []

    # 多线程并行扫描各分集目录
    items = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_scan_windows_episode, d, root_dir): d for d in episode_dirs}
        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    items.append(result)
            except Exception as e:
                print(f"  [警告] 扫描目录失败: {futures[future]}: {e}")

    return _group_items(items, add_index)


def _group_items(items: list[CacheItem], add_index: bool) -> list[CacheGroup]:
    """按 group_id 对 CacheItem 分组，组内按 p 排序。"""
    groups_map: dict[str, CacheGroup] = {}
    for item in items:
        gid = item.group_id or item.path
        if gid in groups_map:
            group = groups_map[gid]
            group.path = item.parent_path
            group.items.append(item)
        else:
            group = CacheGroup(
                group_id=gid,
                title=item.group_title,
                path=item.path,
                items=[item],
            )
            groups_map[gid] = group

    result = list(groups_map.values())

    for group in result:
        group.items.sort(key=lambda x: (x.p is None, x.p or 0))
        if add_index and len(group.items) > 1:
            for it in group.items:
                if it.p is not None:
                    it.title = f"{it.p}.{it.title}"

    return result


def auto_detect_platform(root_dir: str, max_check: int = 10) -> str:
    """自动检测缓存格式。只检查前几个目录，快速返回。使用 os.scandir 减少系统调用。"""
    checked = 0
    try:
        with os.scandir(root_dir) as root_it:
            for d in root_it:
                if not d.is_dir(follow_symlinks=False):
                    continue
                if checked >= max_check:
                    break
                checked += 1
                try:
                    with os.scandir(d.path) as sub_it:
                        for f in sub_it:
                            if f.is_file(follow_symlinks=False) and f.name.endswith(".videoInfo"):
                                return "windows"
                            if f.is_dir(follow_symlinks=False):
                                try:
                                    with os.scandir(f.path) as inner_it:
                                        for ff in inner_it:
                                            if ff.name == "entry.json":
                                                return "android"
                                except (PermissionError, OSError):
                                    pass
                                break
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass
    return "windows"


# ============================================================
# 解密 (Windows/Mac m4s)
# ============================================================

def decrypt_pc_m4s(src_path: str, dst_path: str, buf_size: int = 256 * 1024 * 1024) -> bool:
    """解密 Windows/Mac 的 m4s 文件。
    B站2024年3月后的桌面客户端在 m4s 文件头部插入了 '000000000' 填充，
    破坏了 MPEG-4 格式头，使 ffmpeg 无法识别。本函数读取前32字节，
    移除填充后写入新文件，其余内容原样复制。"""
    try:
        with open(src_path, "rb") as fin, open(dst_path, "wb") as fout:
            header = fin.read(32)
            header_str = header.decode("ascii", errors="replace")
            header_str = header_str.replace("000000000", "")
            fout.write(header_str.encode("ascii", errors="replace"))
            while True:
                chunk = fin.read(buf_size)
                if not chunk:
                    break
                fout.write(chunk)
        log.info("[解密] 成功  %s -> %s", src_path, dst_path)
        return True
    except Exception as e:
        log.error("[解密] 失败  %s: %s", src_path, e)
        print(f"  [解密失败] {src_path}: {e}")
        return False


# ============================================================
# FFmpeg 合并
# ============================================================

def _build_metadata_args(metadata: dict[str, str]) -> list[str]:
    """构建 ffmpeg -metadata 参数列表。"""
    args = []
    for key, value in metadata.items():
        if value:
            args.extend(["-metadata", f"{key}={value}"])
    return args


def _run_ffmpeg(cmd: list[str]) -> tuple[bool, str]:
    """执行 ffmpeg 命令，返回 (成功, 错误信息)。"""
    log.debug("[FFmpeg] 执行命令: %s", " ".join(cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            log.info("[FFmpeg] 成功  %s", cmd[-1])
            return (True, "")
        err = r.stderr[-500:] if r.stderr else f"returncode={r.returncode}"
        log.error("[FFmpeg] 失败  %s: %s", cmd[-1], err)
        return (False, err)
    except subprocess.TimeoutExpired:
        log.error("[FFmpeg] 超时(600s)  %s", cmd[-1])
        return (False, "ffmpeg 超时(600s)")
    except Exception as e:
        log.error("[FFmpeg] 异常  %s: %s", cmd[-1], e)
        return (False, str(e))


def merge_audio_video(ffmpeg_bin: str, audio: str, video: str, output: str,
                      cover: str = "", metadata: dict[str, str] | None = None) -> tuple[bool, str]:
    """用 ffmpeg 合并音频和视频流为 MP4，同时嵌入封面缩略图和标准元数据。
    使用 -c copy 直接复制编码，不重新编码（速度快、无质量损失）。
    如果封面嵌入失败，自动回退到无封面合并。"""
    meta_args = _build_metadata_args(metadata or {})

    if cover and os.path.isfile(cover):
        # 音频(0) + 视频(1) + 封面(2)
        cmd = [ffmpeg_bin, "-y", "-i", audio, "-i", video, "-i", cover,
               "-map", "0:a", "-map", "1:v", "-map", "2:v",
               "-c:a", "copy", "-c:v:0", "copy", "-c:v:1", "mjpeg",
               "-disposition:v:1", "attached_pic",
               *meta_args, output]
        ok, err = _run_ffmpeg(cmd)
        if ok:
            return (True, "")
        # 封面嵌入失败时回退到无封面合并
        print(f"  [提示] 封面嵌入失败，回退到无封面合并")

    cmd = [ffmpeg_bin, "-y", "-i", audio, "-i", video, "-c", "copy",
           *meta_args, output]
    return _run_ffmpeg(cmd)


def merge_blv_videos(ffmpeg_bin: str, blv_paths: list[str], output: str,
                     cover: str = "", metadata: dict[str, str] | None = None) -> tuple[bool, str]:
    """用 ffmpeg concat 协议拼接多段 BLV 视频为 MP4。
    BLV 是旧版 Android 缓存格式，每段是同一视频的连续片段，
    按文件名中的数字排序后拼接。"""
    def sort_key(p):
        m = re.search(r'(\d+)\.blv$', p)
        return int(m.group(1)) if m else 0

    blv_paths = sorted(blv_paths, key=sort_key)
    concat = "|".join(blv_paths)
    meta_args = _build_metadata_args(metadata or {})

    if cover and os.path.isfile(cover):
        cmd = [ffmpeg_bin, "-y", "-i", f"concat:{concat}", "-i", cover,
               "-map", "0", "-map", "1:v",
               "-c", "copy", "-c:v:1", "mjpeg",
               "-disposition:v:1", "attached_pic",
               *meta_args, output]
        ok, err = _run_ffmpeg(cmd)
        if ok:
            return (True, "")
        print(f"  [提示] 封面嵌入失败，回退到无封面合并")

    cmd = [ffmpeg_bin, "-y", "-i", f"concat:{concat}", "-c", "copy",
           *meta_args, output]
    return _run_ffmpeg(cmd)


# ============================================================
# 进度显示
# ============================================================

class ProgressDisplay:
    """终端实时进度显示。"""

    def __init__(self, tasks: list[MergeTask]):
        self.tasks = tasks
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_lines = 0
        # Windows 启用 ANSI 转义
        if sys.platform == "win32":
            os.system("")

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._draw()  # 最终绘制一次

    def _loop(self):
        while not self._stop.is_set():
            self._draw()
            time.sleep(0.5)

    def _safe_write(self, text: str):
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            sys.stdout.write(text.encode("utf-8", errors="replace").decode(
                sys.stdout.encoding or "utf-8", errors="replace"))

    def _draw(self):
        with self._lock:
            # 清除上次输出
            if self._last_lines > 0:
                self._safe_write(f"\033[{self._last_lines}A\033[J")

            total = len(self.tasks)
            done = sum(1 for t in self.tasks if t.status == "completed")
            skipped = sum(1 for t in self.tasks if t.status == "skipped")
            failed = sum(1 for t in self.tasks if t.status == "failed")
            running = sum(1 for t in self.tasks if t.status == "running")
            finished = done + skipped + failed
            pending = total - finished - running

            # 进度条
            pct = finished / total if total else 0
            bar_len = 30
            filled = int(bar_len * pct)
            bar = "#" * filled + "-" * (bar_len - filled)

            lines = []
            status_parts = f"运行:{running}  完成:{done}  失败:{failed}  等待:{pending}"
            if skipped:
                status_parts += f"  跳过:{skipped}"
            lines.append(f"  [{bar}] {finished}/{total}  {status_parts}")
            lines.append("")

            # 每个任务的状态
            try:
                term_h = os.get_terminal_size().lines - 4
            except OSError:
                term_h = 20
            max_show = max(term_h, 5)

            for i, t in enumerate(self.tasks):
                if i >= max_show:
                    lines.append(f"  ... 还有 {total - max_show} 项")
                    break
                icon = {
                    "pending": "[等待]",
                    "running": "[合并]",
                    "completed": "[完成]",
                    "skipped": "[跳过]",
                    "failed": "[失败]",
                }.get(t.status, "[?]  ")
                title = t.item.title or "(未知)"
                if len(title) > 50:
                    title = title[:47] + "..."
                line = f"  {icon} {title}"
                if t.status == "failed" and t.error:
                    err_short = t.error[:60].replace('\n', ' ')
                    line += f"  ({err_short})"
                lines.append(line)

            output = "\n".join(lines) + "\n"
            self._safe_write(output)
            sys.stdout.flush()
            self._last_lines = len(lines)


# ============================================================
# 任务引擎
# ============================================================

class TaskEngine:
    """多线程合并任务引擎。负责调度合并任务、管理后处理（弹幕导出、文件移动/删除）。"""
    def __init__(
        self,
        ffmpeg_bin: str,
        output_dir: str,
        max_workers: int = 2,
        export_danmaku: bool = False,
        move_completed: bool = False,
        completed_dir: str = "",
        single_output: bool = False,
        duplicate_mode: str = "copy",
        delete_source: bool = False,
        export_cover: bool = False,
        export_info: bool = False,
    ):
        self.ffmpeg_bin = ffmpeg_bin
        self.output_dir = output_dir
        self.max_workers = max_workers
        self.export_danmaku = export_danmaku
        self.move_completed = move_completed
        self.completed_dir = completed_dir
        self.single_output = single_output
        self.duplicate_mode = duplicate_mode  # "skip" / "overwrite" / "copy"
        self.delete_source = delete_source
        self.export_cover = export_cover
        self.export_info = export_info

    def build_tasks(self, groups: list[CacheGroup], platform: str) -> list[MergeTask]:
        tasks = []
        for group in groups:
            for item in group.items:
                tasks.append(MergeTask(
                    item=item,
                    group_title=group.title,
                    platform=platform,
                ))
        return tasks

    def run(self, tasks: list[MergeTask]):
        display = ProgressDisplay(tasks)
        display.start()

        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {pool.submit(self._process, t): t for t in tasks}
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        task.status = "failed"
                        task.error = str(e)
        finally:
            display.stop()

    def _process(self, task: MergeTask):
        """处理单个合并任务的完整流程：
        重复检测 → 构建元数据 → 解密(Win/Mac) → ffmpeg合并 → 导出sidecar → 移动/删除源"""
        task.status = "running"
        item = task.item
        log.info("[任务开始] %s (平台: %s)", item.title or "(未知)", task.platform)

        title = sanitize_filename(item.title) if item.title else str(int(time.time() * 1000))
        group_title = sanitize_filename(task.group_title) if task.group_title else ""

        # 输出目录
        if self.single_output or not group_title:
            out_dir = self.output_dir
        else:
            out_dir = os.path.join(self.output_dir, group_title)
        os.makedirs(out_dir, exist_ok=True)

        # 输出路径 + 重复处理
        raw_output_path = os.path.join(out_dir, f"{title}.mp4")
        if os.path.isfile(raw_output_path):
            if self.duplicate_mode == "skip":
                task.status = "skipped"
                task.output_path = raw_output_path
                log.info("[跳过] 文件已存在  %s", raw_output_path)
                return
            elif self.duplicate_mode == "overwrite":
                output_path = raw_output_path
            else:  # "copy"
                output_path = get_available_path(raw_output_path)
        else:
            output_path = raw_output_path
        task.output_path = output_path

        # 构建元数据
        metadata = {}
        if item.title:
            metadata["title"] = item.title
        if item.owner_name:
            metadata["artist"] = item.owner_name
        if task.group_title:
            metadata["album"] = task.group_title
        if item.p is not None:
            metadata["track"] = str(item.p)
        bvav = item.bv_id or (f"av{item.av_id}" if item.av_id else "")
        if bvav:
            metadata["comment"] = bvav

        # 封面路径
        cover = item.cover_path if item.cover_path and os.path.isfile(item.cover_path) else ""

        temp_files = []
        try:
            success = False
            err_msg = ""

            if item.audio_path and item.video_path:
                if task.platform in ("windows", "mac"):
                    # 解密
                    temp_audio = item.audio_path + ".hlb_temp.mp3"
                    temp_video = item.video_path + ".hlb_temp.mp4"
                    temp_files.extend([temp_audio, temp_video])

                    if not decrypt_pc_m4s(item.audio_path, temp_audio):
                        raise RuntimeError(f"音频解密失败: {item.audio_path}")
                    if not decrypt_pc_m4s(item.video_path, temp_video):
                        raise RuntimeError(f"视频解密失败: {item.video_path}")

                    success, err_msg = merge_audio_video(
                        self.ffmpeg_bin, temp_audio, temp_video, output_path,
                        cover=cover, metadata=metadata)
                else:
                    # Android: 直接合并
                    success, err_msg = merge_audio_video(
                        self.ffmpeg_bin, item.audio_path, item.video_path, output_path,
                        cover=cover, metadata=metadata)

            elif item.blv_paths:
                success, err_msg = merge_blv_videos(
                    self.ffmpeg_bin, item.blv_paths, output_path,
                    cover=cover, metadata=metadata)
            else:
                raise RuntimeError("缺少输入源")

            if not success:
                raise RuntimeError(err_msg or "合并失败")

            # 导出封面
            if self.export_cover and cover:
                cover_dest = change_ext(output_path, "jpg")
                shutil.copy2(cover, cover_dest)
                log.info("[导出] 封面  %s", cover_dest)

            # 导出原始元数据
            if self.export_info and item.json_path and os.path.isfile(item.json_path):
                info_dest = change_ext(output_path, "info.json")
                shutil.copy2(item.json_path, info_dest)
                log.info("[导出] 元数据  %s", info_dest)

            # 导出弹幕
            if self.export_danmaku and item.danmaku_path and os.path.isfile(item.danmaku_path):
                danmaku_dest = change_ext(output_path, "xml")
                shutil.copy2(item.danmaku_path, danmaku_dest)
                log.info("[导出] 弹幕  %s", danmaku_dest)

            # 移动到完成目录（MP4 + 所有 sidecar 文件）
            if self.move_completed and self.completed_dir:
                self._move_to_completed(output_path, group_title)
                for ext in ("xml", "jpg", "info.json"):
                    sidecar = change_ext(output_path, ext)
                    if os.path.isfile(sidecar):
                        self._move_to_completed(sidecar, group_title)

            # 删除源缓存文件
            if self.delete_source:
                self._delete_source_files(item)

            task.status = "completed"
            log.info("[转换完成] %s -> %s", item.title or "(未知)", output_path)

        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            log.error("[转换失败] %s: %s", item.title or "(未知)", e)
            # 清理失败的输出文件
            if os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                    log.info("[删除] 失败产物  %s", output_path)
                except OSError:
                    pass
        finally:
            # 清理临时文件
            for tf in temp_files:
                try:
                    if os.path.isfile(tf):
                        os.remove(tf)
                        log.debug("[删除] 临时文件  %s", tf)
                except OSError:
                    pass

    def _move_to_completed(self, filepath: str, group_title: str):
        try:
            if group_title:
                dest_dir = os.path.join(self.completed_dir, group_title)
            else:
                dest_dir = self.completed_dir
            os.makedirs(dest_dir, exist_ok=True)

            filename = os.path.basename(filepath)
            dest_path = get_available_path(os.path.join(dest_dir, filename))
            shutil.move(filepath, dest_path)
            log.info("[移动] %s -> %s", filepath, dest_path)
        except Exception as e:
            log.error("[移动] 失败  %s: %s", filepath, e)
            print(f"  [移动失败] {filepath}: {e}")

    def _delete_source_files(self, item: CacheItem):
        """删除源缓存的整个分集文件夹（如 c_80020353/），包含所有画质、元数据、弹幕。
        删除后检查父目录（合集目录），如果已空也一并删除。使用 shutil.rmtree，不可恢复。"""
        source_dir = item.path
        if not source_dir or not os.path.isdir(source_dir):
            return
        try:
            shutil.rmtree(source_dir)
            log.info("[删除] 源文件夹  %s", source_dir)
        except Exception as e:
            log.error("[删除] 源文件夹失败  %s: %s", source_dir, e)
            print(f"  [删除源文件夹失败] {source_dir}: {e}")
            return

        # 检查父目录（合集目录）是否为空，空则删除
        parent_dir = item.parent_path
        if parent_dir and os.path.isdir(parent_dir):
            try:
                if not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
                    log.info("[删除] 空父目录  %s", parent_dir)
            except Exception:
                pass


# ============================================================
# CLI 入口
# ============================================================

def find_ffmpeg(script_dir: str, user_path: str | None) -> str:
    """查找 ffmpeg 可执行文件。"""
    if user_path:
        if os.path.isfile(user_path):
            return user_path
        raise FileNotFoundError(f"指定的 ffmpeg 不存在: {user_path}")

    # 在脚本同目录查找
    candidates = [
        os.path.join(script_dir, "ffmpeg.exe"),
        os.path.join(script_dir, "ffmpeg"),
        # 项目自带的 ffmpeg
        os.path.join(script_dir, "hlbmerge_flutter", "ffmpeg_hl", "windows", "third_party", "ffmpeg", "bin", "ffmpeg.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    # 尝试系统 PATH
    ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    result = shutil.which(ffmpeg_name)
    if result:
        return result

    raise FileNotFoundError(
        "找不到 ffmpeg。请用 --ffmpeg 指定路径，或将 ffmpeg 放在脚本同目录下。"
    )


def _detect_duplicates(groups: list[CacheGroup], output_dir: str, single_output: bool) -> list[str]:
    """检测输出目录中已存在的同名文件，返回重复的文件名列表。"""
    duplicates = []
    for group in groups:
        group_title = sanitize_filename(group.title) if group.title else ""
        for item in group.items:
            title = sanitize_filename(item.title) if item.title else ""
            if not title:
                continue
            if single_output or not group_title:
                out_dir = output_dir
            else:
                out_dir = os.path.join(output_dir, group_title)
            output_path = os.path.join(out_dir, f"{title}.mp4")
            if os.path.isfile(output_path):
                display_name = f"{group_title}/{title}.mp4" if group_title and not single_output else f"{title}.mp4"
                duplicates.append(display_name)
    return duplicates


def main():
    parser = argparse.ArgumentParser(
        description="Bilibili 缓存视频合并工具 — 将 B 站缓存导出为 MP4",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-i", "--input", required=True, help="B站缓存目录路径")
    parser.add_argument("-o", "--output", required=True, help="MP4 输出目录")
    parser.add_argument("-p", "--platform", choices=["android", "windows", "mac"],
                        default=None, help="缓存格式 (默认自动检测)")
    parser.add_argument("-f", "--ffmpeg", default=None, help="ffmpeg 可执行文件路径")
    parser.add_argument("-j", "--jobs", type=int, default=2, help="并发线程数 (默认 2)")
    parser.add_argument("--danmaku", action="store_true", help="同时导出弹幕 XML 文件")
    parser.add_argument("--move-completed", metavar="DIR", default=None,
                        help="转换成功后移动到指定目录")
    parser.add_argument("--single-output", action="store_true",
                        help="所有文件输出到同一目录 (不按合集分子文件夹)")
    parser.add_argument("--add-index", action="store_true",
                        help="文件名前添加分P序号")
    parser.add_argument("--delete-source", action="store_true",
                        help="转换成功后删除源缓存文件 (与 --move-completed 互斥)")
    parser.add_argument("--export-cover", action="store_true",
                        help="导出封面图片为独立 jpg 文件")
    parser.add_argument("--export-info", action="store_true",
                        help="导出原始元数据为 .info.json 文件")

    args = parser.parse_args()

    # 互斥校验
    if args.move_completed and args.delete_source:
        print("错误: --move-completed 和 --delete-source 不能同时使用。")
        print("  --move-completed: 转换后将输出文件移动到完成目录")
        print("  --delete-source:  转换后删除源缓存文件")
        sys.exit(1)

    # 验证输入目录
    if not os.path.isdir(args.input):
        print(f"错误: 输入目录不存在: {args.input}")
        sys.exit(1)

    # 查找 ffmpeg
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        ffmpeg_bin = find_ffmpeg(script_dir, args.ffmpeg)
    except FileNotFoundError as e:
        print(f"错误: {e}")
        sys.exit(1)
    print(f"ffmpeg: {ffmpeg_bin}")

    # 检测平台
    platform = args.platform
    if not platform:
        platform = auto_detect_platform(args.input)
        print(f"自动检测缓存格式: {platform}")
    else:
        print(f"缓存格式: {platform}")

    # 扫描
    print(f"扫描目录: {args.input}")
    if platform == "android":
        groups = scan_android_cache(args.input, add_index=args.add_index)
    else:
        groups = scan_windows_cache(args.input, add_index=args.add_index)

    if not groups:
        print("未找到任何缓存数据。")
        sys.exit(0)

    total_items = sum(len(g.items) for g in groups)
    print(f"找到 {len(groups)} 个合集, 共 {total_items} 个视频\n")

    # 列出合集
    for g in groups:
        print(f"  [{len(g.items):>3} 集] {g.title or g.group_id}")

    print()

    # 确认
    try:
        answer = input(f"是否开始合并 {total_items} 个视频? (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消。")
            sys.exit(0)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(0)

    print()

    # 创建输出目录
    os.makedirs(args.output, exist_ok=True)

    # 检测重复文件
    duplicate_mode = "copy"  # 默认生成副本
    duplicates = _detect_duplicates(groups, args.output, args.single_output)
    if duplicates:
        print(f"检测到 {len(duplicates)} 个视频在输出目录中已存在:\n")
        for d in duplicates[:20]:
            print(f"  - {d}")
        if len(duplicates) > 20:
            print(f"  ... 还有 {len(duplicates) - 20} 个")
        print()
        print("请选择处理方式:")
        print("  1. 跳过重复 — 已存在的不再转换")
        print("  2. 覆盖重复 — 重新转换并覆盖已有文件")
        print("  3. 生成副本 — 已存在时自动添加编号后缀 (默认)")
        print()
        try:
            choice = input("请输入选项 (1/2/3, 默认3): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            sys.exit(0)

        if choice == "1":
            duplicate_mode = "skip"
            print(f"-> 将跳过 {len(duplicates)} 个已存在的视频\n")
        elif choice == "2":
            duplicate_mode = "overwrite"
            print(f"-> 将覆盖 {len(duplicates)} 个已有文件\n")
        else:
            duplicate_mode = "copy"
            print("-> 重复文件将生成带编号的副本\n")

    # 构建任务
    engine = TaskEngine(
        ffmpeg_bin=ffmpeg_bin,
        output_dir=args.output,
        max_workers=args.jobs,
        export_danmaku=args.danmaku,
        move_completed=bool(args.move_completed),
        completed_dir=args.move_completed or "",
        single_output=args.single_output,
        duplicate_mode=duplicate_mode,
        delete_source=args.delete_source,
        export_cover=args.export_cover,
        export_info=args.export_info,
    )
    tasks = engine.build_tasks(groups, platform)

    # 执行
    log.info("开始合并任务: 共 %d 个, 线程数 %d", len(tasks), args.jobs)
    print(f"开始合并 (线程数: {args.jobs})...\n")
    start_time = time.time()
    engine.run(tasks)
    elapsed = time.time() - start_time

    # 汇总
    done = [t for t in tasks if t.status == "completed"]
    skipped = [t for t in tasks if t.status == "skipped"]
    failed = [t for t in tasks if t.status == "failed"]

    summary_msg = f"成功: {len(done)}  失败: {len(failed)}"
    if skipped:
        summary_msg += f"  跳过: {len(skipped)}"
    summary_msg += f"  总计: {len(tasks)}  耗时: {elapsed:.1f}s"
    log.info("===== 任务汇总 =====  %s", summary_msg)

    if failed:
        for t in failed:
            log.error("[失败详情] %s: %s", t.item.title, t.error)

    print(f"\n{'=' * 50}")
    print(f"完成! 耗时 {elapsed:.1f}s")
    summary = f"成功: {len(done)}  失败: {len(failed)}"
    if skipped:
        summary += f"  跳过: {len(skipped)}"
    summary += f"  总计: {len(tasks)}"
    print(summary)

    if failed:
        print(f"\n失败列表:")
        for t in failed:
            print(f"  [失败] {t.item.title}: {t.error}")

    if args.move_completed and done:
        print(f"\n已移动 {len(done)} 个文件到: {args.move_completed}")

    if args.delete_source and done:
        print(f"\n已删除 {len(done)} 个视频的源缓存文件夹")

    print()


if __name__ == "__main__":
    main()
