"""
抖音视频分析器

功能：
1. 解析抖音链接
2. 下载视频
3. 语音转写
4. 保存转写内容供AI分析

注意：本脚本只负责下载和转写，不自动生成笔记。
笔记由AI读取转写内容后手动分析生成。

临时目录：%LOCALAPPDATA%\\douyin-skill\\<视频id>\\（不在 Obsidian 库内，不会被同步带走）。
- 转写产物带 transcript.meta.json 元信息（video_id/标题/生成时间），读取前必校验，
  不匹配一律按不存在处理并移除，绝不静默复用别的视频的转写。
- 正常结束后自动清理该视频的临时目录（--keep 可保留）；失败时保留并打印路径。
- 成功后转写全文和视频信息会打印在 stdout 的标记块里，清理后仍可读取。
"""

import os
import sys
import json
import getpass
import argparse
import shutil
import subprocess
import time
import requests
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

# 设置UTF-8编码，避免乱码问题
sys.stdout.reconfigure(encoding='utf-8')


# ---------------------------------------------------------------------------
# SiliconFlow API Key 管理
# 读取优先级：环境变量 SILICONFLOW_API_KEY > 用户配置文件 > 首次运行交互输入
# 配置文件放在用户目录（不在技能目录），避免技能目录被分享/同步时带出密钥。
# 脚本内不保存任何明文 Key；终端只打印来源，不打印 Key 内容。
# ---------------------------------------------------------------------------

API_KEY_ENV = "SILICONFLOW_API_KEY"
NO_PROMPT_ENV = "DOUYIN_SKILL_NO_PROMPT"
SKILL_HOME_ENV = "DOUYIN_SKILL_HOME"        # 用户级资源目录（默认 ~/.douyin-skill）
TEMP_ROOT_ENV = "DOUYIN_TEMP_ROOT"          # 临时目录根（默认 %LOCALAPPDATA%/douyin-skill）
NOTES_DIR_ENV = "DOUYIN_NOTES_DIR"          # 笔记输出目录（由用户指定）
LEGACY_TEMP_ENV = "DOUYIN_LEGACY_TEMP_DIR"  # 旧版临时目录（仅做残留提示）

_home_override = (os.environ.get(SKILL_HOME_ENV) or "").strip()
CONFIG_DIR = (os.path.abspath(os.path.expanduser(_home_override)) if _home_override
              else os.path.join(os.path.expanduser("~"), ".douyin-skill"))
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


class ApiKeyError(RuntimeError):
    """API Key 缺失或保存失败"""


def _harden_permissions(path: str, is_dir: bool = False) -> None:
    """尽量把权限收紧到仅当前用户可访问（失败不影响主流程）"""
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except Exception:
        pass
    if os.name != "nt":
        return
    user = (os.environ.get("USERNAME") or "").strip()
    if not user:
        return
    grant = user + (":(OI)(CI)F" if is_dir else ":F")
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", grant],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass


def _stdin_is_interactive() -> bool:
    """判断 stdin 是否真是可交互终端。

    Windows 下 `subprocess.DEVNULL`（NUL 设备）会让 `isatty()` 误报 True，
    因此再用 GetConsoleMode 确认拿到的是真实控制台句柄。
    """
    try:
        if not sys.stdin.isatty():
            return False
    except Exception:
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_ulong()
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except Exception:
        return False


def _read_config() -> Dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"警告：读取配置文件失败（{e}），按未配置处理")
        return {}
    return data if isinstance(data, dict) else {}


