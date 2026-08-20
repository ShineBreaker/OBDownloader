#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ranked 地图下载器增强版
================================
基于 sayobot 镜像 API 的 osu! 谱面批量下载器。

特性：
- 按日期范围抓取谱面数据（自动分页，不再只取前 500 条）
- 灵活筛选：模式、星级、BPM、时长、AR/OD/CS/HP、作者/曲师/标题/标签、审核状态、有无视频/故事板
- 自动跳过已下载的谱面
- 多线程并发下载
- 图形界面（Tkinter，纯标准库）

运行（GUI）：
    python RankedDownloader.py

运行（Linux/无 GUI CLI）：
    python RankedDownloader.py --source sayobot --start-date 2025-01-01 --mode mania --keys 4 --list
    python RankedDownloader.py --source official --client-id ... --client-secret ... --mode mania --keys 4 --download --songs /path/to/Songs

打包为 Windows exe：
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "Ranked地图下载器增强版" RankedDownloader.py
"""

import argparse
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError:
    tk = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

API_BASE = "https://api.sayobot.cn/ppy/get_beatmaps"
DL_BASE = "http://dl.sayobot.cn/beatmaps"
REFERER = "https://osu.sayobot.cn/SayoMapsDownloader.exe"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RankedDownloader/2.0"

MAX_PAGE_SIZE = 500          # sayobot 单次最多返回 500 条
MAX_PAGES = 100000           # 安全上限
MAX_RETRIES = 3              # 单页请求失败重试次数
DEFAULT_START = "2025-01-01"
EARLIEST_DATE = "2007-10-06"   # sayobot 镜像站最早收录谱面日期

MODE_NAMES = {
    "0": "osu!",
    "1": "太鼓",
    "2": "接水果",
    "3": "下落式",
}

APPROVED_LABELS = {
    "1": "Ranked",
    "2": "Approved",
    "3": "Qualified",
    "4": "Loved",
    "0": "Pending",
    "-1": "WIP",
    "-2": "Graveyard",
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def safe_filename(name: str, max_len: int = 120) -> str:
    """去掉 Windows 文件名非法字符，并限制长度。"""
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip()
    name = name.strip(". ")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "unknown"


def parse_float(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse_int(s, default=None):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def is_downloaded(beatmapset_id: int, songs_dir: str) -> bool:
    """通过 Songs 目录里是否已有 'setid ' 前缀的文件/文件夹判断是否已下载。"""
    if not songs_dir or not os.path.isdir(songs_dir):
        return False
    prefix = f"{beatmapset_id} "
    try:
        names = os.listdir(songs_dir)
    except OSError:
        return False
    for name in names:
        if name.startswith(prefix):
            return True
    return False


def build_download_url(beatmapset_id: int, file_type: str) -> str:
    """构造 sayobot 下载链接。

    原版程序使用同一规则：
    - sid >= 10000: /beatmaps/{sid // 10000}/{sid % 10000:04d}/{type}
    - sid < 10000:  /beatmaps/0/{sid}/{type}
    """
    if beatmapset_id >= 10000:
        return f"{DL_BASE}/{beatmapset_id // 10000}/{beatmapset_id % 10000:04d}/{file_type}"
    return f"{DL_BASE}/0/{beatmapset_id}/{file_type}"


# ---------------------------------------------------------------------------
# 官方 osu! API v2 源
# ---------------------------------------------------------------------------

OSU_OAUTH_URL = "https://osu.ppy.sh/oauth/token"
OSU_API_BASE = "https://osu.ppy.sh/api/v2"
OSU_DL_BASE = "https://osu.ppy.sh/beatmapsets"

STATUS_TO_CODE = {
    "ranked": "1",
    "approved": "2",
    "qualified": "3",
    "loved": "4",
    "pending": "0",
    "wip": "-1",
    "graveyard": "-2",
}
CODE_TO_STATUS = {v: k for k, v in STATUS_TO_CODE.items()}


def official_get_token(client_id: str, client_secret: str) -> str:
    """获取 osu! API v2 client_credentials token。"""
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "public",
    }).encode("utf-8")
    req = urllib.request.Request(OSU_OAUTH_URL, data=data, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload.get("access_token")
    if not token:
        raise RuntimeError("官方 OAuth 响应中没有 access_token")
    return token


def official_download_url(beatmapset_id: int, file_type: str) -> str:
    """构造官方下载链接。"""
    query = "?noVideo=1" if file_type == "novideo" else ""
    return f"{OSU_DL_BASE}/{beatmapset_id}/download{query}"


def official_status_code(status) -> str:
    """把官方 status 字符串转成 sayobot 的 approved 数字字符串。"""
    return STATUS_TO_CODE.get(str(status).lower(), str(status))


def official_mode_code(mode) -> str:
    """把官方 mode 转成 sayobot 的 0~3 字符串。"""
    if isinstance(mode, int):
        return str(mode)
    return {
        "osu": "0",
        "taiko": "1",
        "catch": "2",
        "mania": "3",
    }.get(str(mode).lower(), str(mode))


def official_set_to_items(s: dict):
    """把官方 API v2 的一个 beatmapset 转成 sayobot 风格的扁平难度列表。"""
    sid = s.get("id")
    if sid is None:
        return []
    artist = s.get("artist", "")
    artist_unicode = s.get("artist_unicode", artist)
    title = s.get("title", "")
    title_unicode = s.get("title_unicode", title)
    creator = s.get("creator", "")
    approved = official_status_code(s.get("status", ""))
    approved_date = s.get("approved_date", "") or ""
    tags = s.get("tags", "")
    bpm = s.get("bpm")
    video = 1 if str(s.get("video", "0")).lower() in ("1", "true") else 0
    storyboard = 1 if str(s.get("storyboard", "0")).lower() in ("1", "true") else 0
    playcount = s.get("play_count") or s.get("playcount") or 0
    favourite_count = s.get("favourite_count") or 0

    items = []
    for b in s.get("beatmaps", []):
        mode_val = b.get("mode")
        if mode_val is None:
            mode_val = b.get("mode_int", "")
        items.append({
            "beatmapset_id": str(sid),
            "beatmap_id": b.get("id"),
            "approved": approved,
            "approved_date": approved_date,
            "mode": official_mode_code(mode_val),
            "difficultyrating": b.get("difficulty_rating"),
            "diff_size": b.get("cs"),
            "diff_approach": b.get("ar"),
            "diff_overall": b.get("accuracy"),
            "diff_drain": b.get("drain"),
            "bpm": b.get("bpm") or bpm,
            "total_length": b.get("total_length"),
            "version": b.get("version", ""),
            "artist": artist,
            "artist_unicode": artist_unicode,
            "title": title,
            "title_unicode": title_unicode,
            "creator": creator,
            "tags": tags,
            "video": str(video),
            "storyboard": str(storyboard),
            "download_unavailable": "0",
            "audio_unavailable": "0",
            "playcount": playcount,
            "favourite_count": favourite_count,
        })
    return items


def fetch_official_beatmaps(filters, client_id, client_secret,
                            progress_cb=None, cancel_event=None):
    """从官方 osu! API v2 搜索谱面，并转换成旧版扁平格式。"""
    if cancel_event is None:
        cancel_event = threading.Event()
    token = official_get_token(client_id, client_secret)

    params = {"limit": 50}
    if filters.mode:
        params["m"] = filters.mode
    if len(filters.approved) == 1:
        code = next(iter(filters.approved))
        status = CODE_TO_STATUS.get(code, code)
        if status in STATUS_TO_CODE:
            params["s"] = status
    q_parts = []
    if filters.title:
        q_parts.append(filters.title)
    if filters.artist:
        q_parts.append(filters.artist)
    if filters.creator:
        q_parts.append(filters.creator)
    if filters.tags:
        q_parts.append(filters.tags)
    if q_parts:
        params["q"] = " ".join(q_parts)

    items = []
    offset = 0
    last_first_sid = None
    while True:
        if cancel_event.is_set():
            raise RuntimeError("已取消")
        p = dict(params)
        p["offset"] = offset
        url = OSU_API_BASE + "/beatmapsets/search?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "x-api-version": "20220707",
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        sets = data.get("beatmapsets", []) if isinstance(data, dict) else []
        for s in sets:
            items.extend(official_set_to_items(s))
        if progress_cb:
            progress_cb(len(items), "")

        if not sets:
            break
        if len(sets) < params["limit"]:
            break

        first_sid = sets[0].get("id")
        if offset > 0 and first_sid == last_first_sid:
            # offset 可能不受支持，避免死循环
            break
        last_first_sid = first_sid

        offset += len(sets)
        if len(items) > 20000:  # 安全上限
            break
    return items


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------


class FetchError(RuntimeError):
    """抓取中断时携带已获取的部分数据，便于断点续传。"""

    def __init__(self, message, partial_items=None, last_since=None):
        super().__init__(message)
        self.partial_items = partial_items or []
        self.last_since = last_since


def fetch_beatmaps(start_date: str, end_date: str = "", mode: str = "",
                   progress_cb=None, cancel_event: threading.Event = None):
    """
    从 sayobot 获取谱面难度数据，自动按时间分页。

    start_date: YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS
    end_date:   可选，YYYY-MM-DD，用于提前停止
    mode:       "" 表示全部，或 "0"~"3"
    progress_cb: 回调(已获取条数, 当前日期字符串)
    cancel_event: 取消事件
    返回: 原始 beatmap 难度列表
    """
    if cancel_event is None:
        cancel_event = threading.Event()

    since = start_date.strip()
    # 防止开始日期早于镜像站最早收录日期
    since_dt = _parse_date(since)
    earliest_dt = _parse_date(EARLIEST_DATE)
    if since_dt and earliest_dt and since_dt < earliest_dt:
        since = EARLIEST_DATE

    all_items = []
    last_since = None
    page = 0

    while not cancel_event.is_set():
        page += 1
        if page > MAX_PAGES:
            break

        params = {"since": since}
        if mode != "":
            params["m"] = mode

        url = API_BASE + "?" + urllib.parse.urlencode(params)
        data = None
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            if cancel_event.is_set():
                raise RuntimeError("已取消")
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT,
                    "Referer": REFERER,
                })
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                break
            except Exception as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    raise FetchError(
                        f"获取谱面数据失败: {last_exc}（已重试 {MAX_RETRIES} 次）",
                        partial_items=all_items,
                        last_since=since,
                    ) from last_exc
                time.sleep(2 * attempt)

        if not isinstance(data, list) or not data:
            break

        all_items.extend(data)

        # 当前批次的最后时间（数据按 approved_date 升序返回）
        last_date = max(
            (str(item.get("approved_date", "")) for item in data if item.get("approved_date")),
            default=""
        )

        if progress_cb:
            progress_cb(len(all_items), last_date or since)

        # 如果已经超过结束日期，可以提前停止（假设按时间升序）
        if end_date:
            end_ts = _parse_date(end_date)
            if end_ts and last_date:
                last_ts = _parse_date(last_date)
                if last_ts and last_ts > end_ts:
                    break

        # 不足一页说明没有更多了
        if len(data) < MAX_PAGE_SIZE:
            break

        # 防止同一时间点重复请求导致死循环
        if last_date and last_date == last_since:
            break
        last_since = last_date

        if not last_date:
            break

        since = last_date
        time.sleep(0.1)

    return all_items


def _parse_date(s: str):
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# 筛选与分组
# ---------------------------------------------------------------------------


class Filters:
    def __init__(self):
        self.mode = ""                 # "" 全部, 0~3
        self.keys = ""                 # mania 键数，如 "4"；"" 表示不限
        self.pure_keys = False         # True 时只保留整个 set 都是该键数的谱面
        self.approved = set()          # {"1","3","4"...}
        self.star_min = None
        self.star_max = None
        self.bpm_min = None
        self.bpm_max = None
        self.length_min = None         # 秒
        self.length_max = None
        self.ar_min = None
        self.ar_max = None
        self.od_min = None
        self.od_max = None
        self.cs_min = None
        self.cs_max = None
        self.hp_min = None
        self.hp_max = None
        self.creator = ""
        self.artist = ""
        self.title = ""
        self.tags = ""
        self.video = ""                # "" 不限, "0" 仅无视频, "1" 仅含视频
        self.storyboard = ""           # "" 不限, "0" 无, "1" 有
        self.skip_unavailable = True
        self.end_date = ""

    def match(self, b) -> bool:
        # 模式
        if self.mode != "" and str(b.get("mode", "")) != str(self.mode):
            return False

        # mania 键数（以 CS 为准；只有 mode=3 时有意义）
        if self.keys:
            if str(b.get("mode", "")) != "3":
                return False
            if str(b.get("diff_size", "")) != self.keys:
                return False

        # 审核状态
        if self.approved and str(b.get("approved", "")) not in self.approved:
            return False

        # 结束日期（已下载的数据可能包含更晚的，这里再过滤一次）
        if self.end_date:
            end_ts = _parse_date(self.end_date)
            ad = _parse_date(str(b.get("approved_date", "")))
            if end_ts and ad and ad > end_ts:
                return False

        # 星数
        star = parse_float(b.get("difficultyrating"))
        if star is not None:
            if self.star_min is not None and star < self.star_min:
                return False
            if self.star_max is not None and star > self.star_max:
                return False

        # BPM
        bpm = parse_float(b.get("bpm"))
        if bpm is not None:
            if self.bpm_min is not None and bpm < self.bpm_min:
                return False
            if self.bpm_max is not None and bpm > self.bpm_max:
                return False

        # 长度
        length = parse_int(b.get("total_length"))
        if length is not None:
            if self.length_min is not None and length < self.length_min:
                return False
            if self.length_max is not None and length > self.length_max:
                return False

        # AR / OD / CS / HP
        for key, lo, hi in (
            ("diff_approach", self.ar_min, self.ar_max),
            ("diff_overall", self.od_min, self.od_max),
            ("diff_size", self.cs_min, self.cs_max),
            ("diff_drain", self.hp_min, self.hp_max),
        ):
            val = parse_float(b.get(key))
            if val is not None:
                if lo is not None and val < lo:
                    return False
                if hi is not None and val > hi:
                    return False

        # 文本过滤
        if self.creator:
            if self.creator.lower() not in str(b.get("creator", "")).lower():
                return False
        if self.artist:
            haystack = "{} {}".format(b.get("artist", ""), b.get("artist_unicode", "")).lower()
            if self.artist.lower() not in haystack:
                return False
        if self.title:
            haystack = "{} {}".format(b.get("title", ""), b.get("title_unicode", "")).lower()
            if self.title.lower() not in haystack:
                return False
        if self.tags:
            if self.tags.lower() not in str(b.get("tags", "")).lower():
                return False

        # 视频 / 故事板
        if self.video and str(b.get("video", "")) != self.video:
            return False
        if self.storyboard and str(b.get("storyboard", "")) != self.storyboard:
            return False

        # 跳过不可下载
        if self.skip_unavailable:
            if str(b.get("download_unavailable", "0")) != "0":
                return False
            if str(b.get("audio_unavailable", "0")) != "0":
                return False

        return True


def filter_and_group(beatmaps, filters: Filters):
    """
    先按 Filters 过滤难度，再按 beatmapset_id 分组。
    返回谱面集合列表，每个集合包含匹配难度的汇总信息。
    """
    sets = {}
    all_set_keys = {}

    for b in beatmaps:
        sid_raw = str(b.get("beatmapset_id", "")).strip()
        if not sid_raw:
            continue
        sid = int(sid_raw)
        all_set_keys.setdefault(sid, set()).add((str(b.get("mode", "")), str(b.get("diff_size", ""))))

        if not filters.match(b):
            continue

        if sid not in sets:
            sets[sid] = {
                "set_id": sid,
                "artist": b.get("artist", ""),
                "title": b.get("title", ""),
                "creator": b.get("creator", ""),
                "approved": b.get("approved", ""),
                "video": b.get("video", ""),
                "modes": set(),
                "stars": [],
                "bpms": [],
                "lengths": [],
                "matched": 0,
                "total": 0,
                "versions": [],
                "playcount": parse_int(b.get("playcount"), 0),
                "favourite_count": parse_int(b.get("favourite_count"), 0),
            }

        info = sets[sid]
        info["modes"].add(str(b.get("mode", "")))
        star = parse_float(b.get("difficultyrating"))
        bpm = parse_float(b.get("bpm"))
        length = parse_int(b.get("total_length"))
        if star is not None:
            info["stars"].append(star)
        if bpm is not None:
            info["bpms"].append(bpm)
        if length is not None:
            info["lengths"].append(length)
        info["matched"] += 1
        info["total"] += 1
        info["versions"].append(str(b.get("version", "")))
        info["playcount"] = max(info["playcount"], parse_int(b.get("playcount"), 0))
        info["favourite_count"] = max(info["favourite_count"], parse_int(b.get("favourite_count"), 0))

    result = []
    for info in sets.values():
        info["modes_text"] = "/".join(MODE_NAMES.get(m, m) for m in sorted(info["modes"]))
        info["stars_min"] = min(info["stars"]) if info["stars"] else None
        info["stars_max"] = max(info["stars"]) if info["stars"] else None
        info["bpm_min"] = min(info["bpms"]) if info["bpms"] else None
        info["bpm_max"] = max(info["bpms"]) if info["bpms"] else None
        info["length_min"] = min(info["lengths"]) if info["lengths"] else None
        info["length_max"] = max(info["lengths"]) if info["lengths"] else None
        result.append(info)

    # “仅纯该键数”：整个 set 只能包含指定 mania 键数，不能混其他键数/模式
    if filters.pure_keys and filters.keys:
        expected = {("3", filters.keys)}
        result = [info for info in result if all_set_keys.get(info["set_id"], set()) == expected]

    result.sort(key=lambda x: (-x["matched"], x["set_id"]))
    return result


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------


def download_one(set_info, songs_dir, file_type, source="sayobot", token="",
                 cancel_event=None, progress_cb=None):
    """下载一个谱面集合，返回 (set_id, ok, message)。"""
    sid = set_info["set_id"]
    if cancel_event and cancel_event.is_set():
        return sid, False, "已取消"

    if is_downloaded(sid, songs_dir):
        return sid, True, "已存在，跳过"

    attempts = []
    if source == "official":
        official_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/octet-stream",
        }
        if token:
            official_headers["Authorization"] = "Bearer " + token
        attempts.append((official_download_url(sid, file_type), official_headers))
        # 官方下载失败时自动回退到 sayobot
        attempts.append((build_download_url(sid, file_type), {
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
        }))
    else:
        attempts.append((build_download_url(sid, file_type), {
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
        }))

    filename = f"{sid} {safe_filename(set_info['artist'])} - {safe_filename(set_info['title'])}.osz"
    final_path = os.path.join(songs_dir, filename)
    part_path = final_path + ".part"
    last_exc = None

    for url, headers in attempts:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = parse_int(resp.headers.get("Content-Length"), 0)
                downloaded = 0
                last_report = 0
                if progress_cb:
                    progress_cb(sid, 0, total)
                with open(part_path, "wb") as f:
                    while True:
                        if cancel_event and cancel_event.is_set():
                            f.close()
                            try:
                                os.remove(part_path)
                            except OSError:
                                pass
                            return sid, False, "已取消"
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb and downloaded - last_report >= 1024 * 1024:
                            last_report = downloaded
                            progress_cb(sid, downloaded, total)

            if os.path.exists(final_path):
                os.remove(final_path)
            os.rename(part_path, final_path)
            return sid, True, f"完成 ({downloaded / 1024 / 1024:.1f} MB)"
        except Exception as exc:
            last_exc = exc
            try:
                if os.path.exists(part_path):
                    os.remove(part_path)
            except OSError:
                pass

    return sid, False, str(last_exc)


def download_many(set_infos, songs_dir, file_type, workers=3, source="sayobot",
                  token="", cancel_event=None, item_cb=None, progress_cb=None):
    """
    并发下载多个谱面。
    item_cb(sid, ok, message) 在每个任务结束时调用。
    progress_cb(sid, downloaded, total) 下载进度回调。
    """
    if cancel_event is None:
        cancel_event = threading.Event()

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(download_one, info, songs_dir, file_type,
                            source, token, cancel_event, progress_cb): info
            for info in set_infos
        }
        for future in as_completed(futures):
            if cancel_event.is_set():
                # 取消尚未开始/正在进行的任务
                for f in futures:
                    f.cancel()
                break
            try:
                sid, ok, msg = future.result()
            except Exception as exc:
                sid = futures[future]["set_id"]
                ok, msg = False, str(exc)
            if item_cb:
                item_cb(sid, ok, msg)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------


class RankedDownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("osu! Ranked 地图下载器增强版")
        root.geometry("1180x760")
        root.minsize(1000, 650)

        self.cancel_event = threading.Event()
        self.worker_queue = queue.Queue()
        self.searching = False
        self.downloading = False
        self.current_sets = []
        self.download_results = {}
        self.partial_beatmaps = []
        self.partial_since = None
        self.partial_mode = ""

        self._build_style()
        self._build_vars()
        self._build_widgets()
        self.root.after(100, self._poll_queue)

        # 尝试自动检测 osu! 路径
        self.auto_detect_osu()

    # ---------- 样式 ----------
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24)

    # ---------- 变量 ----------
    def _build_vars(self):
        self.var_songs = tk.StringVar()
        self.var_start = tk.StringVar(value=DEFAULT_START)
        self.var_end = tk.StringVar(value="")
        self.var_mode = tk.StringVar(value="全部")
        self.var_keys = tk.StringVar(value="全部")
        self.var_pure_keys = tk.BooleanVar(value=False)
        self.var_file_type = tk.StringVar(value="无视频")

        self.var_source = tk.StringVar(value="sayobot")
        self.var_client_id = tk.StringVar(value="")
        self.var_client_secret = tk.StringVar(value="")

        self.var_star_min = tk.StringVar(value="")
        self.var_star_max = tk.StringVar(value="")
        self.var_bpm_min = tk.StringVar(value="")
        self.var_bpm_max = tk.StringVar(value="")
        self.var_len_min = tk.StringVar(value="")
        self.var_len_max = tk.StringVar(value="")
        self.var_ar_min = tk.StringVar(value="")
        self.var_ar_max = tk.StringVar(value="")
        self.var_od_min = tk.StringVar(value="")
        self.var_od_max = tk.StringVar(value="")
        self.var_cs_min = tk.StringVar(value="")
        self.var_cs_max = tk.StringVar(value="")
        self.var_hp_min = tk.StringVar(value="")
        self.var_hp_max = tk.StringVar(value="")

        self.var_creator = tk.StringVar(value="")
        self.var_artist = tk.StringVar(value="")
        self.var_title = tk.StringVar(value="")
        self.var_tags = tk.StringVar(value="")

        self.var_video = tk.StringVar(value="不限")
        self.var_storyboard = tk.StringVar(value="不限")
        self.var_skip_existing = tk.BooleanVar(value=True)
        self.var_workers = tk.StringVar(value="3")

        self.var_approved = {}
        for key, label in APPROVED_LABELS.items():
            self.var_approved[key] = tk.BooleanVar(value=(key == "1"))

    # ---------- 控件 ----------
    def _build_widgets(self):
        pad = {"padx": 4, "pady": 2}

        # 顶部：osu 路径
        top = ttk.LabelFrame(self.root, text="osu! Songs 目录")
        top.pack(fill="x", padx=8, pady=4)
        ttk.Entry(top, textvariable=self.var_songs, width=70).pack(side="left", fill="x", expand=True, padx=4, pady=4)
        ttk.Button(top, text="浏览…", command=self.browse_songs).pack(side="left", padx=4)
        ttk.Button(top, text="自动检测", command=self.auto_detect_osu).pack(side="left", padx=4)
        ttk.Button(top, text="打开目录", command=self.open_songs_dir).pack(side="left", padx=4)

        # 数据源
        src = ttk.LabelFrame(self.root, text="数据源")
        src.pack(fill="x", padx=8, pady=4)
        ttk.Label(src, text="数据源").pack(side="left", padx=4)
        ttk.Combobox(src, textvariable=self.var_source,
                     values=["sayobot", "official"], width=12, state="readonly").pack(side="left", **pad)
        ttk.Label(src, text="Client ID").pack(side="left", padx=(10, 0))
        ttk.Entry(src, textvariable=self.var_client_id, width=24).pack(side="left", **pad)
        ttk.Label(src, text="Client Secret").pack(side="left")
        ttk.Entry(src, textvariable=self.var_client_secret, width=32, show="*").pack(side="left", **pad)
        oauth_link = tk.Label(src, text="注册 OAuth", fg="blue", cursor="hand2",
                              font=("", 9, "underline"))
        oauth_link.pack(side="left", padx=4)
        oauth_link.bind("<Button-1>", lambda e: webbrowser.open(
            "https://osu.ppy.sh/home/account/edit#oauth"))

        # 筛选区
        filt = ttk.LabelFrame(self.root, text="筛选条件")
        filt.pack(fill="x", padx=8, pady=4)

        row1 = ttk.Frame(filt)
        row1.pack(fill="x", padx=4, pady=2)
        ttk.Label(row1, text="开始日期").pack(side="left")
        ttk.Entry(row1, textvariable=self.var_start, width=12).pack(side="left", **pad)
        ttk.Label(row1, text="结束日期(可空)").pack(side="left")
        ttk.Entry(row1, textvariable=self.var_end, width=12).pack(side="left", **pad)
        ttk.Label(row1, text="游戏模式").pack(side="left")
        ttk.Combobox(row1, textvariable=self.var_mode, values=["全部", "osu!", "太鼓", "接水果", "下落式"], width=8, state="readonly").pack(side="left", **pad)
        ttk.Label(row1, text="mania键数").pack(side="left")
        ttk.Combobox(row1, textvariable=self.var_keys, values=["全部", "4K", "5K", "6K", "7K", "8K", "9K", "10K"], width=6, state="readonly").pack(side="left", **pad)
        ttk.Checkbutton(row1, text="仅纯该键数", variable=self.var_pure_keys).pack(side="left", padx=2)
        ttk.Label(row1, text="下载类型").pack(side="left")
        ttk.Combobox(row1, textvariable=self.var_file_type, values=["无视频", "完整版(含视频)", "迷你版"], width=14, state="readonly").pack(side="left", **pad)
        ttk.Label(row1, text="并发数").pack(side="left")
        ttk.Spinbox(row1, from_=1, to=10, textvariable=self.var_workers, width=3).pack(side="left", **pad)

        row2 = ttk.Frame(filt)
        row2.pack(fill="x", padx=4, pady=2)
        self._add_range(row2, "星级", self.var_star_min, self.var_star_max)
        self._add_range(row2, "BPM", self.var_bpm_min, self.var_bpm_max)
        self._add_range(row2, "时长(秒)", self.var_len_min, self.var_len_max)

        row3 = ttk.Frame(filt)
        row3.pack(fill="x", padx=4, pady=2)
        self._add_range(row3, "AR", self.var_ar_min, self.var_ar_max)
        self._add_range(row3, "OD", self.var_od_min, self.var_od_max)
        self._add_range(row3, "CS", self.var_cs_min, self.var_cs_max)
        self._add_range(row3, "HP", self.var_hp_min, self.var_hp_max)

        row4 = ttk.Frame(filt)
        row4.pack(fill="x", padx=4, pady=2)
        ttk.Label(row4, text="作者/Mapper").pack(side="left")
        ttk.Entry(row4, textvariable=self.var_creator, width=18).pack(side="left", **pad)
        ttk.Label(row4, text="曲师").pack(side="left")
        ttk.Entry(row4, textvariable=self.var_artist, width=18).pack(side="left", **pad)
        ttk.Label(row4, text="标题").pack(side="left")
        ttk.Entry(row4, textvariable=self.var_title, width=18).pack(side="left", **pad)
        ttk.Label(row4, text="标签").pack(side="left")
        ttk.Entry(row4, textvariable=self.var_tags, width=18).pack(side="left", **pad)

        row5 = ttk.Frame(filt)
        row5.pack(fill="x", padx=4, pady=2)
        ttk.Label(row5, text="审核状态").pack(side="left")
        for key in ["1", "2", "3", "4", "0", "-1", "-2"]:
            ttk.Checkbutton(row5, text=APPROVED_LABELS[key], variable=self.var_approved[key]).pack(side="left", padx=2)
        ttk.Label(row5, text="视频").pack(side="left", padx=(10, 0))
        ttk.Combobox(row5, textvariable=self.var_video, values=["不限", "仅无视频", "仅含视频"], width=8, state="readonly").pack(side="left", **pad)
        ttk.Label(row5, text="故事板").pack(side="left")
        ttk.Combobox(row5, textvariable=self.var_storyboard, values=["不限", "无", "有"], width=6, state="readonly").pack(side="left", **pad)
        ttk.Checkbutton(row5, text="跳过已下载", variable=self.var_skip_existing).pack(side="left", padx=8)

        # 操作按钮
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", padx=8, pady=4)
        ttk.Button(btns, text="获取并筛选", command=self.start_search).pack(side="left", padx=4)
        ttk.Button(btns, text="下载全部", command=lambda: self.start_download(selected_only=False)).pack(side="left", padx=4)
        ttk.Button(btns, text="下载选中", command=lambda: self.start_download(selected_only=True)).pack(side="left", padx=4)
        ttk.Button(btns, text="从断点继续", command=lambda: self.start_search(resume=True)).pack(side="left", padx=4)
        ttk.Button(btns, text="停止", command=self.stop).pack(side="left", padx=4)
        ttk.Button(btns, text="清空列表", command=self.clear_list).pack(side="left", padx=4)

        # 结果表格
        table_frame = ttk.LabelFrame(self.root, text="筛选结果")
        table_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("sid", "title", "creator", "modes", "stars", "bpm", "length", "approved", "video", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "sid": "Set ID",
            "title": "标题",
            "creator": "作者",
            "modes": "模式",
            "stars": "星级范围",
            "bpm": "BPM",
            "length": "时长(s)",
            "approved": "状态",
            "video": "视频",
            "status": "下载状态",
        }
        widths = {
            "sid": 80,
            "title": 320,
            "creator": 120,
            "modes": 100,
            "stars": 90,
            "bpm": 80,
            "length": 80,
            "approved": 80,
            "video": 60,
            "status": 160,
        }
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w", stretch=(col in ("title", "status")))

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        # 进度与日志
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=4)

        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", pady=2)

        self.log_text = tk.Text(bottom, height=8, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=False, pady=2)

    def _add_range(self, parent, label, var_min, var_max):
        ttk.Label(parent, text=label).pack(side="left", padx=(8, 0))
        ttk.Entry(parent, textvariable=var_min, width=6).pack(side="left", padx=2)
        ttk.Label(parent, text="~").pack(side="left")
        ttk.Entry(parent, textvariable=var_max, width=6).pack(side="left", padx=2)

    # ---------- 路径 ----------
    def browse_songs(self):
        path = filedialog.askdirectory(title="选择 osu! 的 Songs 文件夹")
        if path:
            self.var_songs.set(path)

    def auto_detect_osu(self):
        """Windows 下从注册表读取 osu! 安装路径。"""
        if sys.platform != "win32":
            self.log("当前不是 Windows，请手动选择 Songs 目录。")
            return
        try:
            import winreg
            for hive, subkey in (
                (winreg.HKEY_CLASSES_ROOT, r"osu!\DefaultIcon"),
                (winreg.HKEY_CLASSES_ROOT, r"osu\DefaultIcon"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\osu!\DefaultIcon"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Classes\osu\DefaultIcon"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Classes\osu!\DefaultIcon"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Classes\osu\DefaultIcon"),
            ):
                try:
                    with winreg.OpenKey(hive, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, "")
                        # 值形如 "E:\...\osu!.exe",0
                        exe_path = value.split(",")[0].strip('"')
                        songs = os.path.join(os.path.dirname(exe_path), "Songs")
                        if os.path.isdir(songs):
                            self.var_songs.set(songs)
                            self.log(f"自动检测到 Songs 目录：{songs}")
                            return
                except OSError:
                    continue
            self.log("未从注册表找到 osu!，请手动选择 Songs 目录。")
        except Exception as exc:
            self.log(f"自动检测失败：{exc}")

    def open_songs_dir(self):
        path = self.var_songs.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showwarning("提示", "请先选择有效的 Songs 目录")
            return
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])

    # ---------- 筛选 ----------
    def _collect_filters(self) -> Filters:
        f = Filters()
        mode_map = {"全部": "", "osu!": "0", "太鼓": "1", "接水果": "2", "下落式": "3"}
        f.mode = mode_map.get(self.var_mode.get(), "")
        keys_text = self.var_keys.get().strip()
        if keys_text and keys_text != "全部":
            f.keys = keys_text.replace("K", "")
            # 选择键数时自动限定为 mania
            f.mode = "3"
        f.pure_keys = self.var_pure_keys.get()
        f.approved = {k for k, v in self.var_approved.items() if v.get()}
        f.star_min = self._get_float(self.var_star_min)
        f.star_max = self._get_float(self.var_star_max)
        f.bpm_min = self._get_float(self.var_bpm_min)
        f.bpm_max = self._get_float(self.var_bpm_max)
        f.length_min = self._get_int(self.var_len_min)
        f.length_max = self._get_int(self.var_len_max)
        f.ar_min = self._get_float(self.var_ar_min)
        f.ar_max = self._get_float(self.var_ar_max)
        f.od_min = self._get_float(self.var_od_min)
        f.od_max = self._get_float(self.var_od_max)
        f.cs_min = self._get_float(self.var_cs_min)
        f.cs_max = self._get_float(self.var_cs_max)
        f.hp_min = self._get_float(self.var_hp_min)
        f.hp_max = self._get_float(self.var_hp_max)
        f.creator = self.var_creator.get().strip()
        f.artist = self.var_artist.get().strip()
        f.title = self.var_title.get().strip()
        f.tags = self.var_tags.get().strip()
        f.video = {"不限": "", "仅无视频": "0", "仅含视频": "1"}.get(self.var_video.get(), "")
        f.storyboard = {"不限": "", "无": "0", "有": "1"}.get(self.var_storyboard.get(), "")
        f.skip_unavailable = True
        f.end_date = self.var_end.get().strip()
        return f

    @staticmethod
    def _get_float(var):
        s = var.get().strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _get_int(var):
        s = var.get().strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None

    def start_search(self, resume=False):
        if self.searching:
            messagebox.showinfo("提示", "正在获取中，请稍候")
            return
        if self.downloading:
            messagebox.showinfo("提示", "正在下载中，请先停止或等待完成")
            return
        songs = self.var_songs.get().strip()
        if not songs:
            messagebox.showwarning("提示", "请先选择 osu! Songs 目录")
            return

        filters = self._collect_filters()
        resume_items = []

        if resume:
            if self.var_source.get() == "official":
                messagebox.showwarning("提示", "官方源不支持断点续传")
                return
            if not self.partial_beatmaps or not self.partial_since:
                messagebox.showwarning("提示", "当前没有可续传的进度")
                return
            if filters.mode != self.partial_mode:
                messagebox.showwarning("提示", "游戏模式已改变，无法续传，请重新获取")
                return
            start = self.partial_since
            resume_items = self.partial_beatmaps
            self.log(f"从断点继续：已有 {len(resume_items)} 条，从 {start} 继续获取……")
        else:
            start = self.var_start.get().strip()
            if self.var_source.get() != "official":
                if not start:
                    messagebox.showwarning("提示", "请填写开始日期")
                    return
                start_dt = _parse_date(start)
                if not start_dt:
                    messagebox.showwarning("提示", "开始日期格式不正确，请使用 YYYY-MM-DD")
                    return
                earliest_dt = _parse_date(EARLIEST_DATE)
                if start_dt < earliest_dt:
                    self.var_start.set(EARLIEST_DATE)
                    self.log(f"开始日期不能早于镜像站最早收录日期 {EARLIEST_DATE}，已自动调整为该日期。")
                    start = EARLIEST_DATE
            # 新任务会清掉旧断点
            self.partial_beatmaps = []
            self.partial_since = None
            self.partial_mode = ""
            self.log("开始获取谱面数据……")

        self.cancel_event = threading.Event()
        self.searching = True
        self.current_sets = []
        self.download_results.clear()
        self._set_buttons_state(searching=True)

        workers = self._get_int(self.var_workers) or 3
        songs_dir = songs

        def worker():
            try:
                if self.var_source.get() == "official":
                    if resume:
                        self.worker_queue.put(("search_done", None, "官方源不支持断点续传"))
                        return
                    cid = self.var_client_id.get().strip()
                    csec = self.var_client_secret.get().strip()
                    if not cid or not csec:
                        self.worker_queue.put(("search_done", None, "请填写官方 API 的 Client ID / Client Secret"))
                        return
                    new_beatmaps = fetch_official_beatmaps(
                        filters,
                        cid,
                        csec,
                        progress_cb=lambda n, d: self.worker_queue.put(("fetch_progress", n, d)),
                        cancel_event=self.cancel_event,
                    )
                else:
                    new_beatmaps = fetch_beatmaps(
                        start,
                        filters.end_date,
                        filters.mode,
                        progress_cb=lambda n, d: self.worker_queue.put(("fetch_progress", n, d)),
                        cancel_event=self.cancel_event,
                    )
                if self.cancel_event.is_set():
                    self.worker_queue.put(("search_done", None, "已取消"))
                    return
                beatmaps = resume_items + new_beatmaps if resume_items else new_beatmaps
                sets = filter_and_group(beatmaps, filters)
                # 跳过已下载
                if self.var_skip_existing.get():
                    before = len(sets)
                    sets = [s for s in sets if not is_downloaded(s["set_id"], songs_dir)]
                    skipped = before - len(sets)
                    if skipped:
                        self.worker_queue.put(("log", None, f"已自动跳过 {skipped} 个已下载的谱面。"))
                self.worker_queue.put(("search_done", sets, None))
            except FetchError as exc:
                combined = resume_items + exc.partial_items
                self.worker_queue.put((
                    "fetch_partial",
                    combined,
                    exc.last_since,
                    filters.mode,
                    str(exc),
                ))
            except Exception as exc:
                self.worker_queue.put(("search_done", None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 下载 ----------
    def start_download(self, selected_only: bool):
        if self.downloading:
            messagebox.showinfo("提示", "正在下载中，请稍候")
            return
        if self.searching:
            messagebox.showinfo("提示", "正在获取中，请等待筛选完成")
            return
        if not self.current_sets:
            messagebox.showwarning("提示", "列表为空，请先获取并筛选")
            return

        songs = self.var_songs.get().strip()
        if not songs or not os.path.isdir(songs):
            messagebox.showwarning("提示", "请选择有效的 Songs 目录")
            return

        if selected_only:
            sel = self.tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请先在列表中选择谱面")
                return
            ids = {int(self.tree.item(i, "values")[0]) for i in sel}
            set_infos = [s for s in self.current_sets if s["set_id"] in ids]
        else:
            set_infos = self.current_sets[:]

        if not set_infos:
            return

        file_type_map = {"无视频": "novideo", "完整版(含视频)": "full", "迷你版": "mini"}
        file_type = file_type_map.get(self.var_file_type.get(), "novideo")
        workers = self._get_int(self.var_workers) or 3
        source = self.var_source.get()

        if source == "official":
            if not self.var_client_id.get().strip() or not self.var_client_secret.get().strip():
                messagebox.showwarning("提示", "请填写官方 API 的 Client ID / Client Secret")
                return

        self.cancel_event = threading.Event()
        self.downloading = True
        self._set_buttons_state(downloading=True)
        self.log(f"开始下载 {len(set_infos)} 个谱面，类型：{self.var_file_type.get()}，并发：{workers}")

        self.progress["maximum"] = len(set_infos)
        self.progress["value"] = 0
        self.download_results = {s["set_id"]: "等待中" for s in set_infos}
        self._update_tree_statuses()

        def worker():
            done = [0]
            lock = threading.Lock()

            def item_cb(sid, ok, msg):
                with lock:
                    done[0] += 1
                    self.download_results[sid] = "完成" if ok else f"失败：{msg}"
                    self.worker_queue.put(("download_item", sid, ok, msg))
                    self.worker_queue.put(("download_progress", done[0]))

            def progress_cb(sid, downloaded, total):
                if total:
                    pct = downloaded / total * 100
                    self.worker_queue.put(("download_file_progress", sid, downloaded, total, pct))

            try:
                token = ""
                if source == "official":
                    token = official_get_token(
                        self.var_client_id.get().strip(),
                        self.var_client_secret.get().strip(),
                    )
                download_many(
                    set_infos,
                    songs,
                    file_type,
                    workers=workers,
                    source=source,
                    token=token,
                    cancel_event=self.cancel_event,
                    item_cb=item_cb,
                    progress_cb=progress_cb,
                )
            except Exception as exc:
                self.worker_queue.put(("log", None, f"下载过程异常：{exc}"))
            self.worker_queue.put(("download_done", None, None))

        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        if self.searching or self.downloading:
            self.cancel_event.set()
            self.log("正在停止……")

    def clear_list(self):
        if self.searching or self.downloading:
            messagebox.showwarning("提示", "任务运行中，不能清空列表")
            return
        self.tree.delete(*self.tree.get_children())
        self.current_sets = []
        self.log("列表已清空")

    # ---------- UI 更新 ----------
    def _set_buttons_state(self, searching=False, downloading=False):
        # 这里简单禁用/恢复比较繁琐，可保留；主要通过停止按钮控制。
        pass

    def _poll_queue(self):
        try:
            while True:
                msg = self.worker_queue.get_nowait()
                kind = msg[0]
                if kind == "fetch_progress":
                    _, n, d = msg
                    self.log(f"已获取 {n} 个难度，当前到 {d}", echo=False)
                elif kind == "search_done":
                    _, sets, err = msg
                    self.searching = False
                    if err:
                        self.log(f"获取失败：{err}")
                        messagebox.showerror("错误", err)
                    elif sets is None:
                        self.log("获取已取消。")
                    else:
                        self.current_sets = sets
                        self._fill_tree(sets)
                        self.log(f"筛选完成，共 {len(sets)} 个谱面集合。")
                        # 成功后清除断点
                        self.partial_beatmaps = []
                        self.partial_since = None
                        self.partial_mode = ""
                elif kind == "fetch_partial":
                    _, partial_items, last_since, mode, errstr = msg
                    self.searching = False
                    self.partial_beatmaps = partial_items
                    self.partial_since = last_since
                    self.partial_mode = mode
                    self.log(f"获取中断：{errstr}")
                    self.log(f"已保留 {len(partial_items)} 条进度，当前到 {last_since}；可点击“从断点继续”。")
                    messagebox.showwarning("获取中断", "已保留当前进度，可点击“从断点继续”继续获取。")
                elif kind == "log":
                    self.log(msg[2])
                elif kind == "download_item":
                    _, sid, ok, msgtext = msg
                    if ok:
                        self.log(f"[{sid}] {msgtext}")
                    else:
                        self.log(f"[{sid}] 失败：{msgtext}")
                    self._update_tree_statuses()
                elif kind == "download_file_progress":
                    _, sid, downloaded, total, pct = msg
                    if sid in self.download_results and not self.download_results[sid].startswith(("完成", "失败")):
                        self.download_results[sid] = f"下载中 {pct:.0f}%"
                        self._update_tree_statuses()
                elif kind == "download_progress":
                    _, n = msg
                    self.progress["value"] = n
                elif kind == "download_done":
                    self.downloading = False
                    self.progress["value"] = self.progress["maximum"]
                    self.log("下载任务结束。")
                    messagebox.showinfo("完成", "下载任务结束")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _fill_tree(self, sets):
        self.tree.delete(*self.tree.get_children())
        songs_dir = self.var_songs.get().strip()
        for s in sets:
            stars = self._format_range(s["stars_min"], s["stars_max"], digits=2)
            bpm = self._format_range(s["bpm_min"], s["bpm_max"], digits=0)
            length = self._format_range(s["length_min"], s["length_max"], digits=0)
            approved = APPROVED_LABELS.get(str(s["approved"]), str(s["approved"]))
            video = "有" if s.get("video") == "1" else "无"
            status = self.download_results.get(s["set_id"])
            if status is None:
                status = "已下载" if is_downloaded(s["set_id"], songs_dir) else "待下载"
            self.tree.insert("", "end", iid=str(s["set_id"]), values=(
                s["set_id"],
                f"{s['artist']} - {s['title']}",
                s["creator"],
                s["modes_text"],
                stars,
                bpm,
                length,
                approved,
                video,
                status,
            ))

    def _update_tree_statuses(self):
        for iid in self.tree.get_children():
            sid = int(iid)
            if sid in self.download_results:
                self.tree.set(iid, "status", self.download_results[sid])

    @staticmethod
    def _format_range(lo, hi, digits=0):
        if lo is None and hi is None:
            return ""
        if lo is None:
            return f"≤{hi:.{digits}f}"
        if hi is None:
            return f"≥{lo:.{digits}f}"
        if lo == hi:
            return f"{lo:.{digits}f}"
        return f"{lo:.{digits}f}~{hi:.{digits}f}"

    def log(self, text, echo=True):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


# ---------------------------------------------------------------------------
# CLI（Linux / 无 GUI 环境）
# ---------------------------------------------------------------------------


def build_cli_parser():
    p = argparse.ArgumentParser(
        prog="RankedDownloader.py",
        description="osu! Ranked 地图下载器 CLI（支持 sayobot 与官方 osu! API v2）",
    )
    p.add_argument("--source", choices=["sayobot", "official"], default="sayobot")
    p.add_argument("--client-id", default="", help="官方 OAuth Client ID")
    p.add_argument("--client-secret", default="", help="官方 OAuth Client Secret")
    p.add_argument("--songs", "--output", dest="songs", default=".",
                   help="Songs/保存目录，默认当前目录")
    p.add_argument("--start-date", default=DEFAULT_START)
    p.add_argument("--end-date", default="")
    p.add_argument("--mode", default="",
                   choices=["", "any", "osu", "taiko", "catch", "mania", "0", "1", "2", "3"])
    p.add_argument("--keys", type=int, default=0, help="mania 键数，如 4")
    p.add_argument("--pure-keys", action="store_true", help="仅保留整组都是该键数的谱面")
    p.add_argument("--approved", default="", help="审核状态列表，如 1,3,4 或 ranked,loved")
    p.add_argument("--star-min", type=float)
    p.add_argument("--star-max", type=float)
    p.add_argument("--bpm-min", type=float)
    p.add_argument("--bpm-max", type=float)
    p.add_argument("--length-min", type=int)
    p.add_argument("--length-max", type=int)
    p.add_argument("--ar-min", type=float)
    p.add_argument("--ar-max", type=float)
    p.add_argument("--od-min", type=float)
    p.add_argument("--od-max", type=float)
    p.add_argument("--cs-min", type=float)
    p.add_argument("--cs-max", type=float)
    p.add_argument("--hp-min", type=float)
    p.add_argument("--hp-max", type=float)
    p.add_argument("--creator", default="")
    p.add_argument("--artist", default="")
    p.add_argument("--title", default="")
    p.add_argument("--tags", default="")
    p.add_argument("--video", choices=["any", "no", "yes"], default="any")
    p.add_argument("--storyboard", choices=["any", "no", "yes"], default="any")
    p.add_argument("--file-type", choices=["novideo", "full", "mini"], default="novideo")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--max-results", type=int, default=0, help="最多显示/下载前 N 个结果，0 表示不限制")
    p.add_argument("--list", action="store_true", help="列出匹配谱面")
    p.add_argument("--download", action="store_true", help="下载匹配谱面")
    return p


def _approved_from_cli(text):
    if not text:
        return {"1"}  # 默认与 GUI 一致：Ranked
    name_to_code = {
        "ranked": "1", "approved": "2", "qualified": "3", "loved": "4",
        "pending": "0", "wip": "-1", "graveyard": "-2",
    }
    out = set()
    for part in text.split(","):
        part = part.strip().lower()
        if not part:
            continue
        out.add(name_to_code.get(part, part))
    return out or {"1"}


def cli_main(argv):
    parser = build_cli_parser()
    args = parser.parse_args(argv)

    f = Filters()
    mode_map = {
        "osu": "0", "taiko": "1", "catch": "2", "mania": "3",
        "any": "", "0": "0", "1": "1", "2": "2", "3": "3", "": "",
    }
    f.mode = mode_map.get(args.mode, args.mode)
    if args.keys:
        f.keys = str(args.keys)
        f.mode = "3"  # 选键数时自动限定 mania
    f.pure_keys = args.pure_keys
    f.approved = _approved_from_cli(args.approved)
    f.star_min = args.star_min
    f.star_max = args.star_max
    f.bpm_min = args.bpm_min
    f.bpm_max = args.bpm_max
    f.length_min = args.length_min
    f.length_max = args.length_max
    f.ar_min = args.ar_min
    f.ar_max = args.ar_max
    f.od_min = args.od_min
    f.od_max = args.od_max
    f.cs_min = args.cs_min
    f.cs_max = args.cs_max
    f.hp_min = args.hp_min
    f.hp_max = args.hp_max
    f.creator = args.creator
    f.artist = args.artist
    f.title = args.title
    f.tags = args.tags
    f.video = {"any": "", "no": "0", "yes": "1"}[args.video]
    f.storyboard = {"any": "", "no": "0", "yes": "1"}[args.storyboard]
    f.skip_unavailable = True
    f.end_date = args.end_date

    if args.source == "official":
        if not args.client_id or not args.client_secret:
            parser.error("使用官方源时必须提供 --client-id 和 --client-secret")
        print("正在使用官方 osu! API v2 获取谱面……")
        beatmaps = fetch_official_beatmaps(f, args.client_id, args.client_secret)
    else:
        print(f"正在使用 sayobot 获取谱面，开始日期：{args.start_date}……")
        beatmaps = fetch_beatmaps(args.start_date, args.end_date, f.mode)

    sets = filter_and_group(beatmaps, f)
    if args.songs and os.path.isdir(args.songs):
        before = len(sets)
        sets = [s for s in sets if not is_downloaded(s["set_id"], args.songs)]
        skipped = before - len(sets)
        if skipped:
            print(f"已自动跳过 {skipped} 个已下载谱面。")
    if args.max_results > 0:
        sets = sets[:args.max_results]

    if args.list or not args.download:
        print(f"\n匹配到 {len(sets)} 个谱面集合：")
        for s in sets:
            modes = s.get("modes_text", "")
            stars = ""
            if s.get("stars_min") is not None:
                stars = f"{s['stars_min']:.2f}~{s['stars_max']:.2f}"
            print(f"[{s['set_id']}] {s['artist']} - {s['title']} by {s['creator']} "
                  f"[{modes}] stars={stars} approved={s.get('approved')}")

    if args.download:
        if not sets:
            print("没有可下载的谱面。")
            return 0
        os.makedirs(args.songs, exist_ok=True)
        token = ""
        if args.source == "official":
            token = official_get_token(args.client_id, args.client_secret)
        print(f"\n开始下载 {len(sets)} 个谱面到 {args.songs}，类型：{args.file_type}，并发：{args.workers}")
        download_many(sets, args.songs, args.file_type, workers=args.workers,
                      source=args.source, token=token,
                      item_cb=lambda sid, ok, msg: print(f"[{sid}] {'OK' if ok else 'FAIL'} {msg}"))

    return 0


def main():
    if len(sys.argv) > 1:
        return cli_main(sys.argv[1:])
    if tk is None:
        print("当前环境没有 Tkinter，无法启动 GUI。请安装 python3-tk 或在 Windows 上运行。")
        print("你也可以直接使用 CLI：python RankedDownloader.py --help")
        return 1
    root = tk.Tk()
    app = RankedDownloaderApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