def _saved_api_key() -> Optional[str]:
    value = _read_config().get("siliconflow_api_key")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _save_config(field: str, value) -> str:
    """把单个字段写入用户配置文件（保留其它字段），返回配置文件路径"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    _harden_permissions(CONFIG_DIR, is_dir=True)
    data = _read_config()
    data[field] = value
    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, CONFIG_PATH)
    _harden_permissions(CONFIG_PATH)
    return CONFIG_PATH


def save_api_key(key: str) -> str:
    """保存 API Key 到用户配置文件，返回配置文件路径"""
    key = (key or "").strip()
    if not key:
        raise ApiKeyError("API Key 为空，未保存。")
    return _save_config("siliconflow_api_key", key)


def resolve_notes_dir() -> str:
    """笔记输出目录：环境变量 > 配置文件 notes_dir > 空（未配置）"""
    env_value = (os.environ.get(NOTES_DIR_ENV) or "").strip()
    if env_value:
        return os.path.abspath(os.path.expanduser(env_value))
    value = _read_config().get("notes_dir")
    if isinstance(value, str) and value.strip():
        return os.path.abspath(os.path.expanduser(value.strip()))
    return ""


def save_notes_dir(path: str) -> str:
    """保存笔记输出目录到用户配置文件，返回该目录"""
    value = (path or "").strip()
    if not value:
        raise ApiKeyError("笔记目录为空，未保存。")
    resolved = os.path.abspath(os.path.expanduser(value))
    os.makedirs(resolved, exist_ok=True)
    _save_config("notes_dir", resolved)
    return resolved


def missing_api_key_message() -> str:
    script = os.path.abspath(__file__)
    return (
        "未找到 SiliconFlow API Key，当前为非交互环境，无法提示输入。\n"
        "请任选一种方式配置后重试：\n"
        f"  1) 设置环境变量 {API_KEY_ENV}\n"
        f'  2) 运行 python "{script}" --set-key\n'
        f"  3) 编辑配置文件并填写 siliconflow_api_key：{CONFIG_PATH}"
    )


def load_api_key(allow_prompt: bool = True):
    """按 环境变量 > 配置文件 > 交互输入 的顺序取 Key，返回 (key, 来源说明)"""
    env_key = (os.environ.get(API_KEY_ENV) or "").strip()
    if env_key:
        return env_key, f"环境变量 {API_KEY_ENV}"

    saved = _saved_api_key()
    if saved:
        return saved, f"配置文件 {CONFIG_PATH}"

    no_prompt = (os.environ.get(NO_PROMPT_ENV) or "").strip().lower() in ("1", "true", "yes")
    if not (allow_prompt and _stdin_is_interactive() and not no_prompt):
        raise ApiKeyError(missing_api_key_message())

    print("首次运行：未检测到 SiliconFlow API Key。")
    print(f"（也可以设置环境变量 {API_KEY_ENV} 或运行 --set-key 来免交互配置）")
    try:
        key = getpass.getpass("请输入 SiliconFlow API Key（直接回车取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        key = ""
    if not key:
        raise ApiKeyError("未输入 API Key，已取消。")
    path = save_api_key(key)
    print(f"API Key 已保存到: {path}")
    print("后续运行会自动读取，无需再次输入。")
    return key, f"配置文件 {path}"


def _default_temp_root() -> str:
    """临时目录根：DOUYIN_TEMP_ROOT > %LOCALAPPDATA%\\douyin-skill > ~/.douyin-skill/local-temp"""
    env_value = (os.environ.get(TEMP_ROOT_ENV) or "").strip()
    if env_value:
        return os.path.abspath(os.path.expanduser(env_value))
    local_app = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app:
        return os.path.join(local_app, "douyin-skill")
    return os.path.join(os.path.expanduser("~"), ".douyin-skill", "local-temp")


# ---------------------------------------------------------------------------
# detail → 内部结构：纯函数区（无 IO、无 print，便于 fixture 测试）
# ---------------------------------------------------------------------------

SUBTITLE_MIN_COVERAGE = 0.9   # 末条 end_time / duration
SUBTITLE_MIN_DENSITY = 2.0    # 字幕字数 / 末条 end_time 秒数


def clean_author_name(name: str):
    """清理作者名字。返回 (名字或 None, 是否有乱码)，不打印。"""
    if not name:
        return None, False
    for c in name:
        if ord(c) > 127 and not (0x4E00 <= ord(c) <= 0x9FFF):
            return None, True
    return name, False


def media_candidates(detail: Dict):
    """生成转写用媒体的候选列表：音频流 > 最低码率视频档 > play_addr（纯函数）"""
    def gear_urls(gear):
        gear = gear or {}
        urls = list(((gear.get("play_addr") or {}).get("url_list")) or [])
        if not urls:
            # bit_rate_audio 档位：地址在 audio_meta.url_list 的 main/backup/fallback
            ul = ((gear.get("audio_meta") or {}).get("url_list")) or {}
            if isinstance(ul, dict):
                for key in ("main_url", "backup_url", "fallback_url"):
                    v = ul.get(key)
                    if isinstance(v, str) and v:
                        urls.append(v)
                    elif isinstance(v, list):
                        urls.extend([u for u in v if u])
            elif isinstance(ul, list):
                urls.extend([u for u in ul if u])
        return urls

    def gear_bitrate(g):
        try:
            return int((g or {}).get("bit_rate") or 0)
        except Exception:
            return 0

    def gear_size(g):
        g = g or {}
        size = (g.get("audio_meta") or {}).get("size")
        if size is None:
            size = (g.get("play_addr") or {}).get("data_size")
        try:
            return int(size) if size is not None else 0
        except Exception:
            return 0

    def as_gear_list(value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
        return []

    video = (detail or {}).get("video") or {}
    candidates = []

    def add(url, kind, size=0):
        if url and (url, kind) not in [(c[0], c[1]) for c in candidates]:
            candidates.append((url, kind, size))

    # 1) 音频流（bit_rate_audio 顶层列表为实测主要形态）
    audio_gears = []
    for holder in (detail or {}, video):
        for key, value in holder.items():
            if "audio" not in str(key).lower():
                continue
            for gear in as_gear_list(value):
                if gear_urls(gear):
                    audio_gears.append(gear)
    for gear in (video.get("bit_rate") or []):
        name = str(gear.get("gear_name") or "")
        if gear.get("is_audio") or "audio" in name.lower():
            if gear_urls(gear):
                audio_gears.append(gear)
    if audio_gears:
        def audio_bitrate(g):
            try:
                return int((g.get("audio_meta") or {}).get("bitrate") or 0)
            except Exception:
                return 0
        lowest = min(audio_gears, key=lambda g: (audio_bitrate(g) or gear_bitrate(g)))
        for u in gear_urls(lowest):
            add(u, "audio", gear_size(lowest))

    # 2) 最低码率视频档
    usable = [g for g in (video.get("bit_rate") or []) if gear_urls(g)]
    if usable:
        lowest = min(usable, key=gear_bitrate)
        for u in gear_urls(lowest):
            add(u, "video-low", gear_size(lowest))

    # 3) play_addr
    for u in ((video.get("play_addr") or {}).get("url_list") or []):
        add(u, "video-default")

    return candidates


def _as_int(value) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _segment_time(seg: Dict):
    """字幕段落的时间字段：兼容 start_time/end_time、start/end、*_ms"""
    start = _as_int(seg.get("start_time", seg.get("start_ms", seg.get("start"))))
    end = _as_int(seg.get("end_time", seg.get("end_ms", seg.get("end"))))
    return start, end


def extract_caption_infos(detail: Dict):
    """返回 (caption_infos 列表, 来源路径)。找不到返回 ([], "")"""
    video = (detail or {}).get("video") or {}
    sources = (
        ("video.cla_info.caption_infos", ((video.get("cla_info") or {}).get("caption_infos"))),
        ("cla_info.caption_infos", (((detail or {}).get("cla_info") or {}).get("caption_infos"))),
        ("caption_infos", (detail or {}).get("caption_infos")),
    )
    for path, infos in sources:
        if isinstance(infos, list) and infos:
            return infos, path
    return [], ""


def build_chapters(detail: Dict, duration_ms: int):
    """chapter_list → (chapters, anchors, stats)

    - 无标题章节：保留，title 为空字符串，锚点只有时间码（不写任何占位词）
    - detail 兜底：仅当 desc 为空时使用，最多保留 20 字，超出加省略号
    - 无 timestamp：无法定位，丢弃并逐条记录原因
    - 同秒重复：保留原顺序，不合并，记 warning
    """
    raw = (detail or {}).get("chapter_list") or []
    stats = {"raw_count": len(raw) if isinstance(raw, list) else 0,
             "kept": 0, "dropped": [], "warnings": []}
    chapters = []
    seen_starts = set()

    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            stats["dropped"].append({"index": index, "desc": None,
                                     "timestamp": None, "reason": "条目不是对象"})
            continue
        start = _as_int(item.get("timestamp"))
        desc = str(item.get("desc") or "").strip()
        detail_text = str(item.get("detail") or "").strip()
        if start is None:
            stats["dropped"].append({"index": index, "desc": desc,
                                     "timestamp": item.get("timestamp"),
                                     "reason": "无有效时间戳"})
            continue

        title = desc
        if not title and detail_text:
            title = detail_text
            if len(title) > 20:
                title = title[:20] + "…"
                stats["warnings"].append({"index": index, "type": "title_truncated",
                                          "detail": f"detail 兜底标题原长 {len(detail_text)} 字，已截到 20 字"})
            else:
                stats["warnings"].append({"index": index, "type": "detail_fallback",
                                          "detail": "desc 为空，标题取自 detail"})
        elif not title:
            stats["warnings"].append({"index": index, "type": "untitled",
                                      "detail": "desc 与 detail 均为空，输出无标题锚点"})

        if start in seen_starts:
            stats["warnings"].append({"index": index, "type": "duplicate_timestamp",
                                      "detail": f"timestamp={start} 与前面章节重复，保留顺序不合并"})
        seen_starts.add(start)

        chapters.append({"title": title, "start_ms": start, "untitled": not title})

    chapters.sort(key=lambda c: c["start_ms"])
    for i, c in enumerate(chapters):
        end = chapters[i + 1]["start_ms"] if i + 1 < len(chapters) else duration_ms
        c["end_ms"] = max(int(end), c["start_ms"])
    stats["kept"] = len(chapters)
    anchors = [f"{format_mmss(c['start_ms'])} {c['title']}".strip() for c in chapters]
    return chapters, anchors, stats


def format_mmss(ms: int) -> str:
    total = max(int(ms), 0) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def probe_subtitles(detail: Dict, duration_ms: int) -> Dict:
    """字幕可用性判定 + 段落归一化。返回 subtitle_* 四个字段，不做 IO。"""
    caption_infos, source = extract_caption_infos(detail)
    stats = {
        "present": bool(caption_infos),
        "source": source,
        "segment_count": len(caption_infos),
        "chars": 0,
        "duration_ms": duration_ms,
        "last_end_raw": None,
        "last_end": None,
        "coverage": None,
        "density": None,
        "unit": "none",
        "result": "miss",
        "reason": "",
        "raw_sample": caption_infos[:2] if caption_infos else [],
    }

    # 判据 1：字段存在且非空、文本非空
    if not caption_infos:
        stats["reason"] = "无 caption_infos 字段（平台未提供字幕）"
        return {"subtitle_segments": None, "subtitle_usable": False, "subtitle_stats": stats}

    segments = []
    for seg in caption_infos:
        if not isinstance(seg, dict):
            continue
        start, end = _segment_time(seg)
        text = str(seg.get("text") or seg.get("content") or "").strip()
        if start is None or end is None or not text:
            continue
        segments.append({"start_raw": start, "end_raw": end, "text": text})

    total_chars = sum(len(s["text"]) for s in segments)
    stats["chars"] = total_chars
    if not segments:
        stats["reason"] = "caption_infos 非空但无有效文本段落"
        return {"subtitle_segments": None, "subtitle_usable": False, "subtitle_stats": stats}
    if total_chars == 0:
        stats["reason"] = "字幕文本为空"
        return {"subtitle_segments": None, "subtitle_usable": False, "subtitle_stats": stats}

    # 单位归一：默认毫秒；只有当按毫秒算明显不合理、按秒算刚好落在片长内时才乘 1000
    # （判据：ms 覆盖率 < 0.3 且 s 覆盖率 <= 1.2，等价于 last_end <= duration/1000*1.2）
    last_end_raw = max(s["end_raw"] for s in segments)
    stats["last_end_raw"] = last_end_raw
    scale = 1
    unit = "ms"
    if duration_ms > 0 and last_end_raw > 0:
        cov_ms = last_end_raw / duration_ms
        cov_s = last_end_raw * 1000 / duration_ms
        if cov_ms < 0.3 and cov_s <= 1.2:
            scale = 1000
            unit = "s"
        elif cov_ms > 1.2:
            unit = "ms(越界)"
    stats["unit"] = unit

    for s in segments:
        s["start_ms"] = s["start_raw"] * scale if unit == "s" else s["start_raw"]
        s["end_ms"] = s["end_raw"] * scale if unit == "s" else s["end_raw"]
    segments.sort(key=lambda s: s["start_ms"])

    # 重叠处理：后段 start 早于前段 end 时，把前段截到后段 start；整段被覆盖才丢弃
    merged = []
    for seg in segments:
        if merged and seg["start_ms"] < merged[-1]["end_ms"]:
            if seg["start_ms"] <= merged[-1]["start_ms"]:
                # 整段被覆盖：若后段完全包住前段且文本更长，则用后段替换（信息量优先）
                if seg["end_ms"] >= merged[-1]["end_ms"] and len(seg["text"]) > len(merged[-1]["text"]):
                    merged[-1] = dict(seg)
                continue
            merged[-1]["end_ms"] = seg["start_ms"]
            if merged[-1]["end_ms"] <= merged[-1]["start_ms"]:
                merged.pop()
                continue
        merged.append(dict(seg))

    last_end = max(s["end_ms"] for s in merged) if merged else 0
    stats["last_end"] = last_end
    coverage = (last_end / duration_ms) if duration_ms > 0 else 0.0
    end_seconds = last_end / 1000 if last_end > 0 else 0.0
    density = (total_chars / end_seconds) if end_seconds > 0 else 0.0
    stats["coverage"] = round(coverage, 4)
    stats["density"] = round(density, 4)

    # 判据 2：覆盖到片尾 90%
    if coverage < SUBTITLE_MIN_COVERAGE:
        stats["reason"] = f"尾部截断：覆盖 {stats['coverage']} < {SUBTITLE_MIN_COVERAGE}"
        return {"subtitle_segments": None, "subtitle_usable": False, "subtitle_stats": stats}

    # 判据 3：密度 2 字/秒
    if density < SUBTITLE_MIN_DENSITY:
        stats["reason"] = f"密度过低：{stats['density']} 字/秒 < {SUBTITLE_MIN_DENSITY}"
        return {"subtitle_segments": None, "subtitle_usable": False, "subtitle_stats": stats}

    stats["result"] = "hit"
    stats["reason"] = "通过"
    out_segments = [{"start_ms": s["start_ms"], "end_ms": s["end_ms"], "text": s["text"]} for s in merged]
    return {"subtitle_segments": out_segments, "subtitle_usable": True, "subtitle_stats": stats}


def build_video_info(video_id: str, detail: Dict, sample_tag: str = "") -> Dict:
    """aweme_detail → 内部视频信息结构（纯函数：dict 进 dict 出，无 IO、无打印）"""
    author = (detail or {}).get("author") or {}
    video = (detail or {}).get("video") or {}
    duration_ms = _as_int(video.get("duration")) or 0
    candidates = media_candidates(detail)
    author_name, garbled = clean_author_name(author.get("nickname", "未知作者"))
    chapters, chapter_anchors, chapter_stats = build_chapters(detail, duration_ms)
    subtitle = probe_subtitles(detail, duration_ms)

    info = {
        "video_id": video_id,
        "title": (detail or {}).get("desc", "未知标题"),
        "author": author_name or "未知作者",
        "author_name_garbled": garbled,
        "author_id": author.get("unique_id", author.get("short_id", "")),
        "aweme_type": (detail or {}).get("aweme_type"),
        "duration": duration_ms // 1000,
        "duration_ms": duration_ms,
        "url": f"https://www.douyin.com/video/{video_id}",
        "video_url": candidates[0][0] if candidates else None,
        "media_kind": candidates[0][1] if candidates else "none",
        "media_size": candidates[0][2] if candidates else 0,
        "media_candidates": [{"url": u, "kind": k, "size": s} for u, k, s in candidates],
        "chapters": chapters,
        "chapter_anchors": chapter_anchors,
        "chapter_stats": chapter_stats,
        "subtitle_usable": subtitle["subtitle_usable"],
        "subtitle_segments": subtitle["subtitle_segments"],
        "subtitle_stats": subtitle["subtitle_stats"],
    }
    if sample_tag:
        info["sample_tag"] = sample_tag
    return info


def assemble_subtitle_text(video_info: Dict) -> str:
    """把字幕段落拼成转写全文（纯函数）"""
    segments = video_info.get("subtitle_segments") or []
    return "\n".join(s["text"] for s in segments if s.get("text"))


class DouyinAnalyzer:
    """抖音视频分析器"""

    def __init__(self):
        self.api_key = None           # 运行时从环境变量/配置文件加载，不硬编码
        self.api_key_source = ""
        self.config = {
            "api_url": "https://api.siliconflow.cn/v1/audio/transcriptions",
            "model": "FunAudioLLM/SenseVoiceSmall",
            "temp_root": _default_temp_root(),   # 每个视频一个子目录：temp_root/<视频id>
            "legacy_temp_dir": (os.environ.get(LEGACY_TEMP_ENV) or "").strip(),  # 旧版目录，只提示不删除
            "http_timeout": (20, 30),   # (连接超时, 读取超时) 秒
            "max_retries": 3,           # 网页接口重试次数
            "retry_backoff": 2.0,       # 指数退避基数（秒）
            "ytdlp_path": "yt-dlp",     # yt-dlp 可执行文件（在 PATH 中）
            "ytdlp_timeout": 120,       # yt-dlp 兜底调用超时（秒）
            "user_agents": [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
            ],
            "referers": [
                "https://www.douyin.com/",
                "https://www.douyin.com/?recommend=1",
            ],
            "browser_node_path": "node",        # node 可执行文件
            "browser_script": os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch_douyin_detail.js"),
            "browser_timeout_ms": 45000,        # 等待目标响应的超时
            "browser_hard_timeout": 180,        # node 进程硬超时（秒，单次尝试）
            "browser_channel": "auto",          # auto=先 Edge 后 Chrome；可用 --browser-channel 指定
            "browser_user_data_dir": "",        # 留空=临时 profile；填路径=复用该浏览器 profile
            "browser_cookies_path": os.path.join(CONFIG_DIR, "cookies.json"),  # 浏览器兜底 cookie 持久化
            "playwright_module": "",            # 显式指定 playwright-core 模块路径；也可用环境变量 DOUYIN_PLAYWRIGHT_MODULE
        }
        self.headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
        }

    def ensure_api_key(self, allow_prompt: bool = True) -> None:
        """确保已加载 API Key（环境变量 > 配置文件 > 交互输入）"""
        if self.api_key:
            return
        key, source = load_api_key(allow_prompt=allow_prompt)
        self.api_key = key
        self.api_key_source = source

    # ------------------------------------------------------------------
    # 临时目录（per-video）
    # ------------------------------------------------------------------

    def _temp_dir(self, video_id: str) -> str:
        """该视频专属的临时目录：temp_root/<视频id>"""
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(video_id))
        return os.path.join(self.config["temp_root"], safe)

    def _hint_legacy_temp(self) -> None:
        """旧版临时目录（Obsidian 库内）如有残留，提示用户手动处理；不自动删除"""
        d = self.config.get("legacy_temp_dir") or ""
        try:
            if not d or not os.path.isdir(d):
                return
            entries = os.listdir(d)
        except OSError:
            return
        if not entries:
            return
        print(f"提示：检测到旧版临时目录仍有 {len(entries)} 个条目: {d}")
        print(f"  新版临时目录已迁到 {self.config['temp_root']}\\<视频id>，旧目录内容不会被使用。")
        print(f'  确认无用后可手动删除: Remove-Item -Recurse -Force "{d}"')

    def _cleanup_temp_dir(self, video_id: str) -> None:
        """成功后清理该视频的临时目录，并说明清理了什么"""
        d = self._temp_dir(video_id)
        if not os.path.isdir(d):
            return
        files: List[str] = []
        total = 0
        for root, _dirs, names in os.walk(d):
            for n in names:
                p = os.path.join(root, n)
                files.append(os.path.relpath(p, d))
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
        shutil.rmtree(d, ignore_errors=True)
        if os.path.isdir(d):
            print(f"警告：临时目录未能完全删除，请手动处理: {d}")
            return
        listing = ", ".join(sorted(files)) if files else "无文件"
        print(f"已清理临时目录 {d}（{len(files)} 个文件，共 {total / 1024 / 1024:.2f} MB: {listing}）")

    # ------------------------------------------------------------------
    # 转写产物元信息（防串味）：transcript.txt 必须配 transcript.meta.json，
    # 读取前校验 video_id；不匹配一律按不存在处理并移除。
    # ------------------------------------------------------------------

    def _transcript_paths(self, video_id: str) -> Tuple[str, str]:
        d = self._temp_dir(video_id)
        return os.path.join(d, "transcript.txt"), os.path.join(d, "transcript.meta.json")

    def _write_transcript(self, video_info: Dict, transcript: str, provider: str = "siliconflow") -> str:
        """落盘转写；provider 标注来源（siliconflow / platform_subtitle），model 字段保持原样"""
        video_id = video_info["video_id"]
        transcript_path, meta_path = self._transcript_paths(video_id)
        os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript)
        meta = {
            "video_id": video_id,
            "title": video_info.get("title") or "",
            "url": video_info.get("url") or "",
            "provider": provider,
            "model": self.config["model"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "chars": len(transcript),
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return transcript_path

    def _subtitle_stats_path(self) -> str:
        return os.path.join(CONFIG_DIR, "stats", "subtitle_probe.jsonl")

    def _subtitle_hit_seen(self) -> bool:
        """历史上是否已经命中过字幕（决定单位推断打 INFO 还是 WARN）"""
        path = self._subtitle_stats_path()
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                return '"hit"' in f.read()
        except OSError:
            return False

    def report_subtitle_probe(self, video_info: Dict, sample_tag: str = "") -> None:
        """打印字幕闸门判定（判负必须可见），并把一行统计追加到 jsonl"""
        stats = video_info.get("subtitle_stats") or {}
        usable = bool(video_info.get("subtitle_usable"))
        last_end = stats.get("last_end")
        raw_last_end = stats.get("last_end_raw")
        duration_ms = stats.get("duration_ms") or 0
        coverage = stats.get("coverage")
        density = stats.get("density")
        reason = stats.get("reason") or ""

        if usable:
            print(f"[字幕探测] 命中：单位={stats.get('unit')} 覆盖={coverage} 密度={density} 字/秒 "
                  f"（raw_last_end={raw_last_end} duration_ms={duration_ms}）")
            if not self._subtitle_hit_seen():
                print(f"[字幕探测] 首次命中，原始 caption_infos 前两条（来源 {stats.get('source')}）：")
                for seg in (stats.get("raw_sample") or [])[:2]:
                    print("  RAW " + json.dumps(seg, ensure_ascii=False)[:600])
            elif stats.get("unit") == "s":
                print(f"WARN [字幕探测] 单位自适应走兜底：last_end_raw={raw_last_end} duration_ms={duration_ms}，"
                      f"判为秒并乘 1000（已有历史命中，此为兜底路径）")
        else:
            print(f"[字幕探测] 未命中：raw_last_end={raw_last_end} duration_ms={duration_ms} "
                  f"coverage={coverage} density={density} 单位={stats.get('unit')}｜判否原因：{reason}")
            if stats.get("unit") == "s":
                print(f"WARN [字幕探测] 单位自适应走兜底：last_end_raw={raw_last_end} duration_ms={duration_ms}，判为秒并乘 1000")

        stats_ch = video_info.get("chapter_stats") or {}
        for dropped in stats_ch.get("dropped") or []:
            print(f"WARN [章节] 丢弃第 {dropped.get('index')} 条（timestamp={dropped.get('timestamp')}，"
                  f"desc={dropped.get('desc')!r}）：{dropped.get('reason')}")
        for warn in stats_ch.get("warnings") or []:
            print(f"WARN [章节] 第 {warn.get('index')} 条：{warn.get('type')}｜{warn.get('detail')}")

        if video_info.get("chapters"):
            anchors = video_info.get("chapter_anchors") or []
            print(f"[章节] 原始 {stats_ch.get('raw_count')} 条，保留 {stats_ch.get('kept')} 条："
                  + " / ".join(f">{a}" for a in anchors))
        elif stats_ch.get("raw_count"):
            print(f"[章节] 原始 {stats_ch.get('raw_count')} 条，全部丢弃，正文退回单段结构")
        else:
            print("[章节] 无 chapter_list，正文退回单段结构")

        record = {
            "aweme_id": video_info.get("video_id"),
            "aweme_type": video_info.get("aweme_type"),
            "duration": duration_ms,
            "last_end": last_end,
            "coverage": coverage,
            "density": density,
            "result": stats.get("result"),
            "unit": stats.get("unit"),
            "reason": reason,
            "chars": stats.get("chars"),
            "segment_count": stats.get("segment_count"),
            "source": stats.get("source"),
            "sample_tag": sample_tag or "",
            "at": datetime.now(timezone.utc).isoformat(),
        }
        path = self._subtitle_stats_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"警告：字幕探测统计写入失败（{e}）")

    def _load_reusable_transcript(self, video_info: Dict) -> Optional[str]:
        """同一视频重跑时复用既有转写；元信息缺失或不匹配则移除并按不存在处理"""
        video_id = video_info["video_id"]
        transcript_path, meta_path = self._transcript_paths(video_id)
        if not os.path.exists(transcript_path):
            return None

        meta = None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = None

        if not isinstance(meta, dict) or meta.get("video_id") != video_id:
            print("警告：发现无法确认归属的转写文件（元信息缺失或 video_id 不匹配），")
            print("  按不存在处理并移除，绝不复用别的视频的转写，将重新转写。")
            for p in (transcript_path, meta_path):
                try:
                    os.remove(p)
                except OSError:
                    pass
            return None

        try:
            with open(transcript_path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(f"警告：既有转写读取失败（{e}），将重新转写")
            return None

        old_title = meta.get("title") or ""
        new_title = video_info.get("title") or ""
        if old_title and new_title and old_title != new_title:
            print(f"注意：既有转写的标题与当前获取到的不一致（同一视频，可能标题被作者修改）:")
            print(f"  {old_title!r} -> {new_title!r}")
        print(f"复用同一视频（id={video_id}）的既有转写（{len(text)} 字，生成于 {meta.get('created_at', '?')}），跳过下载与转写")
        return text

    # ------------------------------------------------------------------

    def _clean_author_name(self, name: str) -> Optional[str]:
        """清理作者名字（薄封装，打印告警；判定逻辑在纯函数 clean_author_name）"""
        original = name
        name, garbled = clean_author_name(name)
        if garbled:
            print(f"警告：作者名字可能有乱码: {original}")
        return name

    def parse_douyin_url(self, url: str) -> Optional[str]:
        """解析抖音短链接获取视频ID"""
        try:
            response = requests.head(url, allow_redirects=True, headers=self.headers, timeout=10)
            final_url = response.url

            if "/video/" in final_url:
                video_id = final_url.split("/video/")[-1].split("?")[0]
                # 移除末尾的斜杠
                video_id = video_id.rstrip("/")
                return video_id

            return None
        except Exception as e:
            print(f"解析链接失败: {e}")
            return None

    def _pick_headers(self, attempt: int = 1):
        """轮换 UA/Referer，降低被固定指纹拦截的概率"""
        uas = self.config["user_agents"]
        referers = self.config["referers"]
        return uas[(attempt - 1) % len(uas)], referers[(attempt - 1) % len(referers)]

    def _media_candidates(self, detail: Dict):
        """薄封装：候选链逻辑在纯函数 media_candidates"""
        return media_candidates(detail)

    def _build_video_info(self, video_id: str, detail: Dict) -> Dict:
        """薄封装：映射与字幕/章节判定都在纯函数 build_video_info"""
        return build_video_info(video_id, detail)

    def _is_unretryable(self, status_code: int, body: str) -> bool:
        """身份校验类拦截（Argus 风控 / 401 / 403）：重试无意义，直接降级"""
        if status_code in (401, 403):
            return True
        return any(m in (body or "") for m in ("ArgusSecurityPlugin", "Uifid Not Found", "uifid"))

    def _fetch_detail_via_web(self, video_id: str):
        """调用抖音 web 接口取 aweme_detail，返回 (detail, 失败原因)"""
        api_url = f"https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id={video_id}"
        last_reason = "未知原因"

        for attempt in range(1, self.config["max_retries"] + 1):
            ua, referer = self._pick_headers(attempt)
            headers = {"User-Agent": ua, "Referer": referer}
            try:
                response = requests.get(
                    api_url, headers=headers, timeout=self.config["http_timeout"]
                )
                body = response.text or ""
                snippet = body[:200]

                if self._is_unretryable(response.status_code, body):
                    last_reason = f"HTTP {response.status_code}，被身份校验拦截（不可重试）"
                    print(f"[web接口] {last_reason}，响应体前200字: {snippet!r}")
                    if response.status_code != 200:
                        try:
                            response.raise_for_status()
                        except requests.RequestException as e:
                            print(f"[web接口] raise_for_status: {e}")
                    break
                if response.status_code != 200:
                    last_reason = f"HTTP {response.status_code}（第 {attempt} 次）"
                    print(f"[web接口] {last_reason}，响应体前200字: {snippet!r}")
                    try:
                        response.raise_for_status()
                    except requests.RequestException as e:
                        print(f"[web接口] raise_for_status: {e}")
                elif not body.strip():
                    last_reason = f"HTTP {response.status_code}，响应体为空（第 {attempt} 次）"
                    print(f"[web接口] {last_reason}，响应体前200字: {snippet!r}")
                else:
                    try:
                        data = response.json()
                    except ValueError as e:
                        last_reason = f"HTTP {response.status_code}，JSON 解析失败: {e}（第 {attempt} 次）"
                        print(f"[web接口] {last_reason}，响应体前200字: {snippet!r}")
                    else:
                        detail = data.get("aweme_detail") or {}
                        if detail:
                            return detail, ""
                        last_reason = f"HTTP {response.status_code}，aweme_detail 为空（第 {attempt} 次）"
                        print(f"[web接口] {last_reason}，响应体前200字: {snippet!r}")
            except Exception as e:
                last_reason = f"请求异常: {e}（第 {attempt} 次）"
                print(f"[web接口] {last_reason}")

            if attempt < self.config["max_retries"]:
                wait = self.config["retry_backoff"] * (2 ** (attempt - 1))
                print(f"[web接口] {wait:.1f} 秒后重试...")
                time.sleep(wait)

        return None, last_reason

    def _fetch_info_via_ytdlp(self, video_id: str):
        """yt-dlp 兜底：一次调用取元信息，返回 (video_info, 失败原因)"""
        url = f"https://www.douyin.com/video/{video_id}"
        cmd = [self.config["ytdlp_path"], "--dump-single-json", "--no-warnings", "-f", "bestaudio/best", url]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=self.config["ytdlp_timeout"]
            )
        except FileNotFoundError:
            return None, f"未找到 yt-dlp（{self.config['ytdlp_path']}）"
        except subprocess.TimeoutExpired:
            return None, "yt-dlp 调用超时"
        except Exception as e:
            return None, f"yt-dlp 调用异常: {e}"

        if proc.returncode != 0:
            stderr_text = (proc.stderr or b"").decode("utf-8", "replace").strip()
            tail = " / ".join(stderr_text.splitlines()[-3:]) or "无 stderr 输出"
            return None, f"yt-dlp 退出码 {proc.returncode}: {tail}"

        try:
            info = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
        except Exception as e:
            return None, f"yt-dlp 输出解析失败: {e}"

        return {
            "video_id": video_id,
            "title": info.get("title") or "未知标题",
            "author": self._clean_author_name(info.get("uploader")) or "未知作者",
            "author_id": info.get("uploader_id") or "",
            "duration": int(info.get("duration") or 0),
            "url": info.get("webpage_url") or url,
            "video_url": info.get("url") or None,
            "media_kind": "ytdlp-" + str(info.get("ext") or "?"),
        }, ""

    def _browser_stage_text(self, stage: str) -> str:
        """把浏览器兜底脚本返回的阶段名翻译成中文"""
        return {
            "missing-playwright": "缺少 playwright 依赖",
            "launch": "启动浏览器失败",
            "navigate": "页面加载失败或超时",
            "timeout": "未捕获到目标响应",
            "parse": "响应 JSON 解析失败",
            "empty-detail": "捕获到响应但 aweme_detail 为空",
        }.get(stage, f"浏览器兜底失败（{stage}）")

    def _cookies_usable(self) -> bool:
        """持久化 cookie 里是否有未过期的 s_v_web_id 和 ttwid（只看名称和过期时间，不读值）"""
        path = self.config["browser_cookies_path"]
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        if not isinstance(data, list):
            return False
        now = time.time()
        names = set()
        for c in data:
            if not isinstance(c, dict):
                continue
            exp = c.get("expires")
            if isinstance(exp, (int, float)) and exp > 0 and exp <= now:
                continue
            name = c.get("name")
            if isinstance(name, str):
                names.add(name)
        return {"s_v_web_id", "ttwid"} <= names

    def _run_browser_capture(self, video_id: str, headless: bool):
        """调一次 node 浏览器脚本，返回 (detail, 实际channel, 失败原因)"""
        script = self.config["browser_script"]
        if not os.path.exists(script):
            return None, "", f"启动浏览器失败: 缺少脚本 {script}"

        cmd = [
            self.config["browser_node_path"], script,
            f"https://www.douyin.com/video/{video_id}",
            "--timeout", str(self.config["browser_timeout_ms"]),
            "--browser-channel", self.config["browser_channel"],
            "--cookies-file", self.config["browser_cookies_path"],
        ]
        if headless:
            cmd.append("--headless")
        if self.config["browser_user_data_dir"]:
            cmd += ["--user-data-dir", self.config["browser_user_data_dir"]]
        if self.config["playwright_module"]:
            cmd += ["--playwright-module", self.config["playwright_module"]]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=self.config["browser_hard_timeout"]
            )
        except FileNotFoundError:
            return None, "", f"启动浏览器失败: 未找到 node（{self.config['browser_node_path']}）"
        except subprocess.TimeoutExpired:
            return None, "", "启动浏览器失败: node 进程超时"
        except Exception as e:
            return None, "", f"启动浏览器失败: {e}"
        finally:
            # cookie 文件落盘后收紧权限（含会话标识，只保留当前用户可读）
            cookies_path = self.config["browser_cookies_path"]
            if os.path.exists(cookies_path):
                _harden_permissions(cookies_path)

        stderr_text = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if stderr_text:
            print(f"[浏览器兜底] node stderr 末尾: {' / '.join(stderr_text.splitlines()[-3:])}")

        stdout_text = (proc.stdout or b"").decode("utf-8", "replace").strip()
        if not stdout_text:
            return None, "", f"浏览器兜底无输出（node 退出码 {proc.returncode}）"
        try:
            payload = json.loads(stdout_text.splitlines()[-1])
        except Exception as e:
            return None, "", f"浏览器兜底输出不是 JSON: {e}"

        if payload.get("ok") and payload.get("detail"):
            return payload["detail"], str(payload.get("channel") or ""), ""

        stage = payload.get("stage") or "unknown"
        err = payload.get("error") or "无错误信息"
        return None, "", f"{self._browser_stage_text(stage)}: {err}"

    def _fetch_detail_via_browser(self, video_id: str):
        """浏览器兜底：复用 cookie 时先试无头（不弹窗），失败再退有头窗口"""
        attempts: List[Tuple[bool, str]] = []
        if self._cookies_usable():
            attempts.append((True, "无头+复用cookie"))
        attempts.append((False, "有头窗口"))

        reasons = []
        for headless, label in attempts:
            print(f"[浏览器兜底] 尝试{label}模式（channel={self.config['browser_channel']}）")
            detail, channel, reason = self._run_browser_capture(video_id, headless)
            if detail:
                print(f"[浏览器兜底] 已捕获 aweme detail（{label}，浏览器: {channel or '未知'}）")
                return detail, ""
            print(f"[浏览器兜底] {label}模式失败：{reason}")
            reasons.append(f"{label}模式: {reason}")

        return None, "；".join(reasons)

    def _dump_raw_detail(self, video_id: str, detail: Dict) -> None:
        """把捕获到的原始 aweme_detail 存到该视频的临时目录，方便核对媒体流真实结构"""
        try:
            d = self._temp_dir(video_id)
            os.makedirs(d, exist_ok=True)
            raw_path = os.path.join(d, "detail_raw.json")
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump({"video_id": video_id, "detail": detail}, f, ensure_ascii=False, indent=2)
            print(f"原始详情已保存到: {raw_path}")
        except Exception as e:
            print(f"保存原始详情失败（不影响主流程）: {e}")

    def get_video_info(self, video_id: str, use_browser: bool = True) -> Optional[Dict]:
        """获取视频元信息：web 接口 → yt-dlp → 浏览器兜底（--no-browser 关闭）"""
        detail, reason = self._fetch_detail_via_web(video_id)
        if detail:
            self._dump_raw_detail(video_id, detail)
            return self._build_video_info(video_id, detail)

        print(f"[get_video_info] 网页接口失败（{reason}），改用 yt-dlp 兜底")

        info, ytdlp_reason = self._fetch_info_via_ytdlp(video_id)
        if info:
            print("[get_video_info] yt-dlp 兜底成功")
            return info

        print(f"[get_video_info] yt-dlp 兜底也失败（{ytdlp_reason}）")

        if not use_browser:
            print("[get_video_info] 已用 --no-browser 关闭浏览器兜底")
            return None

        print("[get_video_info] 改用浏览器兜底")
        detail, browser_reason = self._fetch_detail_via_browser(video_id)
        if detail:
            print("[get_video_info] 浏览器兜底成功")
            self._dump_raw_detail(video_id, detail)
            return self._build_video_info(video_id, detail)

        print(f"[get_video_info] 浏览器兜底也失败（{browser_reason}）")
        return None

    def download_video(self, video_info: Dict) -> Optional[str]:
        """按候选顺序下载媒体：音频流 → 最低码率视频档 → play_addr，一个失败自动换下一个"""
        video_id = video_info["video_id"]
        try:
            os.makedirs(self._temp_dir(video_id), exist_ok=True)
        except Exception as e:
            print(f"创建临时目录失败: {e}")
            return None

        candidates = video_info.get("media_candidates")
        if not candidates and video_info.get("video_url"):
            candidates = [{"url": video_info["video_url"], "kind": "single"}]
        if not candidates:
            print("没有可用的下载地址")
            return None

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
        }
        video_path = os.path.join(self._temp_dir(video_id), "video.mp4")

        for idx, cand in enumerate(candidates, 1):
            url = cand.get("url")
            if not url:
                continue
            print(f"下载媒体中（候选 {idx}/{len(candidates)}，{cand.get('kind', '?')}）...")
            try:
                response = requests.get(url, headers=headers, stream=True, timeout=(20, 60))
                ctype = response.headers.get("Content-Type", "")
                if "text/html" in ctype:
                    print(f"候选 {idx} 失败: 返回的是网页而不是媒体（Content-Type: {ctype}）")
                    continue
                with open(video_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                file_size = os.path.getsize(video_path) / 1024 / 1024
                if file_size <= 0:
                    print(f"候选 {idx} 失败: 下载内容为空")
                    continue
                print(f"媒体下载完成（{cand.get('kind', '?')}）: {file_size:.2f} MB")
                return video_path
            except Exception as e:
                print(f"候选 {idx} 失败: {e}")
                continue
        print("所有候选下载地址都失败")
        return None

    def transcribe_video(self, video_path: str, video_info: Dict) -> Optional[str]:
        """使用SiliconFlow API转写视频；成功才落盘 transcript.txt + transcript.meta.json"""
        self.ensure_api_key()
        delays = (0, 5, 15)  # 最多 3 次尝试，间隔 5s / 15s，应对上传时的间歇性 TLS 断连
        last_error = ""
        for attempt, delay in enumerate(delays, 1):
            if delay:
                print(f"转写重试 {attempt}/3：{delay} 秒后重试（上次失败: {last_error}）")
                time.sleep(delay)
            try:
                print(f"上传文件到SiliconFlow API...（第 {attempt}/3 次）")

                headers = {
                    "Authorization": f"Bearer {self.api_key}"
                }

                with open(video_path, "rb") as f:
                    files = {"file": ("video.mp4", f, "video/mp4")}
                    data = {"model": self.config["model"]}

                    response = requests.post(
                        self.config["api_url"],
                        headers=headers,
                        files=files,
                        data=data,
                        timeout=600
                    )

                result = response.json()

                if "text" in result:
                    transcript = result["text"]
                    print(f"转写完成，共 {len(transcript)} 字")

                    transcript_path = self._write_transcript(video_info, transcript)
                    print(f"转写内容已保存到: {transcript_path}")
                    self._save_transcript_copy(video_info, transcript)

                    return transcript
                last_error = f"API 返回无 text 字段: {str(result)[:200]}"
                print(f"转写失败（第 {attempt}/3 次）: {last_error}")
            except Exception as e:
                last_error = repr(e)
                print(f"转写失败（第 {attempt}/3 次）: {e}")
        print("转写失败：已尝试 3 次")
        return None

    def _save_transcript_copy(self, video_info: Dict, transcript: str) -> None:
        """成功后额外把转写副本留在 ~/.douyin-skill/transcripts/<video_id>.txt，
        避免临时目录被清理后转写丢失。"""
        try:
            copy_dir = os.path.join(CONFIG_DIR, "transcripts")
            os.makedirs(copy_dir, exist_ok=True)
            copy_path = os.path.join(copy_dir, f"{video_info['video_id']}.txt")
            with open(copy_path, "w", encoding="utf-8") as f:
                f.write(transcript)
            print(f"转写副本已保存到: {copy_path}")
        except Exception as e:
            print(f"保存转写副本失败（不影响主流程）: {e}")

    def save_video_info(self, video_info: Dict) -> str:
        """保存视频信息到该视频的临时目录"""
        info_path = os.path.join(self._temp_dir(video_info["video_id"]), "video_info.json")
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(video_info, f, ensure_ascii=False, indent=2)
        print(f"视频信息已保存到: {info_path}")
        return info_path

    def analyze(self, url: str, use_browser: bool = True, keep_temp: bool = False,
                sample_tag: str = "") -> Optional[Dict]:
        """
        分析视频（只下载和转写，不生成笔记）

        Args:
            url: 抖音视频链接
            use_browser: 是否允许浏览器兜底（默认True；--no-browser 关闭）
            keep_temp: 成功后仍保留该视频的临时目录（默认自动清理；失败时始终保留）

        Returns:
            包含视频信息和转写内容的字典
        """
        print(f"\n{'='*60}")
        print(f"开始分析视频: {url}")
        print(f"{'='*60}\n")

        self._hint_legacy_temp()

        video_id = None
        success = False
        try:
            # 0. 提前检查 API Key，避免下载完大文件后才发现缺少 Key
            print("[0/4] 检查 SiliconFlow API Key...")
            try:
                self.ensure_api_key()
            except ApiKeyError as e:
                print(str(e))
                return None
            print(f"API Key 已就绪（来源: {self.api_key_source}，内容不显示）")

            # 1. 解析链接
            print("[1/4] 解析抖音链接...")
            video_id = self.parse_douyin_url(url)
            if not video_id:
                print("无法解析链接")
                return None
            print(f"视频ID: {video_id}")
            print(f"临时目录: {self._temp_dir(video_id)}")

            # 2. 获取视频信息
            print("\n[2/4] 获取视频信息...")
            video_info = self.get_video_info(video_id, use_browser=use_browser)
            if not video_info:
                print("无法获取视频信息")
                return None
            print(f"视频标题: {video_info['title']}")
            print(f"作者: {video_info['author']}")
            print(f"时长: {video_info['duration']}秒")
            media_size = video_info.get("media_size") or 0
            if media_size > 0:
                print(f"媒体流: {video_info.get('media_kind', 'unknown')}（约 {media_size / 1024 / 1024:.1f} MB）")
            else:
                print(f"媒体流: {video_info.get('media_kind', 'unknown')}")

            # 字幕闸门 + 章节：判定全部在纯函数里完成，这里只读布尔值并落统计
            self.report_subtitle_probe(video_info, sample_tag=sample_tag)

            # 3+4. 字幕可用则直接走平台字幕；否则下载并转写（同一视频重跑时复用既有转写）
            if video_info.get("subtitle_usable"):
                transcript = assemble_subtitle_text(video_info)
                print("\n[3/4] 跳过下载：使用平台自带字幕")
                print("[4/4] 跳过转写：使用平台自带字幕")
                self._write_transcript(video_info, transcript, provider="platform_subtitle")
            else:
                transcript = self._load_reusable_transcript(video_info)
            if transcript is None:
                print("\n[3/4] 下载媒体...")
                video_path = self.download_video(video_info)
                if not video_path:
                    print("媒体下载失败")
                    return None

                print("\n[4/4] 语音转写...")
                transcript = self.transcribe_video(video_path, video_info)
                if not transcript:
                    print("语音转写失败")
                    return None
            elif not video_info.get("subtitle_usable"):
                print("\n[3/4] 跳过下载：复用既有转写")
                print("[4/4] 跳过转写：复用既有转写")

            # 保存视频信息
            self.save_video_info(video_info)

            print(f"\n{'='*60}")
            print(f"下载和转写完成!")
            print(f"{'='*60}")
            # 临时目录随后会被自动清理（--keep 除外），把转写全文和视频信息
            # 打到 stdout 标记块里，清理之后 AI 仍可直接读取。
            print(f"========== TRANSCRIPT BEGIN (video_id={video_id}) ==========")
            print(transcript)
            print("========== TRANSCRIPT END ==========")
            print("========== VIDEO_INFO_JSON BEGIN ==========")
            print(json.dumps(video_info, ensure_ascii=False, indent=2))
            print("========== VIDEO_INFO_JSON END ==========")

            success = True
            return {
                "video_info": video_info,
                "transcript": transcript,
                "temp_dir": self._temp_dir(video_id),
                "temp_kept": keep_temp,
            }
        finally:
            if video_id:
                d = self._temp_dir(video_id)
                if success and not keep_temp:
                    self._cleanup_temp_dir(video_id)
                elif os.path.isdir(d):
                    reason = "已成功，按 --keep 保留" if success else "本次失败，保留供排查"
                    print(f"临时目录已保留（{reason}）: {d}")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抖音视频分析器：下载媒体并用 SiliconFlow API 转写（不生成笔记）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            f"API Key 读取顺序：环境变量 {API_KEY_ENV} > 配置文件 {CONFIG_PATH} > 首次运行交互输入。\n"
            "查看当前来源：--check-key；更换 Key：--set-key（建议省略值，交互输入）。\n"
            f"笔记目录：环境变量 {NOTES_DIR_ENV} > 配置文件 notes_dir；可用 --set-notes-dir 保存。\n"
            "浏览器兜底预检：node scripts/fetch_douyin_detail.js --check（真实拉起 Edge/Chrome 验证）。"
        ),
    )
    parser.add_argument("url", nargs="?", help="抖音视频链接")
    parser.add_argument("--no-browser", action="store_true",
                        help="关闭浏览器兜底（默认开启：web 接口 → yt-dlp → 本机 Edge/Chrome）")
    parser.add_argument("--use-browser", action="store_true",
                        help="兼容参数：强制开启浏览器兜底")
    parser.add_argument("--browser-channel", choices=["auto", "msedge", "chrome"], default=None,
                        help="浏览器兜底用哪个浏览器：auto=先 Edge 后 Chrome（默认）；msedge/chrome=只用指定的")
    parser.add_argument("--sample-tag", default="",
                        help="人工分层采样标签（可选），仅写入字幕探测统计，便于日后按类型分析命中率")
    parser.add_argument("--playwright-module", default=None, metavar="PATH",
                        help="显式指定 playwright-core 模块路径（也可用环境变量 DOUYIN_PLAYWRIGHT_MODULE）")
    parser.add_argument("--keep", action="store_true",
                        help="成功后仍保留该视频的临时目录（默认自动清理；失败时始终保留供排查）")
    parser.add_argument("--set-key", nargs="?", const="", metavar="KEY",
                        help="保存/替换 SiliconFlow API Key；省略值则交互输入（不落命令历史）")
    parser.add_argument("--check-key", action="store_true",
                        help="只检查 API Key 来源，不显示 Key 内容")
    parser.add_argument("--set-notes-dir", default=None, metavar="DIR",
                        help="保存笔记输出目录（AI 生成笔记时写入的位置）")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.set_key is not None:
        key = args.set_key.strip()
        if not key:
            if _stdin_is_interactive():
                try:
                    key = getpass.getpass("请输入新的 SiliconFlow API Key（直接回车取消）: ").strip()
                except (EOFError, KeyboardInterrupt):
                    key = ""
            else:
                key = (os.environ.get(API_KEY_ENV) or "").strip()
            if not key:
                print("未提供新的 API Key。")
                print(f'  终端下运行：python "{os.path.abspath(__file__)}" --set-key')
                print(f"  或先设置环境变量 {API_KEY_ENV} 再运行 --set-key")
                return 1
        try:
            path = save_api_key(key)
        except ApiKeyError as e:
            print(str(e))
            return 1
        print(f"API Key 已保存到: {path}")
        print("查看当前来源：--check-key")
        return 0

    if args.check_key:
        try:
            _, source = load_api_key(allow_prompt=False)
        except ApiKeyError as e:
            print(str(e))
            return 1
        print(f"API Key 已配置，来源: {source}")
        print("（为避免泄露，不显示 Key 内容）")
        return 0

    if args.set_notes_dir is not None:
        try:
            path = save_notes_dir(args.set_notes_dir)
        except ApiKeyError as e:
            print(str(e))
            return 1
        print(f"笔记目录已保存: {path}")
        return 0

    if not args.url:
        parser.print_help()
        return 1

    use_browser = args.use_browser or not args.no_browser
    analyzer = DouyinAnalyzer()
    if args.browser_channel:
        analyzer.config["browser_channel"] = args.browser_channel
    if args.playwright_module:
        analyzer.config["playwright_module"] = args.playwright_module
    result = analyzer.analyze(args.url, use_browser=use_browser, keep_temp=args.keep,
                              sample_tag=args.sample_tag)

    if result:
        print(f"\n分析完成!")
        print(f"视频标题: {result['video_info']['title']}")
        if result["temp_kept"]:
            print(f"临时目录（已保留）: {result['temp_dir']}")
        else:
            print("临时目录已自动清理；转写全文与视频信息见上方 TRANSCRIPT / VIDEO_INFO_JSON 输出块。")
        notes_dir = resolve_notes_dir()
        if notes_dir:
            print(f"笔记目录: {notes_dir}")
        else:
            print(f"笔记目录未配置：请用 --set-notes-dir <目录> 或环境变量 {NOTES_DIR_ENV} 指定")
        print("")
        print("下一步：AI 读取转写内容，手动分析并生成详细笔记")
        return 0

    print("\n分析失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
