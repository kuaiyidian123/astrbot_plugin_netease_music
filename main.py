"""
astrbot_plugin_netease_music - 网易云点歌插件
基于 AstrBot Star 插件框架 + 直连网易云 HTTP API（无需任何第三方音乐库）

已验证可用的 API:
- 搜索:   GET  /api/cloudsearch/pc
- 获取音频: POST /api/song/enhance/player/url/v1
- 登录:   浏览器 Cookie 导入（绕开加密接口限制）
"""

import os
import io
import re
import json
import time
import html
import random
import hashlib
import asyncio
import tempfile
import shutil
import zipfile
import urllib.parse
from io import BytesIO
from typing import Dict, List, Optional, Any

# ============= AstrBot API =============
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.message_components import Plain, Image, Record, File, Video
from astrbot.api import logger

try:
    from astrbot.api.message_components import At
except ImportError:  # 兼容不支持 At 组件的版本
    At = None

# ============= 第三方依赖 =============
import aiohttp
from PIL import Image as PILImage

# ============= 内部模块 =============
from .draw import (
    draw_search_result_image,
    draw_playlist_image,
    draw_comments_image,
    draw_help_image,
    draw_binding_image,
    draw_playlist_list_image,
    draw_playlist_usage_image,
    draw_bilibili_videos_image,
    draw_playlist_search_image,
    draw_playlist_songs_image,
    SEARCH_PAGE_SIZE,
    SEARCH_IMG_EXT,
    HELP_IMG_EXT,
    parse_image_size,
)


# ============ NovelAI 生图（/生图） ============
NAI_DEFAULT_API_URL = "https://image.novelai.net/ai/generate-image"
# 中文描述 -> NovelAI 英文绘画 tag 的系统提示词（规则来自使用者指定）
NAI_TAG_SYSTEM_PROMPT = (
    "你是NovelAI绘画tag生成器，把下面中文描述转换成英文绘画关键词，逗号分隔。\n"
    "输出格式：\n"
    "正向tag：xxx\n"
    "负面tag：xxx\n"
    "规则：\n"
    "1. 只输出绘画关键词，不要完整长句子\n"
    "2. 二次元插画风格，masterpiece, best quality放最前面\n"
    "3. 负面词固定包含：lowres, bad anatomy, bad hands, extra limbs, deformed, blurry, ugly\n"
    "4. 严格只输出上面两行，「正向tag：」「负面tag：」这两个标签要原样保留，不要改写成别的词\n"
    "5. 不要输出任何解释、不要用 markdown 或代码块、不要反问\n"
    "6. 即使描述不是一个具体画面，也要转成最接近的绘画关键词，不要拒绝\n"
)
# 中文描述 -> NovelAI 英文绘画 tag 的**用户消息**模板
# 同一套规则在 system 与 user 里各说一遍：部分中转/模型会忽略或不重视 system 提示词，
# 只靠 system_prompt 时会答非所问，导致格式解析失败。
NAI_TAG_USER_TEMPLATE = (
    "请把下面的中文描述转成英文绘画 tag。\n"
    "只输出两行，格式必须是（不要解释、不要 markdown、不要代码块）：\n"
    "正向tag：<英文关键词，逗号分隔>\n"
    "负面tag：<英文关键词，逗号分隔>\n"
    "中文描述：{text}"
)
# 固定必须出现的负面词（大模型漏掉时自动补齐）
NAI_REQUIRED_NEGATIVE = [
    "lowres", "bad anatomy", "bad hands", "extra limbs", "deformed", "blurry", "ugly",
]
# 生图总像素上限（超过等比缩小，避免超出 NovelAI 单图上限被拒）
NAI_MAX_PIXELS = 1024 * 3072
NAI_SAMPLER_OPTIONS = [
    "k_euler_ancestral", "k_euler", "k_dpmpp_2m", "k_dpmpp_2s_ancestral",
    "k_dpmpp_sde", "ddim", "k_heun", "k_dpm_2", "k_dpm_2_ancestral",
]
NAI_REQUEST_TIMEOUT = 180          # 生图请求总超时（秒）
NAI_MAX_IMAGE_BYTES = 32 * 1024 * 1024  # 单次响应体上限（32MB），防止异常响应撑爆内存
MAX_NAI_PROMPT_CHARS = 500         # 用户输入的描述长度上限


# ============ 吐司（TAMS / tusi.cn）生图 ============
TUSI_DEFAULT_BASE_URL = "https://cn.tensorart.net"
# 模板里提示词/负面词字段的自动识别关键词（可用配置项手动指定覆盖）
TUSI_PROMPT_KEYS = ("prompt", "提示词", "提示语", "关键词", "描述")
TUSI_NEGATIVE_KEYS = ("negative", "负面", "反向", "负向")
TUSI_API_TIMEOUT = 60                   # 单次接口请求超时（秒）
TUSI_JOB_TIMEOUT = 300                  # 提交后等待作业完成的整体上限（秒）
TUSI_POLL_INTERVAL = 4                  # 作业状态轮询间隔（秒）
TUSI_MAX_JSON_BYTES = 2 * 1024 * 1024   # 接口 JSON 响应上限
TUSI_MAX_IMAGE_BYTES = 32 * 1024 * 1024  # 结果图片上限


def _split_tusi_prefix(text: str) -> tuple:
    """识别「/生图 吐司 xxx」中的渠道前缀，返回 (是否走吐司, 剩余描述)

    「吐司」后必须跟空白或分隔符才算前缀，避免「吐司面包」这类描述被误判。
    """
    raw = (text or "").strip()
    m = re.match(r"^吐司(?:\s+|[,，:：]+\s*)(.+)$", raw)
    if m:
        return True, m.group(1).strip()
    if raw == "吐司":
        return True, ""
    return False, raw


def _sniff_image_suffix(data: bytes) -> str:
    """按文件头判断图片真实格式，避免把 JPEG/WebP 存成 .png 导致平台发送失败"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"GIF":
        return ".gif"
    return ".png"


def _clean_tag_line(s: str) -> str:
    """清理大模型输出的一行 tag：去掉 markdown 装饰与首尾标点

    注意 `*` 与反引号一定不属于 tag，直接全行剔除（能修好 `**正向tag**：` 这类
    夹在标签与冒号之间的加粗符号）；下划线与连字符可能是 tag 的一部分，保留。
    """
    s = str(s or "").replace("*", "").replace("`", "")
    s = re.sub(r"^[\s#>+\-|]+", "", s)
    s = re.sub(r"[\s|]+$", "", s)
    return s.strip(" ,，;；.")


# 「正向tag / 负面tag」的标签行（冒号可有可无，内容也可能落在下一行）
_TAG_LABEL_NEG = re.compile(
    r"^(?:负面|反向|负向|negative)\s*(?:tags?|标签|关键词|prompt)?\s*[：:、]?\s*(.*)$",
    re.IGNORECASE,
)
_TAG_LABEL_POS = re.compile(
    r"^(?:正向|正面|positive|prompt)\s*(?:tags?|标签|关键词|prompt)?\s*[：:、]?\s*(.*)$",
    re.IGNORECASE,
)


def _match_tag_label(line: str) -> tuple:
    """判断一行是否为标签行，返回 ("pos"/"neg", 同行内容)；不是标签行返回 (None, "")"""
    m = _TAG_LABEL_NEG.match(line)
    if m:
        return "neg", m.group(1).strip()
    m = _TAG_LABEL_POS.match(line)
    if m:
        return "pos", m.group(1).strip()
    return None, ""


def _looks_like_tag_list(text: str) -> bool:
    """判断整段文本是否像一串英文绘画 tag（模型没写标签时的兜底依据）"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s or len(s) > 400 or "," not in s:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
    return cjk / len(s) <= 0.1


def _parse_nai_size(value, default: tuple = (832, 1216)) -> tuple:
    """解析生图尺寸 "宽x高"（自动对齐到 64 的倍数，并限制总像素），非法时返回默认值"""
    text = str(value or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{2,5})[x*×](\d{2,5})", text)
    if not m:
        return default
    w = max(64, (int(m.group(1)) + 63) // 64 * 64)
    h = max(64, (int(m.group(2)) + 63) // 64 * 64)
    if w * h > NAI_MAX_PIXELS:
        ratio = (NAI_MAX_PIXELS / (w * h)) ** 0.5
        w = max(64, int(w * ratio) // 64 * 64)
        h = max(64, int(h * ratio) // 64 * 64)
    return (w, h)


def _describe_llm_error(err: Exception) -> str:
    """把对话模型的报错转成可读提示（额度不足/鉴权失败/限流等常见原因单独点出）"""
    detail = " ".join(str(err or "").split())
    low = detail.lower()
    if "balance" in low or "insufficient" in low or "quota" in low or "欠费" in detail:
        tip = "原因：模型额度不足（insufficient balance），请充值或换一个对话模型"
    elif "401" in detail or "unauthorized" in low or "invalid api key" in low:
        tip = "原因：模型鉴权失败（API Key 无效或已过期）"
    elif "403" in detail or "forbidden" in low or "permission" in low:
        tip = "原因：模型拒绝了本次请求（403）"
    elif "429" in detail or "rate limit" in low or "too many requests" in low:
        tip = "原因：模型请求过于频繁（被限流），请稍后再试"
    elif "timeout" in low or "timed out" in low:
        tip = "原因：调用模型超时，请稍后重试"
    else:
        tip = "原因：调用模型时出错"
    if detail:
        tip += f"\n原始错误：{detail[:160]}{'…' if len(detail) > 160 else ''}"
    tip += "\n（可在后台「生图 tag 转换模型」下拉里换其他可用模型）"
    return tip


# ============ 常量 ============
DEFAULT_CONFIG = {
    "search_cache_expire_minutes": 10,
    "max_search_results": 100,
    "search_image_size": "",
    "help_image_size": "",
    "video_image_size": "",
    "search_cmd_image_size": "",
    "playlist_image_size": "",
    "enable_random_bg_search": False,
    "enable_random_bg_help": False,
    "enable_random_bg_video": False,
    "enable_random_bg_search_cmd": False,
    "enable_random_bg_playlist": False,
    "enable_playlist_search": True,
    "video_random_bg_api": "",
    "search_cmd_random_bg_api": "",
    "playlist_random_bg_api": "",
    "random_bg_api": "https://uapis.cn/api/v1/random/image?type=pc",
    "random_bg_cache_seconds": 1800,
    "allow_unlogged_search": True,
    "audio_quality": "higher",
    "send_method": "auto",
    "enable_bilibili_video": False,
    "enable_bili_search_cmd": True,
    "bilibili_sessdata": "",
    "bilibili_video_max_mb": 50,
    "bilibili_result_limit": 10,
    "enable_image_gen": False,
    "nai_api_url": NAI_DEFAULT_API_URL,
    "nai_api_key": "",
    "nai_model": "nai-diffusion-4-5-full",
    "nai_size": "832x1216",
    "nai_steps": 28,
    "nai_scale": 5.0,
    "nai_sampler": "k_euler_ancestral",
    "nai_negative_extra": "",
    "nai_tag_provider": "",
    "nai_daily_limit": 5,
    "tusi_base_url": TUSI_DEFAULT_BASE_URL,
    "tusi_api_key": "",
    "tusi_template_id": "",
    "tusi_prompt_field": "",
    "tusi_negative_field": "",
    "http_proxy": ""
}

# 随机背景图下载大小上限（防止异常接口返回超大文件占满内存/磁盘）
RANDOM_BG_MAX_BYTES = 10 * 1024 * 1024
# 随机背景图缓存文件写回画布前的最大宽度（超出等比缩小，加快读取与渲染）
RANDOM_BG_MAX_WIDTH = 5120

# 用户配置音质 -> API level 参数
# standard=标准, higher=较高, exhigh=极高(会员), lossless=无损(会员),
# jyeffect=臻音全景/高清臻音(SVIP), sky=沉浸环绕声(SVIP), jymaster=超清母带(SVIP)
QUALITY_MAP = {
    "standard": "standard",
    "higher": "higher",
    "exhigh": "exhigh",
    "lossless": "lossless",
    "jyeffect": "jyeffect",
    "sky": "sky",
    "jymaster": "jymaster"
}

NETEASE = "https://music.163.com"
MUSIC_ROOT = r"D:\music"  # 本地歌单根目录（默认值，实际以配置 music_root 为准）
INVITE_TTL_SECONDS = 180  # 歌单成员邀请有效期（3 分钟）
PLAYLIST_MARKER = ".netease_playlist"  # 歌单标记文件，用于识别本插件创建的目录
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024  # 音频单文件下载上限（128MB），防止内存尖峰
MAX_COVER_BYTES = 10 * 1024 * 1024      # 封面单文件下载上限（10MB）
MAX_SEARCH_LIMIT = 500                  # 单次搜索出图数量硬上限，防止一次请求打满渲染与发送
MAX_SEARCH_CACHE_ENTRIES = 50           # 搜索结果缓存条目上限，防止只搜不点导致内存堆积
# 临时文件命名前缀（独立命名空间，避免与其它程序同前缀文件互相误删）
TEMP_FILE_PREFIX = "netease_music_"

# ============ 哔哩哔哩（B站视频搜索 / 下载） ============
BILIBILI_API = "https://api.bilibili.com"
BILIBILI_REFERER = "https://www.bilibili.com"
BILIBILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# WBI 签名用的字符重排表（B 站固定表，请勿修改）
_BILI_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
WBI_KEY_TTL = 1800          # WBI 密钥缓存时长（秒）
VIDEO_CACHE_ENTRIES = 30    # B 站视频搜索结果缓存条目上限
BILI_RESULT_LIMIT_MAX = 100  # 单次搜索视频数量硬上限（超出按 100 处理）
BILI_PAGE_SIZE = 25         # 视频列表图每页最多 25 条（5×5），超出自动分页
BILI_COLUMNS = 5            # 视频列表图横版列数（5 列 × 5 行）
BILI_VIDEO_MAX_MB_HARD = 2048  # 后台可配置的视频大小上限硬顶（MB）

# ============ 网易云歌单搜索 / 整单下载 ============
PLAYLIST_SEARCH_LIMIT = 50          # 「/搜歌单 关键词」一次最多返回多少个歌单候选
PLAYLIST_SONGS_PAGE_SIZE = 50       # 歌单歌曲列表图每页 50 首
PLAYLIST_CACHE_ENTRIES = 30         # 歌单搜索结果缓存条目上限
PLAYLIST_SONGS_CACHE_ENTRIES = 30   # 歌单歌曲列表缓存条目上限
PLAYLIST_SONG_DETAIL_BATCH = 50     # 批量拉取歌曲详情的每批数量（接口实测稳定）
MAX_PLAYLIST_TRACKS = 2000          # 单个歌单最多处理的歌曲数（防超大歌单拖垮流程）

# 支持发送语音（Record）的平台标识，用于 send_method=auto 时显式选择发送方式
VOICE_PLATFORMS = {
    "aiocqhttp", "qq_official", "qq_official_webhook", "telegram",
    "wechatpadpro", "gewechat", "lark", "discord",
}

# 盘符根下的危险一级目录名（含其任意子目录，如 C:\Windows\System32）
_FORBIDDEN_ROOT_DIRS = {
    "windows", "program files", "program files (x86)", "programdata", "users",
    "system32", "syswow64", "perflogs",
    "$recycle.bin", "system volume information", "recovery",
}

# Windows 保留设备名（不能用作文件/目录名，不区分大小写，带扩展名同样不可用）
_WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _is_dangerous_music_root(root: str) -> bool:
    """判断音乐根目录是否为危险的系统/盘符根目录"""
    if not root:
        return True
    # 裸盘符（如 "C:"）必须先拦：os.path.abspath("C:") 会解析成该盘的当前目录
    if re.fullmatch(r"[a-z]:", str(root).strip().rstrip("\\/").lower()):
        return True
    norm = os.path.abspath(root).rstrip("\\/").lower()
    # 盘符根，如 c:
    if re.fullmatch(r"[a-z]:", norm):
        return True
    # 盘符根下的系统目录及其任意子目录（前缀匹配，避免只拦一层）
    m = re.match(r"^([a-z]):[\\/](.+)$", norm)
    if m and m.group(2).split("\\")[0].split("/")[0] in _FORBIDDEN_ROOT_DIRS:
        return True
    return False


def _get_music_root(config: Dict) -> str:
    """从插件配置读取音乐根目录（任意盘），未配置或留空时默认 D:\\music"""
    root = str(config.get("music_root", r"D:\music") or r"D:\music").strip()
    root = root.rstrip("\\/")
    return root if root else r"D:\music"


def _resolve_config_image(value, base_dir: str) -> Optional[str]:
    """把后台「文件上传」配置项解析成本地绝对路径

    后台上传后配置值形如 ["files/search_bg_image/xxx.png"]（相对于插件数据目录），
    也兼容单个字符串或绝对路径。返回第一个真实存在的文件，均不存在返回 None。
    """
    if not value:
        return None
    items = value if isinstance(value, (list, tuple)) else [value]
    base = os.path.abspath(base_dir)
    for item in items:
        rel = str(item or "").strip().replace("\\", "/")
        if not rel:
            continue
        if os.path.isabs(rel):
            path = os.path.abspath(rel)
        else:
            path = os.path.abspath(os.path.join(base, rel))
            # 禁止路径穿越：解析后必须仍位于插件数据目录内
            try:
                if os.path.commonpath([base, path]) != base:
                    continue
            except ValueError:
                continue
        if os.path.isfile(path):
            return path
    return None


def _resolve_image_size(value) -> Optional[tuple]:
    """解析图片分辨率配置

    留空 = 自动跟随背景图（返回 None，由绘制端取背景图实际分辨率）；
    填写合法值则按填写值；格式错误或超范围时按默认 4320x2236。
    """
    text = str(value or "").strip()
    if not text:
        return None
    return parse_image_size(text)


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
    "Origin": "https://music.163.com",
    "X-Requested-With": "XMLHttpRequest",
}


def _bili_mixin_key(img_key: str, sub_key: str) -> str:
    """由 nav 接口返回的 img_key / sub_key 计算 WBI 签名密钥"""
    raw = f"{img_key}{sub_key}"
    try:
        return "".join(raw[i] for i in _BILI_MIXIN_KEY_ENC_TAB)[:32]
    except IndexError:
        return ""


def _bili_wbi_sign(params: Dict, mixin_key: str) -> Dict:
    """对请求参数做 WBI 签名，返回带 wts / w_rid 的新字典（不改动入参）"""
    signed = dict(params)
    signed["wts"] = int(time.time())
    filtered = [
        (k, "".join(ch for ch in str(v) if ch not in "!'()*"))
        for k, v in sorted(signed.items())
    ]
    query = urllib.parse.urlencode(filtered)
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


def _bili_clean_title(title: str) -> str:
    """去掉 B 站搜索结果标题里的 <em> 高亮标签并反转义 HTML 实体"""
    return html.unescape(re.sub(r"<[^>]+>", "", title or "")).strip()


def _format_duration(seconds) -> str:
    """秒数格式化为 mm:ss / h:mm:ss"""
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "00:00"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _format_play_count(value) -> str:
    """播放量格式化（万 / 亿）"""
    try:
        num = int(value)
    except (TypeError, ValueError):
        return "0"
    if num >= 100000000:
        return f"{num / 100000000:.1f}亿"
    if num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


@register(
    "astrbot_plugin_netease_music",
    "kuaiyidian123",
    "网易云点歌插件，支持 Cookie 导入登录，搜索歌曲并返回图片列表，选择后以语音发送",
    "1.8.2",
    "https://github.com/kuaiyidian123/astrbot_plugin_netease_music"
)
class NeteaseMusicPlugin(Star):
    """网易云音乐点歌插件"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)

        user_config = dict(config) if config else {}
        self.config = {**DEFAULT_CONFIG, **user_config}

        # 兼容旧配置：v1.4.0 把 random_bg_cache_minutes 改名为 random_bg_cache_seconds，
        # 老用户配置里仍是旧键名，这里折算为秒，避免升级后缓存时长静默失效
        if 'random_bg_cache_minutes' in user_config and 'random_bg_cache_seconds' not in user_config:
            try:
                minutes = int(user_config['random_bg_cache_minutes'])
                self.config['random_bg_cache_seconds'] = minutes * 60
                logger.info(
                    f"检测到旧配置 random_bg_cache_minutes={minutes}，"
                    f"已折算为 random_bg_cache_seconds={minutes * 60}，请尽快更新配置"
                )
            except (TypeError, ValueError):
                logger.warning("旧配置 random_bg_cache_minutes 值非法，已忽略并沿用默认值")

        # 持久化目录：优先使用框架接口（兼容 ASTRBOT_ROOT / 桌面运行时），失败回退到 cwd 约定
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_netease_music"))
        except Exception as e:
            logger.warning(f"获取框架数据目录失败，回退到默认路径: {e}")
            data_dir = os.path.join(
                os.getcwd(), 'data', 'plugin_data', 'astrbot_plugin_netease_music'
            )
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir

        self.cookie_file = os.path.join(data_dir, 'cookies.json')
        self.search_cache: Dict[str, Dict[str, Any]] = {}
        self.video_cache: Dict[str, Dict[str, Any]] = {}  # B 站视频搜索结果缓存
        self.playlist_cache: Dict[str, Dict[str, Any]] = {}        # 网易云歌单搜索结果缓存
        self.playlist_songs_cache: Dict[str, Dict[str, Any]] = {}  # 选中歌单后的歌曲列表缓存
        self._img_quota: Dict[str, Dict[str, Any]] = {}  # /生图 每日次数统计（按用户隔离）
        self._img_busy: set = set()                      # 正在生图的用户，防重复触发烧额度
        self._bili_buvid = ""            # 匿名访问用 buvid3（获取一次后复用）
        self._bili_buvid4 = ""           # 匿名访问用 buvid4（风控校验需要）
        self._bili_bnut = 0              # b_nut 指纹时间戳
        self._bili_wbi_key_cache = ""    # WBI 签名密钥缓存
        self._bili_wbi_key_expire = 0.0  # WBI 密钥过期时间戳
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._last_temp_cleanup = 0.0  # 上次临时文件清理时间戳（节流用）
        self._session_lock = asyncio.Lock()  # 保护 HTTP 会话创建，避免并发重复建连
        self._binding_lock = asyncio.Lock()  # 保护 binding.json 的读-改-写
        self._random_bg_lock = asyncio.Lock()  # 保护随机背景图的下载与缓存写回

        # Cookie 管理 (替代 QR 登录)
        self._cookies: Dict[str, str] = {}
        self._user_info: Dict[str, Any] = {}

        self._restore_cookies()

        # 本地歌单根目录（拒绝盘符根/系统目录，防止误删系统文件）
        music_root = _get_music_root(self.config)
        if _is_dangerous_music_root(music_root):
            logger.error(
                f"music_root 配置为危险目录「{music_root}」，"
                f"已回退到默认 D:\\music。请改用独立子目录（如 D:\\music）"
            )
            self.config["music_root"] = MUSIC_ROOT
            music_root = MUSIC_ROOT
        os.makedirs(music_root, exist_ok=True)

        logger.info("网易云点歌插件初始化完成")

    # ==================== 生命周期 ====================

    async def terminate(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None
        logger.info("网易云点歌插件已卸载")

    async def _get_http_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._http_session is None or self._http_session.closed:
                timeout = aiohttp.ClientTimeout(total=30)
                self._http_session = aiohttp.ClientSession(
                    timeout=timeout,
                    cookie_jar=aiohttp.CookieJar()
                )
                # 注入已保存的 cookies
                for k, v in self._cookies.items():
                    self._http_session.cookie_jar.update_cookies(
                        {k: v},
                        response_url=aiohttp.client.URL(NETEASE)
                    )
            return self._http_session

    def _get_proxy(self) -> Optional[str]:
        return self.config.get("http_proxy") or None

    # ==================== 图片渲染配置 ====================

    async def _get_search_image_options(self):
        """搜索结果图 (分辨率, 背景图路径)

        背景优先级：随机图 API（开启时） > 后台上传的固定背景图 > 插件内置背景图。
        分辨率来自后台配置，非法/未设置时回退默认。
        """
        size = _resolve_image_size(self.config.get("search_image_size"))
        bg_path = await self._get_random_bg_path("search")
        if not bg_path:
            bg_path = _resolve_config_image(self.config.get("search_bg_image"), self.data_dir)
        return size, bg_path

    async def _get_help_image_options(self):
        """帮助图 (分辨率, 背景图路径)，背景优先级同搜索结果图"""
        size = _resolve_image_size(self.config.get("help_image_size"))
        bg_path = await self._get_random_bg_path("help")
        if not bg_path:
            bg_path = _resolve_config_image(self.config.get("help_bg_image"), self.data_dir)
        return size, bg_path

    async def _get_bili_image_options(self, kind: str = "video"):
        """B 站视频列表图 (分辨率, 背景图路径)

        kind: 'video' = 选歌搜视频；'cmd' = /搜视频 直接搜索。
        两套配置彼此完全独立（开关、随机图接口、缓存文件、背景图、分辨率互不影响），
        背景优先级：随机图 API（开启时） > 后台上传的固定背景图 > 内置/渐变。
        """
        size_key = "search_cmd_image_size" if kind == "cmd" else "video_image_size"
        bg_key = "search_cmd_bg_image" if kind == "cmd" else "video_bg_image"
        size = _resolve_image_size(self.config.get(size_key))
        bg_path = await self._get_random_bg_path(kind)
        if not bg_path:
            bg_path = _resolve_config_image(self.config.get(bg_key), self.data_dir)
        return size, bg_path

    async def _get_playlist_image_options(self):
        """网易云歌单图 (分辨率, 背景图路径)

        「歌单搜索图」与「歌单歌曲图」共用这一套设置，
        背景优先级：随机图 API（开启时） > 后台上传的固定背景图 > 内置/渐变。
        """
        size = _resolve_image_size(self.config.get("playlist_image_size"))
        bg_path = await self._get_random_bg_path("playlist")
        if not bg_path:
            bg_path = _resolve_config_image(
                self.config.get("playlist_bg_image"), self.data_dir
            )
        return size, bg_path

    # ==================== 随机背景图 ====================

    def _random_bg_cache_path(self, kind: str) -> str:
        """随机背景图的本地缓存文件路径（search / help / video / cmd 各自独立）"""
        return os.path.join(self.data_dir, f"random_bg_{kind}.jpg")

    async def _download_random_bg(self, api: str) -> Optional[bytes]:
        """从随机图接口下载图片二进制，失败或超限返回 None"""
        headers = {"User-Agent": DEFAULT_HEADERS["User-Agent"]}
        try:
            session = await self._get_http_session()
            timeout = aiohttp.ClientTimeout(total=20)
            async with session.get(api, headers=headers, allow_redirects=True,
                                   timeout=timeout, proxy=self._get_proxy()) as resp:
                if resp.status != 200:
                    logger.warning(f"随机背景图接口返回状态码 {resp.status}")
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > RANDOM_BG_MAX_BYTES:
                        logger.warning("随机背景图超过大小上限，已丢弃")
                        return None
                return bytes(buf) if buf else None
        except Exception as e:
            logger.warning(f"随机背景图下载失败: {e}")
            return None

    async def _get_random_bg_path(self, kind: str) -> Optional[str]:
        """获取随机背景图并缓存为本地文件，返回其绝对路径

        kind: 'search' / 'help' / 'video' / 'cmd' / 'playlist'，各自开关与缓存均独立，因此可分别控制、背景互不相同。
        未开启随机图、接口异常或图片无法解析时返回 None（由调用方回退到固定背景图）。
        """
        switch_key = {
            "search": "enable_random_bg_search",
            "help": "enable_random_bg_help",
            "video": "enable_random_bg_video",
            "cmd": "enable_random_bg_search_cmd",
            "playlist": "enable_random_bg_playlist",
        }.get(kind, "enable_random_bg_search")
        if not self.config.get(switch_key, False):
            return None
        # 视频列表图 / 「/搜视频」/ 歌单图可分别指定随机图接口；留空则沿用通用接口
        api_key = {
            "video": "video_random_bg_api",
            "cmd": "search_cmd_random_bg_api",
            "playlist": "playlist_random_bg_api",
        }.get(kind, "")
        api = str(self.config.get(api_key) or "").strip() if api_key else ""
        if not api:
            api = str(self.config.get("random_bg_api") or "").strip()
        if not api:
            return None
        try:
            ttl = max(0, int(self.config.get("random_bg_cache_seconds", 1800)))
        except (TypeError, ValueError):
            ttl = 1800
        cache_path = self._random_bg_cache_path(kind)

        def _fresh() -> bool:
            # ttl 为 0 表示每次出图都重新获取；否则在有效期内（秒）直接复用缓存
            if ttl <= 0 or not os.path.isfile(cache_path):
                return False
            try:
                return time.time() - os.path.getmtime(cache_path) < ttl
            except OSError:
                return False

        if _fresh():
            return cache_path

        async with self._random_bg_lock:
            if _fresh():  # 等锁期间可能已被其他协程刷新
                return cache_path

            data = await self._download_random_bg(api)
            if not data:
                # 下载失败时退而使用旧缓存，避免背景图直接消失
                return cache_path if os.path.isfile(cache_path) else None
            try:
                img = PILImage.open(BytesIO(data))
                img.load()
                img = img.convert("RGB")
                if img.width > RANDOM_BG_MAX_WIDTH:
                    ratio = RANDOM_BG_MAX_WIDTH / img.width
                    img = img.resize(
                        (RANDOM_BG_MAX_WIDTH, max(1, int(img.height * ratio + 0.5))),
                        PILImage.LANCZOS
                    )
                # 统一转存为 JPEG，规避 webp/avif 等格式的解码兼容问题
                tmp_path = cache_path + ".tmp"
                img.save(tmp_path, format="JPEG", quality=92)
                os.replace(tmp_path, cache_path)
                return cache_path
            except Exception as e:
                logger.warning(f"随机背景图解析失败: {e}")
                return cache_path if os.path.isfile(cache_path) else None

    # ==================== Cookie 持久化 ====================

    def _restore_cookies(self):
        if not os.path.exists(self.cookie_file):
            return
        try:
            with open(self.cookie_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self._cookies = data.get('cookies', {})
            self._user_info = data.get('user_info', {})
            if self._cookies:
                nickname = self._user_info.get('nickname', '用户')
                logger.info(f"已恢复网易云登录状态 ({nickname})")
        except Exception as e:
            logger.warning(f"恢复登录状态失败: {e}")

    def _save_cookies(self):
        """原子保存登录凭证（与 binding.json 一致，避免写入中断损坏文件）"""
        try:
            tmp = self.cookie_file + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({
                    'cookies': self._cookies,
                    'user_info': self._user_info,
                    'saved_at': time.time()
                }, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.cookie_file)
        except Exception as e:
            logger.warning(f"保存登录凭证失败: {e}")

    def _is_logged_in(self) -> bool:
        """有 MUSIC_U Cookie 视为已登录"""
        return bool(self._cookies.get('MUSIC_U'))

    # ==================== 通用 HTTP ====================

    async def _api_get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """GET 请求网易云 API"""
        try:
            session = await self._get_http_session()
            proxy = self._get_proxy()
            params = dict(params or {})
            params['timestamp'] = int(time.time() * 1000)

            async with session.get(
                f"{NETEASE}{path}",
                params=params,
                headers=DEFAULT_HEADERS,
                proxy=proxy
            ) as resp:
                return await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"GET {path} 失败: {e}")
            return None

    async def _api_post_form(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """POST form-urlencoded 请求网易云 API"""
        try:
            session = await self._get_http_session()
            proxy = self._get_proxy()
            params = dict(params or {})
            params['timestamp'] = int(time.time() * 1000)

            headers = {**DEFAULT_HEADERS, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}

            async with session.post(
                f"{NETEASE}{path}",
                data=params,
                headers=headers,
                proxy=proxy
            ) as resp:
                return await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"POST {path} 失败: {e}")
            return None

    # ==================== 指令：点歌帮助 ====================

    @filter.command("点歌帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """列出所有功能与用法，渲染成图片发送（文本模式下直接发文本）"""
        if self._text_mode():
            yield event.plain_result(self._help_text())
            return
        try:
            size, bg_path = await self._get_help_image_options()

            # 「B站视频」分区按各自开关独立展示（两个入口互不依赖）
            bili_rows = []
            if self._bili_video_enabled():
                bili_rows.append(("选歌后发 序号 搜索视频", "按歌曲搜索相关 B 站视频"))
            if self._bili_search_cmd_enabled():
                bili_rows.append(("/搜视频 关键词", "直接按关键词搜索 B 站视频"))
            if bili_rows:
                bili_rows.append(("序号", "直接发送对应的 B 站视频文件（如 5）"))

            # 「搜歌单」分区（开关开启时展示）
            playlist_rows = []
            if self._playlist_search_enabled():
                playlist_rows.append(("/搜歌单 关键词", "搜索网易云歌单，返回歌单图片"))
                playlist_rows.append(("序号", "查看该歌单全部歌曲（每页 50 首）"))
                playlist_rows.append(("序号 下载歌单", "把该歌单整单下载到本地歌单"))
                playlist_rows.append(("/搜歌单 歌单链接", "按歌单分享链接查看全部歌曲"))
                playlist_rows.append(("序号（歌曲列表）", "发送对应歌曲的音频文件"))

            # 「AI 生图」分区（开关开启时展示）
            imagegen_rows = []
            if self._nai_enabled():
                imagegen_rows.append(("/生图 中文描述", "AI 生成二次元插画"))
                if self._tusi_ready():
                    imagegen_rows.append(("/生图 吐司 描述", "改走吐司接口生图"))
                imagegen_rows.append(("/生图帮助", "查看生图配置与可用模型 ID"))

            def _draw():
                return draw_help_image(
                    _get_music_root(self.config), size=size, bg_path=bg_path,
                    bili_rows=bili_rows, playlist_rows=playlist_rows,
                    imagegen_rows=imagegen_rows
                )

            image_io = await asyncio.get_running_loop().run_in_executor(None, _draw)
            image_path = self._bytes_to_tempfile(
                image_io.getvalue(), HELP_IMG_EXT, "netease_help"
            )
            yield event.image_result(image_path)
            self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生成帮助图片失败: {e}")
            # 兜底：文字版帮助
            yield event.plain_result(self._help_text())

    # ==================== 指令：点歌 ====================

    @filter.command("点歌")
    async def cmd_search(self, event: AstrMessageEvent):
        self._clean_expired_cache()

        keyword = event.message_str.replace("点歌", "").strip()
        if not keyword:
            yield event.plain_result("请输入歌曲名，用法：/点歌 歌名")
            return
        # 限制关键词长度，避免超长输入拖垮搜索出图（防 DoS）
        if len(keyword) > 100:
            keyword = keyword[:100]

        if not self._is_logged_in() and not self.config.get("allow_unlogged_search", True):
            yield event.plain_result(
                "请先导入网易云 Cookie 后再搜索。"
                "发送 /导入cookie 查看方法。"
            )
            return

        yield event.plain_result(f"🔍 正在搜索「{keyword}」，请稍候...")

        try:
            # 总数上限：至少保证能出一整页（每页 SEARCH_PAGE_SIZE 首）
            limit = self.config.get("max_search_results", 100)
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = 100
            # 下限保证能出一整页，上限防止单次请求渲染/发送过多图片
            limit = min(max(limit, SEARCH_PAGE_SIZE), MAX_SEARCH_LIMIT)
            data = await self._api_get('/api/cloudsearch/pc', {
                's': keyword,
                'type': 1,
                'limit': limit,
                'offset': 0
            })

            if not data or data.get('code') != 200:
                yield event.plain_result(f"😔 搜索请求失败，请稍后重试。")
                return

            songs = data.get('result', {}).get('songs', [])
            if not songs:
                yield event.plain_result(
                    f"😔 未找到与「{keyword}」相关的歌曲，请更换关键词试试。"
                )
                return

            display_songs = songs[:limit]

            # 缓存搜索结果（图片/文本模式都需要，选歌依赖它）
            session_key = self._get_session_key(event)
            expire_minutes = self.config.get("search_cache_expire_minutes", 10)
            self.search_cache[session_key] = {
                'expire_time': time.time() + expire_minutes * 60,
                'created_at': time.time(),
                'songs': [
                    {
                        'id': s.get('id'),
                        'name': s.get('name', ''),
                        'artists': [
                            {'name': a.get('name', '')}
                            for a in s.get('ar', [])
                        ],
                        'album': {'name': s.get('al', {}).get('name', '')}
                    }
                    for s in display_songs
                ]
            }

            # 文本发送模式：直接输出纯文本列表，不渲染图片
            if self._text_mode():
                yield event.plain_result(self._format_search_text(keyword, display_songs))
                return

            # 并发下载歌曲封面（失败自动跳过）
            covers = await self._fetch_covers(display_songs)

            size, bg_path = await self._get_search_image_options()

            def _draw():
                return draw_search_result_image(keyword, display_songs, None, covers,
                                                size=size, bg_path=bg_path)
            image_list = await asyncio.get_running_loop().run_in_executor(None, _draw)

            # 分页发送（每页 50 首，超过则自动多张图）
            for page_index, image_io in enumerate(image_list, 1):
                image_path = self._bytes_to_tempfile(
                    image_io.getvalue(), SEARCH_IMG_EXT, f"netease_search{page_index}"
                )
                yield event.image_result(image_path)
                self._schedule_tempfile_cleanup(image_path)

        except Exception as e:
            logger.error(f"点歌搜索失败: {e}")
            import traceback
            traceback.print_exc()
            yield event.plain_result("❌ 点歌失败，请稍后重试")

    # ==================== 指令：选歌 ====================

    @filter.command("选歌")
    async def cmd_select(self, event: AstrMessageEvent):
        """选歌：/选歌 序号 [添加歌单 歌单名]"""
        text = event.message_str.replace("选歌", "").strip()
        async for result in self._do_select(event, text):
            yield result

    @filter.regex(r"^\d+(?:\s+添加歌单\s+.+|\s+搜索视频|\s+下载歌单)?$")
    async def cmd_select_plain(self, event: AstrMessageEvent):
        """快捷选择：搜索后直接发「序号」；最近搜的是什么，序号就作用于什么

        「序号 添加歌单 歌单名」「序号 搜索视频」始终按点歌流程处理；
        「序号 下载歌单」把歌单搜索结果里的第 N 个歌单整单下载到本地；
        纯序号则看「最近一次列表」：B站视频 → 发视频文件，歌单 → 看该歌单歌曲，
        歌单歌曲 → 发对应音频，点歌搜索 → 点播歌曲。
        仅当「当前用户自己」有未过期缓存时才响应，且越界提示仅带唤醒前缀时给出。
        """
        text = event.message_str.strip()
        session_key = self._get_session_key(event)
        now = time.time()

        # 1) 「序号 下载歌单」→ 整单下载（作用于歌单搜索结果）
        m_dl = re.fullmatch(r"(\d+)\s+下载歌单", text)
        if m_dl:
            async for result in self._download_searched_playlist(event, int(m_dl.group(1))):
                yield result
            return

        # 2) 「序号 添加歌单 X」「序号 搜索视频」→ 始终走点歌流程
        if re.fullmatch(r"\d+\s+添加歌单\s+.+", text) or re.fullmatch(r"\d+\s+搜索视频", text):
            cache_data = self.search_cache.get(session_key)
            if not cache_data or cache_data.get('expire_time', 0) < now:
                if event.is_at_or_wake_command:
                    yield event.plain_result(
                        "❌ 未找到对应的搜索结果，请先发送 /点歌 歌名 进行搜索。"
                    )
                return
            async for result in self._do_select(event, text):
                yield result
            return

        # 3) 纯序号 → 按「最近一次列表」路由
        if not re.fullmatch(r"\d+", text):
            return
        index = int(text)
        kind, data = self._latest_list_cache(session_key, now)
        if kind is None:
            # 带唤醒前缀（如 /1）属于明确指令，给个提示；裸数字静默，避免打扰群聊
            if event.is_at_or_wake_command:
                yield event.plain_result(
                    "❌ 未找到对应的搜索结果，请先发送 /点歌 或 /搜歌单 进行搜索。"
                )
            return

        if kind == "video":
            videos = data.get('videos', [])
            if 1 <= index <= len(videos):
                async for result in self._do_send_video(event, index):
                    yield result
                return
            if event.is_at_or_wake_command:
                yield event.plain_result(
                    f"⚠️ 序号 {index} 超出范围，当前只有 {len(videos)} 个视频。"
                )
            return

        if kind == "playlist":
            playlists = data.get('playlists', [])
            if 1 <= index <= len(playlists):
                pl = playlists[index - 1]
                async for result in self._show_playlist_songs(
                    event, pl.get('id'), pl.get('name', '')
                ):
                    yield result
                return
            if event.is_at_or_wake_command:
                yield event.plain_result(
                    f"⚠️ 序号 {index} 超出范围，当前只有 {len(playlists)} 个歌单。"
                )
            return

        if kind == "playlist_songs":
            songs = data.get('songs', [])
            if 1 <= index <= len(songs):
                async for result in self._send_playlist_song(event, songs[index - 1]):
                    yield result
                return
            if event.is_at_or_wake_command:
                yield event.plain_result(
                    f"⚠️ 序号 {index} 超出范围，当前只有 {len(songs)} 首歌曲。"
                )
            return

        # kind == "song"：裸数字越界时静默，避免群里闲聊数字被回一条报错
        songs = data.get('songs', [])
        if not event.is_at_or_wake_command and not (1 <= index <= len(songs)):
            return
        async for result in self._do_select(event, text):
            yield result

    async def _do_select(self, event: AstrMessageEvent, text: str):
        """选歌公共实现：text 为「序号」「序号 添加歌单 歌单名」或「序号 搜索视频」"""
        self._clean_expired_cache()

        # 解析：可能是 "序号"、"序号 添加歌单 歌单名称"、"序号 搜索视频"
        parts = text.split(None, 2)
        download_playlist = None
        search_video = False

        if len(parts) >= 3 and parts[1] == "添加歌单":
            try:
                index = int(parts[0])
            except ValueError:
                yield event.plain_result("⚠️ 用法：/选歌 序号 或 /选歌 序号 添加歌单 歌单名")
                return
            download_playlist = parts[2].strip()
        elif len(parts) == 2 and parts[1].strip() == "搜索视频":
            try:
                index = int(parts[0])
            except ValueError:
                yield event.plain_result("⚠️ 用法：/选歌 序号 搜索视频")
                return
            search_video = True
        else:
            try:
                index = int(text)
            except ValueError:
                yield event.plain_result(
                    "⚠️ 请输入有效的歌曲序号\n"
                    "用法：/选歌 序号\n"
                    "下载到歌单：/选歌 序号 添加歌单 歌单名\n"
                    "搜索B站视频：/选歌 序号 搜索视频"
                )
                return

        if index < 1:
            yield event.plain_result("⚠️ 序号必须大于 0")
            return

        session_key = self._get_session_key(event)
        cache_data = self.search_cache.get(session_key)

        if not cache_data:
            yield event.plain_result(
                "❌ 未找到对应的搜索结果，请先发送 /点歌 歌名 进行搜索。"
            )
            return

        if cache_data.get('expire_time', 0) < time.time():
            del self.search_cache[session_key]
            yield event.plain_result("⏰ 搜索结果已过期，请重新搜索。")
            return

        songs = cache_data.get('songs', [])
        if index > len(songs):
            yield event.plain_result(
                f"⚠️ 序号 {index} 超出范围，当前只有 {len(songs)} 首歌曲。"
            )
            return

        selected_song = songs[index - 1]
        song_id = selected_song.get('id')
        song_name = selected_song.get('name', '未知歌曲')
        artist_name = selected_song.get('artists', [{}])[0].get('name', '未知歌手')

        # ===== 搜索 B 站视频模式：不发音频，改为返回视频候选列表 =====
        if search_video:
            async for result in self._search_bilibili_videos(event, song_name, artist_name):
                yield result
            return

        yield event.plain_result(f"🎵 正在获取「{artist_name} - {song_name}」...")

        try:
            quality = QUALITY_MAP.get(
                self.config.get("audio_quality", "higher"), "higher"
            )
            audio_url = await self._get_song_url(song_id, quality)

            if not audio_url:
                for fallback in ['standard', 'higher']:
                    audio_url = await self._get_song_url(song_id, fallback)
                    if audio_url:
                        break

            if not audio_url:
                yield event.plain_result(
                    "❌ 该歌曲可能无法获取音频，请尝试选择其他歌曲。"
                )
                return

            audio_data, audio_format = await self._download_audio(audio_url)
            if not audio_data:
                logger.warning(f"音频下载失败 song_id={song_id} url={audio_url}")
                yield event.plain_result(
                    f"🎵 {artist_name} - {song_name}\n"
                    f"❌ 音频下载失败，请稍后重试"
                )
                return

            # ===== 下载到歌单模式 =====
            if download_playlist:
                _off = self._feature_off_msg("playlist")
                if _off:
                    yield event.plain_result(_off)
                    return
                playlist_dir, _pdir_err = self._resolve_playlist_dir(download_playlist)
                if _pdir_err:
                    yield event.plain_result(_pdir_err)
                    return

                # 歌单必须已存在，避免歌单名输错时自动创建文件夹
                if not os.path.isdir(playlist_dir):
                    yield event.plain_result(
                        f"❌ 歌单「{download_playlist}」不存在\n"
                        f"请先用 /创建歌单 {download_playlist} 创建后再添加"
                    )
                    return

                # 必须是本插件创建的歌单，防止向音乐根目录下的其它目录写入文件
                marker_err = self._require_plugin_playlist(download_playlist, playlist_dir)
                if marker_err:
                    yield event.plain_result(marker_err)
                    return

                # 歌单所有权校验：需先绑定，且仅歌单主/成员可添加歌曲
                denied = self._check_admin(download_playlist, playlist_dir, event)
                if denied:
                    yield event.plain_result(denied)
                    return

                safe_name = self._sanitize_filename(f"{artist_name} - {song_name}")
                save_path = os.path.join(playlist_dir, f"{safe_name}.{audio_format}")

                try:
                    with open(save_path, 'wb') as f:
                        f.write(audio_data)

                    # 封面自动保存到歌单文件夹（渲染歌单图时直接读本地）
                    await self._save_song_cover(selected_song, playlist_dir, safe_name)

                    yield event.plain_result(
                        f"✅ 已保存到歌单「{download_playlist}」\n"
                        f"🎵 {artist_name} - {song_name}\n"
                        f"📁 {download_playlist}/{os.path.basename(save_path)}"
                    )
                except OSError as e:
                    yield event.plain_result(self._err_msg(e, "保存文件"))
                    return
            else:
                # ===== 直接播放模式（发送方式见 _emit_audio） =====
                for result in self._emit_audio(
                    event, audio_data, audio_format, audio_url, artist_name, song_name
                ):
                    yield result

            # 点播成功后作废本次搜索结果：需重新 /点歌 才能再点，避免重复点播
            self.search_cache.pop(session_key, None)
        except Exception as e:
            logger.error(f"选歌失败: {e}")
            yield event.plain_result("❌ 选歌失败，请稍后重试")

    # ==================== 指令：搜索网易云歌单 ====================

    def _playlist_search_enabled(self) -> bool:
        """「/搜歌单」入口开关"""
        return bool(self.config.get("enable_playlist_search", True))

    def _emit_audio(self, event: AstrMessageEvent, audio_data: bytes, audio_format: str,
                    audio_url: str, artist_name: str, song_name: str):
        """按配置的发送方式产出发送结果（auto 优先语音，平台不支持时回退文件）"""
        send_method = self.config.get("send_method", "auto")
        audio_path = self._bytes_to_tempfile(
            audio_data, f".{audio_format}", "netease_voice"
        )
        if send_method == "link":
            yield event.plain_result(
                f"🎵 {artist_name} - {song_name}\n🔗 链接: {audio_url}"
            )
            self._remove_tempfile(audio_path)
        elif send_method == "file":
            yield event.chain_result(
                [File(name=os.path.basename(audio_path), file=audio_path)]
            )
            self._schedule_tempfile_cleanup(audio_path)
        else:
            # auto：按平台能力显式选择。发送由后续 pipeline stage 执行，
            # 此处 yield 不会因平台不支持语音而抛异常，故不能依赖 try/except 回退。
            platform = ""
            try:
                platform = (event.get_platform_name() or "").lower()
            except Exception:
                platform = ""
            if platform in VOICE_PLATFORMS or not platform:
                yield event.chain_result([Record(file=audio_path)])
            else:
                # 已知不支持语音的平台，直接发文件，避免用户「什么都没收到」
                yield event.chain_result(
                    [File(name=os.path.basename(audio_path), file=audio_path)]
                )
            self._schedule_tempfile_cleanup(audio_path)

    @filter.command("搜歌单")
    async def cmd_search_playlist(self, event: AstrMessageEvent):
        """/搜歌单 关键词 → 搜索歌单出图；/搜歌单 歌单链接 [下载] → 查看 / 整单下载

        先搜索歌单，再发序号 → 查看该歌单全部歌曲（每页 50 首）；
        发「序号 下载歌单」→ 把该歌单整单下载到本地歌单文件夹；
        进入歌曲列表后发序号 → 直接发送该歌曲的音频文件。
        """
        if not self._playlist_search_enabled():
            yield event.plain_result("⚠️「/搜歌单」指令已被管理员关闭")
            return

        text = self._strip_command(event.message_str, "搜歌单")
        if not text:
            yield event.plain_result(
                "用法：\n"
                "/搜歌单 关键词 — 搜索网易云歌单\n"
                "/搜歌单 歌单链接 — 查看该歌单全部歌曲\n"
                "/搜歌单 歌单链接 下载 — 把该歌单下载到本地歌单\n"
                "选定歌单后：发 序号 查看歌曲；发 序号 下载歌单 整单下载"
            )
            return
        # 限制长度，避免超长输入拖垮请求（防 DoS）
        if len(text) > 500:
            text = text[:500]

        # 「… 下载」/「… 下载歌单」后缀 → 整单下载
        action = None
        m = re.search(r"\s+下载歌单\s*$|\s+下载\s*$", text)
        if m:
            action = "download"
            text = text[:m.start()].strip()

        playlist_id = await self._resolve_playlist_link(text)
        if playlist_id:
            if action == "download":
                async for result in self._download_playlist(event, playlist_id):
                    yield result
            else:
                async for result in self._show_playlist_songs(event, playlist_id):
                    yield result
            return

        if action == "download":
            yield event.plain_result(
                "⚠️ 整单下载需要歌单分享链接，用法：/搜歌单 歌单链接 下载\n"
                "或先 /搜歌单 关键词 搜索，再发「序号 下载歌单」"
            )
            return

        # 形如链接但解析不出歌单 id → 明确提示，避免把链接整串当作关键词去搜索
        if re.search(r"https?://", text):
            yield event.plain_result(
                "⚠️ 未能从该链接解析出歌单 id\n"
                "请使用歌单分享链接（music.163.com/playlist?id=xxx 或 163cn.tv 短链）"
            )
            return

        async for result in self._search_playlists_and_render(event, text):
            yield result

    @staticmethod
    def _extract_playlist_id(text: str) -> Optional[int]:
        """从歌单链接或纯数字中提取歌单 id"""
        if not text:
            return None
        m = re.search(r"[?&#]id=(\d+)", text)
        if m:
            return int(m.group(1))
        m = re.search(r"/playlist/(\d+)", text)
        if m:
            return int(m.group(1))
        m = re.fullmatch(r"\s*(\d{5,})\s*", text)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def _is_netease_host(host: str) -> bool:
        """是否为网易云相关域名（仅对这些域名跟随跳转解析歌单 id）"""
        h = (host or "").lower().strip(".")
        return (
            h == "163.com" or h.endswith(".163.com")
            or h == "163cn.tv" or h.endswith(".163cn.tv")
            or h.endswith(".126.com")
        )

    async def _resolve_playlist_link(self, text: str) -> Optional[int]:
        """解析歌单链接（含 163cn.tv 等短链：跟随跳转后再从最终地址取 id）

        仅对网易云相关域名发起请求，避免把任意 URL 当成歌单链接去请求（SSRF）。
        """
        pid = self._extract_playlist_id(text)
        if pid:
            return pid
        m = re.search(r"https?://\S+", text or "")
        if not m:
            return None
        url = m.group(0)
        if not self._is_netease_host(urllib.parse.urlparse(url).hostname or ""):
            return None
        try:
            session = await self._get_http_session()
            async with session.get(
                url, headers=DEFAULT_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
                proxy=self._get_proxy(), allow_redirects=True,
            ) as resp:
                final_url = str(resp.url)
                body = await resp.content.read(200 * 1024)
            for cand in (final_url, body.decode("utf-8", "ignore")):
                pid = self._extract_playlist_id(cand)
                if pid:
                    return pid
        except Exception as e:
            logger.warning(f"解析歌单链接失败: {e}")
        return None

    async def _search_playlists(self, keyword: str, limit: int) -> List[Dict]:
        """搜索网易云歌单，返回规范化后的列表"""
        data = await self._api_get('/api/cloudsearch/pc', {
            's': keyword, 'type': 1000, 'limit': limit, 'offset': 0
        })
        if not data or data.get('code') != 200:
            return []
        raw = (data.get('result') or {}).get('playlists') or []
        playlists: List[Dict] = []
        for p in raw[:limit]:
            pid = p.get('id')
            if not pid:
                continue
            playlists.append({
                'id': pid,
                'name': p.get('name') or '未知歌单',
                'cover': p.get('coverImgUrl') or '',
                'track_count': p.get('trackCount') or 0,
                'creator': ((p.get('creator') or {}).get('nickname') or '未知'),
                'play_count': p.get('playCount') or 0,
            })
        return playlists

    async def _fetch_cover_urls(self, mapping: Dict) -> Dict:
        """并发下载 {key: 图片URL} 的封面，返回 {key: PIL.Image}；单张失败自动跳过"""
        covers: Dict = {}
        sem = asyncio.Semaphore(4)

        async def _one(key, url):
            if not url:
                return None
            async with sem:
                try:
                    session = await self._get_http_session()
                    async with session.get(
                        str(url).replace('http://', 'https://'),
                        headers=DEFAULT_HEADERS, timeout=8,
                        proxy=self._get_proxy(), allow_redirects=True,
                    ) as resp:
                        if resp.status != 200:
                            return None
                        # 流式累加限流，避免异常响应撑爆内存
                        if (resp.content_length or 0) > MAX_COVER_BYTES:
                            return None
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            buf.extend(chunk)
                            if len(buf) > MAX_COVER_BYTES:
                                return None
                        data = bytes(buf)
                    return (key, PILImage.open(BytesIO(data)).convert('RGB'))
                except Exception as e:
                    logger.debug(f"封面下载失败 {key}: {e}")
                    return None

        results = await asyncio.gather(*[_one(k, u) for k, u in mapping.items()])
        for r in results:
            if r:
                covers[r[0]] = r[1]
        return covers

    async def _fetch_playlist_meta_and_ids(self, playlist_id: int) -> tuple:
        """获取歌单信息与全部歌曲 id

        v6 详情接口的 trackIds 含完整歌曲列表，tracks 仅返回前 10 首，
        因此统一以 trackIds 为准。
        """
        data = await self._api_get(
            '/api/v6/playlist/detail', {'id': playlist_id, 'n': 1000}
        )
        pl = (data or {}).get('playlist') or {}
        if not pl:
            return {}, []
        ids = [t.get('id') for t in (pl.get('trackIds') or []) if t.get('id')]
        if not ids:
            ids = [t.get('id') for t in (pl.get('tracks') or []) if t.get('id')]
        if len(ids) > MAX_PLAYLIST_TRACKS:
            ids = ids[:MAX_PLAYLIST_TRACKS]
        return pl, ids

    async def _fetch_songs_by_ids(self, ids: List[int]) -> List[Dict]:
        """按 id 分批拉取歌曲详情（ar/al 新格式），并按歌单原顺序返回"""
        if not ids:
            return []
        found: Dict[Any, Dict] = {}
        for i in range(0, len(ids), PLAYLIST_SONG_DETAIL_BATCH):
            chunk = ids[i:i + PLAYLIST_SONG_DETAIL_BATCH]
            c = "[" + ",".join('{"id":%d}' % int(x) for x in chunk) + "]"
            data = await self._api_get('/api/v3/song/detail', {'c': c})
            for song in (data or {}).get('songs') or []:
                found[song.get('id')] = song
        return [found[i] for i in ids if i in found]

    async def _search_playlists_and_render(self, event: AstrMessageEvent, keyword: str):
        """搜索歌单 → 缓存 → 出图（文本模式或出图失败时回退纯文本）"""
        yield event.plain_result(f"🔍 正在搜索歌单「{keyword}」，请稍候...")

        playlists = await self._search_playlists(keyword, PLAYLIST_SEARCH_LIMIT)
        if not playlists:
            yield event.plain_result(
                f"😔 未找到与「{keyword}」相关的歌单，请更换关键词试试。"
            )
            return

        expire_minutes = self.config.get("search_cache_expire_minutes", 10)
        self.playlist_cache[self._get_session_key(event)] = {
            'expire_time': time.time() + expire_minutes * 60,
            'created_at': time.time(),
            'keyword': keyword,
            'playlists': playlists,
        }
        # 写入后立即做容量/过期清理，保证缓存条数不超过上限
        self._clean_expired_playlist_cache()

        if self._text_mode():
            yield event.plain_result(self._format_playlists_text(keyword, playlists))
            return

        try:
            covers = await self._fetch_cover_urls(
                {p['id']: p.get('cover') for p in playlists}
            )
            size, bg_path = await self._get_playlist_image_options()

            def _draw():
                return draw_playlist_search_image(
                    keyword, playlists, covers, size=size, bg_path=bg_path
                )

            image_list = await asyncio.get_running_loop().run_in_executor(None, _draw)
            for page_index, image_io in enumerate(image_list, 1):
                image_path = self._bytes_to_tempfile(
                    image_io.getvalue(), SEARCH_IMG_EXT, f"netease_pl{page_index}"
                )
                yield event.image_result(image_path)
                self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生成歌单搜索图失败: {e}")
            yield event.plain_result(self._format_playlists_text(keyword, playlists))

    async def _show_playlist_songs(self, event: AstrMessageEvent, playlist_id: int,
                                   playlist_name: str = ""):
        """获取并展示某个歌单的全部歌曲（每页 50 首）"""
        yield event.plain_result("🔍 正在获取歌单歌曲，请稍候...")

        pl, ids = await self._fetch_playlist_meta_and_ids(playlist_id)
        if not ids:
            yield event.plain_result("😔 未获取到该歌单的歌曲（歌单可能为空或已失效）。")
            return
        songs = await self._fetch_songs_by_ids(ids)
        if not songs:
            yield event.plain_result("😔 未获取到该歌单的歌曲详情，请稍后重试。")
            return

        name = pl.get('name') or playlist_name or f"歌单{playlist_id}"
        expire_minutes = self.config.get("search_cache_expire_minutes", 10)
        self.playlist_songs_cache[self._get_session_key(event)] = {
            'expire_time': time.time() + expire_minutes * 60,
            'created_at': time.time(),
            'playlist_id': playlist_id,
            'playlist_name': name,
            'songs': songs,
        }
        # 本方法是 playlist_songs_cache 的唯一写入点，写入后立即做容量/过期清理，
        # 否则只翻歌单不搜索时缓存会无上限增长（新写入项过期时间最新，不会被淘汰）
        self._clean_expired_playlist_cache()

        if self._text_mode():
            yield event.plain_result(self._format_playlist_songs_text(name, songs))
            return

        try:
            covers = await self._fetch_covers(songs)
            size, bg_path = await self._get_playlist_image_options()

            def _draw():
                return draw_playlist_songs_image(
                    name, songs, covers, size=size, bg_path=bg_path,
                    per_page=PLAYLIST_SONGS_PAGE_SIZE,
                )

            image_list = await asyncio.get_running_loop().run_in_executor(None, _draw)
            for page_index, image_io in enumerate(image_list, 1):
                image_path = self._bytes_to_tempfile(
                    image_io.getvalue(), SEARCH_IMG_EXT, f"netease_plsong{page_index}"
                )
                yield event.image_result(image_path)
                self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生成歌单歌曲图失败: {e}")
            yield event.plain_result(self._format_playlist_songs_text(name, songs))

    async def _send_playlist_song(self, event: AstrMessageEvent, song: Dict):
        """发送歌单中某一首歌的音频（发送方式与 /选歌 一致）"""
        song_id = song.get('id')
        song_name = song.get('name') or '未知歌曲'
        artists = song.get('ar') or song.get('artists') or []
        artist_name = artists[0].get('name', '未知歌手') if artists else '未知歌手'

        yield event.plain_result(f"🎵 正在获取「{artist_name} - {song_name}」...")
        try:
            quality = QUALITY_MAP.get(
                self.config.get("audio_quality", "higher"), "higher"
            )
            audio_url = await self._get_song_url(song_id, quality)
            if not audio_url:
                for fallback in ('standard', 'higher'):
                    audio_url = await self._get_song_url(song_id, fallback)
                    if audio_url:
                        break
            if not audio_url:
                yield event.plain_result("❌ 该歌曲可能无法获取音频，请尝试选择其他歌曲。")
                return
            audio_data, audio_format = await self._download_audio(audio_url)
            if not audio_data:
                yield event.plain_result(
                    f"🎵 {artist_name} - {song_name}\n❌ 音频下载失败，请稍后重试"
                )
                return
            for result in self._emit_audio(
                event, audio_data, audio_format, audio_url, artist_name, song_name
            ):
                yield result
        except Exception as e:
            logger.error(f"发送歌单歌曲失败: {e}")
            yield event.plain_result("❌ 发送失败，请稍后重试")

    def _clean_expired_playlist_cache(self):
        """清理过期的歌单搜索/歌曲缓存，并按容量上限淘汰最早过期的条目"""
        now = time.time()
        for store, cap in (
            (self.playlist_cache, PLAYLIST_CACHE_ENTRIES),
            (self.playlist_songs_cache, PLAYLIST_SONGS_CACHE_ENTRIES),
        ):
            for k in [k for k, v in store.items() if v.get('expire_time', 0) < now]:
                del store[k]
            overflow = len(store) - cap
            if overflow > 0:
                oldest = sorted(store, key=lambda k: store[k].get('expire_time', 0))
                for k in oldest[:overflow]:
                    del store[k]

    def _make_local_playlist_name(self, name: str) -> str:
        """把网易云歌单名转成合法的本地歌单名（过滤非法字符 + 截断到 15 字符）"""
        n = self._sanitize_filename(str(name or '').strip())
        n = n.replace('..', '_').strip()
        n = n[:15].strip().rstrip(' .')
        return n or '_'

    def _latest_list_cache(self, session_key: str, now: float) -> tuple:
        """返回当前用户「最近一次」有效的列表缓存 (类型, 数据)

        类型：'video'（B站视频）/ 'playlist'（歌单列表）/ 'playlist_songs'（歌单歌曲）/
        'song'（点歌搜索）。裸序号据此路由：最近搜什么，发序号就作用于什么。
        """
        options = []
        for kind, store in (
            ("video", self.video_cache),
            ("playlist", self.playlist_cache),
            ("playlist_songs", self.playlist_songs_cache),
            ("song", self.search_cache),
        ):
            # 入口开关已关闭时，对应缓存不再参与裸序号路由
            if kind == "video" and not self._bili_any_enabled():
                continue
            if kind in ("playlist", "playlist_songs") and not self._playlist_search_enabled():
                continue
            data = store.get(session_key)
            if data and data.get('expire_time', 0) >= now:
                options.append((data.get('created_at', 0), kind, data))
        if not options:
            return None, None
        options.sort(key=lambda x: x[0], reverse=True)
        return options[0][1], options[0][2]

    async def _download_searched_playlist(self, event: AstrMessageEvent, index: int):
        """「序号 下载歌单」：按歌单搜索结果的序号整单下载"""
        if not self._playlist_search_enabled():
            yield event.plain_result("⚠️「/搜歌单」指令已被管理员关闭")
            return
        self._clean_expired_playlist_cache()
        data = self.playlist_cache.get(self._get_session_key(event))
        if not data or data.get('expire_time', 0) < time.time():
            yield event.plain_result(
                "❌ 未找到歌单搜索结果，请先发送 /搜歌单 关键词 进行搜索。"
            )
            return
        playlists = data.get('playlists', [])
        if index < 1 or index > len(playlists):
            yield event.plain_result(
                f"⚠️ 序号 {index} 超出范围，当前只有 {len(playlists)} 个歌单。"
            )
            return
        pl = playlists[index - 1]
        async for result in self._download_playlist(
            event, pl.get('id'), pl.get('name', '')
        ):
            yield result

    async def _download_playlist(self, event: AstrMessageEvent, playlist_id: int,
                                 playlist_name: str = ""):
        """把网易云歌单整单下载到本地歌单文件夹

        本地歌单不存在时自动创建（用网易云歌单名，截断到 15 字符）并绑定发起人；
        已存在时必须是本插件歌单，且发起人具备管理权限。
        """
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return

        yield event.plain_result("🔍 正在获取歌单信息，请稍候...")
        pl, ids = await self._fetch_playlist_meta_and_ids(playlist_id)
        if not ids:
            yield event.plain_result("😔 未获取到该歌单的歌曲（歌单可能为空或已失效）。")
            return

        remote_name = pl.get('name') or playlist_name or f"歌单{playlist_id}"
        local_name = self._make_local_playlist_name(remote_name)
        playlist_dir, pdir_err = self._resolve_playlist_dir(local_name)
        if pdir_err:
            yield event.plain_result(pdir_err)
            return

        # 先取歌曲详情再创建本地目录：避免详情拉取失败时残留一个空的已绑定歌单
        songs = await self._fetch_songs_by_ids(ids)
        if not songs:
            yield event.plain_result("😔 未获取到该歌单的歌曲详情，请稍后重试。")
            return

        if not os.path.isdir(playlist_dir):
            try:
                os.makedirs(playlist_dir, exist_ok=False)
            except OSError as e:
                yield event.plain_result(self._err_msg(e, "创建歌单"))
                return
            self._mark_playlist_dir(playlist_dir)
            # 自动把发起人绑定为歌单主，便于后续管理
            self._save_binding(playlist_dir, {
                'owner': {'user_id': self._get_user_id(event),
                          'name': self._get_user_name(event)},
                'members': [], 'pending': [],
            })
        else:
            marker_err = self._require_plugin_playlist(local_name, playlist_dir)
            if marker_err:
                yield event.plain_result(marker_err)
                return
            denied = self._check_admin(local_name, playlist_dir, event)
            if denied:
                yield event.plain_result(denied)
                return

        total = len(songs)
        yield event.plain_result(
            f"🎵 歌单「{remote_name}」共 {total} 首\n"
            f"开始下载到本地歌单「{local_name}」，请稍候..."
        )

        ok = skip = fail = 0
        for i, song in enumerate(songs, 1):
            status = await self._download_one_song_to_dir(song, playlist_dir)
            if status == 'ok':
                ok += 1
            elif status == 'skip':
                skip += 1
            else:
                fail += 1
            if i % 20 == 0 and i < total:
                yield event.plain_result(
                    f"⏳ 下载中… {i}/{total}（成功 {ok}，跳过 {skip}，失败 {fail}）"
                )

        yield event.plain_result(
            f"✅ 歌单「{remote_name}」下载完成\n"
            f"📁 本地歌单：{local_name}\n"
            f"成功 {ok} 首，跳过 {skip} 首（已存在），失败 {fail} 首"
        )

    async def _download_one_song_to_dir(self, song: Dict, playlist_dir: str) -> str:
        """下载单曲到指定歌单目录，返回 'ok' / 'skip'（已存在）/ 'fail'"""
        song_id = song.get('id')
        if not song_id:
            return 'fail'
        name = song.get('name') or '未知歌曲'
        artists = song.get('ar') or song.get('artists') or []
        artist = artists[0].get('name', '未知歌手') if artists else '未知歌手'
        safe_name = self._sanitize_filename(f"{artist} - {name}")

        # 已存在任意音频后缀则跳过，避免大歌单重复下载
        for ext in ('mp3', 'm4a', 'flac', 'aac', 'wav', 'ogg'):
            if os.path.exists(os.path.join(playlist_dir, f"{safe_name}.{ext}")):
                return 'skip'

        try:
            quality = QUALITY_MAP.get(
                self.config.get("audio_quality", "higher"), "higher"
            )
            audio_url = await self._get_song_url(song_id, quality)
            if not audio_url:
                for fallback in ('standard', 'higher'):
                    audio_url = await self._get_song_url(song_id, fallback)
                    if audio_url:
                        break
            if not audio_url:
                return 'fail'
            audio_data, audio_format = await self._download_audio(audio_url)
            if not audio_data:
                return 'fail'
            save_path = os.path.join(playlist_dir, f"{safe_name}.{audio_format}")
            with open(save_path, 'wb') as f:
                f.write(audio_data)
            # 封面自动保存到歌单文件夹（渲染歌单图时直接读本地）
            await self._save_song_cover(song, playlist_dir, safe_name)
            return 'ok'
        except Exception as e:
            logger.warning(f"下载歌曲失败 {song_id}: {e}")
            return 'fail'

    # ==================== 指令：搜索 / 发送 B 站视频 ====================

    @filter.command("搜视频")
    async def cmd_bili_search(self, event: AstrMessageEvent):
        """/搜视频 关键词 → 直接搜索 B 站视频（按播放量降序），再发序号即可发送视频"""
        if not self._bili_search_cmd_enabled():
            yield event.plain_result("⚠️「/搜视频」指令已被管理员关闭")
            return

        text = self._strip_command(event.message_str, "搜视频")
        if not text:
            yield event.plain_result(
                "请输入搜索关键词，用法：/搜视频 关键词\n"
                "示例：/搜视频 Take Me Hand"
            )
            return
        # 限制关键词长度，避免超长输入拖垮搜索出图（防 DoS）
        if len(text) > 100:
            text = text[:100]

        async for result in self._search_bilibili_videos(event, keyword=text):
            yield result

    @filter.regex(r"^视频\s*\d+$")
    async def cmd_bili_video(self, event: AstrMessageEvent):
        """发送 B 站视频（兼容写法）：「视频N」等价于直接发序号「N」"""
        if not self._bili_any_enabled():
            if event.is_at_or_wake_command:
                yield event.plain_result("⚠️ B 站视频功能已被管理员关闭")
            return

        m = re.match(r"^视频\s*(\d+)$", event.message_str.strip())
        if not m:
            return
        async for result in self._do_send_video(event, int(m.group(1))):
            yield result

    async def _do_send_video(self, event: AstrMessageEvent, index: int):
        """发送视频列表中第 index 个 B 站视频文件（纯序号与「视频N」共用）"""
        if not self._bili_any_enabled():
            if event.is_at_or_wake_command:
                yield event.plain_result("⚠️ B 站视频功能已被管理员关闭")
            return

        self._clean_expired_video_cache()
        data = self.video_cache.get(self._get_session_key(event))
        if not data or data.get('expire_time', 0) < time.time():
            if event.is_at_or_wake_command:
                yield event.plain_result(
                    "❌ 未找到视频搜索结果，请先发送「/选歌 序号 搜索视频」。"
                )
            return

        videos = data.get('videos', [])
        if not videos:
            yield event.plain_result("❌ 没有可发送的视频，请重新搜索。")
            return
        if index < 1 or index > len(videos):
            # 裸数字场景静默（避免群里闲聊打扰），带唤醒前缀才提示
            if event.is_at_or_wake_command:
                yield event.plain_result(
                    f"⚠️ 序号 {index} 超出范围，当前只有 {len(videos)} 个视频。"
                )
            return

        video = videos[index - 1]
        max_mb = self._bili_max_mb()
        max_bytes = max_mb * 1024 * 1024
        yield event.plain_result(
            f"🎬 正在下载视频「{video.get('title', '')}」，请稍候（上限 {max_mb}MB）..."
        )

        try:
            path, err = await self._bili_fetch_video(video, max_bytes)
        except Exception as e:
            logger.error(f"B 站视频下载异常: {e}")
            path, err = None, "❌ 视频下载失败，请稍后重试"

        if not path:
            yield event.plain_result(self._bili_link_fallback(video, err))
            return

        try:
            if os.path.getsize(path) > max_bytes:
                self._remove_tempfile(path)
                yield event.plain_result(self._bili_link_fallback(
                    video, f"⚠️ 视频超过 {max_mb}MB 上限，改为发送链接"
                ))
                return
            yield event.chain_result([Video(file=path)])
            self._schedule_tempfile_cleanup(path, delay=600)
        except OSError as e:
            self._remove_tempfile(path)
            yield event.plain_result(self._err_msg(e, "发送视频"))

    @staticmethod
    def _bili_link_fallback(video: Dict, reason: Optional[str]) -> str:
        """下载失败/超出大小上限时的兜底文案：说明原因并附视频链接"""
        return (
            f"{reason or '❌ 无法发送该视频'}\n"
            f"🎬 {video.get('title', '')}\n"
            f"🔗 https://www.bilibili.com/video/{video.get('bvid', '')}"
        )

    def _bili_video_enabled(self) -> bool:
        """「选歌搜视频」入口开关（/点歌 后发「序号 搜索视频」）"""
        return bool(self.config.get("enable_bilibili_video", False))

    def _bili_search_cmd_enabled(self) -> bool:
        """「/搜视频」直接搜索入口开关（与上一项互不依赖）"""
        return bool(self.config.get("enable_bili_search_cmd", True))

    def _bili_any_enabled(self) -> bool:
        """任一 B 站视频入口开启（「直接发序号发送视频」与帮助展示用）"""
        return self._bili_video_enabled() or self._bili_search_cmd_enabled()

    def _bili_max_mb(self) -> int:
        """单视频大小上限（MB），后台可配置，越界自动夹紧"""
        try:
            mb = int(self.config.get("bilibili_video_max_mb", 50))
        except (TypeError, ValueError):
            mb = 50
        return min(max(mb, 1), BILI_VIDEO_MAX_MB_HARD)

    def _bili_result_limit(self) -> int:
        """返回的视频候选数量（个）"""
        try:
            num = int(self.config.get("bilibili_result_limit", 10))
        except (TypeError, ValueError):
            num = 10
        return min(max(num, 1), BILI_RESULT_LIMIT_MAX)

    def _clean_expired_video_cache(self):
        now = time.time()
        expired = [
            k for k, v in self.video_cache.items()
            if v.get('expire_time', 0) < now
        ]
        for k in expired:
            del self.video_cache[k]
        # 容量上限：超出按最早过期淘汰（每项持有完整视频列表）
        overflow = len(self.video_cache) - VIDEO_CACHE_ENTRIES
        if overflow > 0:
            oldest = sorted(
                self.video_cache,
                key=lambda k: self.video_cache[k].get('expire_time', 0)
            )
            for k in oldest[:overflow]:
                del self.video_cache[k]

    # ---------- B 站接口 ----------

    def _bili_browser_headers(self) -> Dict[str, str]:
        """模拟浏览器的请求头（B 站接口强校验 UA / Referer / buvid 指纹）"""
        headers = {"User-Agent": BILIBILI_UA, "Referer": BILIBILI_REFERER}
        cookie = []
        if self._bili_buvid:
            cookie.append(f"buvid3={self._bili_buvid}")
        if self._bili_buvid4:
            cookie.append(f"buvid4={self._bili_buvid4}")
        if self._bili_bnut:
            cookie.append(f"b_nut={self._bili_bnut}")
        sessdata = (self.config.get("bilibili_sessdata") or "").strip()
        if sessdata:
            cookie.append(f"SESSDATA={sessdata}")
        if cookie:
            headers["Cookie"] = "; ".join(cookie)
        return headers

    async def _bili_ensure_buvid(self) -> None:
        """匿名访问需携带 buvid3/buvid4 指纹，否则会被风控（返回 v_voucher）"""
        if self._bili_buvid and self._bili_buvid4:
            return
        data = await self._bili_get_json("/x/frontend/finger/spi", with_cookie=False)
        payload = (data or {}).get("data") or {}
        if payload.get("b_3"):
            self._bili_buvid = payload["b_3"]
        if payload.get("b_4"):
            self._bili_buvid4 = payload["b_4"]
        if self._bili_buvid and not self._bili_bnut:
            self._bili_bnut = int(time.time())

    async def _bili_get_json(self, path: str, params: Optional[Dict] = None,
                             need_wbi: bool = False,
                             with_cookie: bool = True) -> Optional[Dict]:
        """GET 调用 B 站接口，可选 WBI 签名与浏览器 Cookie"""
        try:
            if with_cookie:
                await self._bili_ensure_buvid()
            session = await self._get_http_session()
            query = dict(params or {})
            if need_wbi:
                mixin = await self._bili_wbi_key()
                if not mixin:
                    return None
                query = _bili_wbi_sign(query, mixin)
            headers = (self._bili_browser_headers() if with_cookie
                       else {"User-Agent": BILIBILI_UA})
            async with session.get(
                f"{BILIBILI_API}{path}",
                params=query,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
                proxy=self._get_proxy(),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"B 站接口 {path} 返回状态码 {resp.status}")
                    return None
                return await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"B 站接口 {path} 请求失败: {e}")
            return None

    async def _bili_wbi_key(self) -> str:
        """获取并缓存 WBI 签名密钥（nav 接口无需登录即可返回）"""
        now = time.time()
        if self._bili_wbi_key_cache and now < self._bili_wbi_key_expire:
            return self._bili_wbi_key_cache
        data = await self._bili_get_json("/x/web-interface/nav")
        wbi = ((data or {}).get("data") or {}).get("wbi_img") or {}
        img_key = os.path.splitext(os.path.basename(wbi.get("img_url", "")))[0]
        sub_key = os.path.splitext(os.path.basename(wbi.get("sub_url", "")))[0]
        key = _bili_mixin_key(img_key, sub_key)
        if key:
            self._bili_wbi_key_cache = key
            self._bili_wbi_key_expire = now + WBI_KEY_TTL
        else:
            logger.warning("获取 B 站 WBI 密钥失败（可能被风控），视频搜索将不可用")
        return key

    @staticmethod
    def _bili_parse_duration(value) -> int:
        """B 站时长字段可能是秒数或 "MM:SS" / "H:MM:SS"，统一转为秒"""
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value or "").strip()
        if not text:
            return 0
        if text.isdigit():
            return int(text)
        try:
            total = 0
            for part in text.split(":"):
                total = total * 60 + int(part)
            return total
        except ValueError:
            return 0

    async def _bili_search_videos(self, keyword: str, limit: int) -> List[Dict]:
        """按播放量降序搜索 B 站视频（超过单页上限时自动翻页汇总）"""
        videos: List[Dict] = []
        seen = set()
        page_size = max(1, min(limit, 30))
        page = 1
        while len(videos) < limit and page <= 10:
            data = await self._bili_get_json(
                "/x/web-interface/wbi/search/type",
                {
                    "search_type": "video",
                    "keyword": keyword,
                    "page": page,
                    "page_size": page_size,
                    "order": "click",  # 按播放量排序
                },
                need_wbi=True,
            )
            if not data or data.get("code") != 0:
                if page == 1 and data:
                    logger.warning(
                        f"B 站搜索失败 code={data.get('code')} msg={data.get('message')}"
                    )
                break
            results = (data.get("data") or {}).get("result") or []
            if not results:
                break
            for item in results:
                bvid = item.get("bvid")
                if not bvid or bvid in seen:
                    continue
                seen.add(bvid)
                pic = item.get("pic") or ""
                if pic.startswith("//"):
                    pic = "https:" + pic
                try:
                    play = int(item.get("play") or 0)
                except (TypeError, ValueError):
                    play = 0
                videos.append({
                    "bvid": bvid,
                    "title": _bili_clean_title(item.get("title", "")),
                    "author": item.get("author") or "未知UP主",
                    "play": play,
                    "duration": self._bili_parse_duration(item.get("duration")),
                    "pic": pic,
                })
            page += 1

        # 双保险：再按播放量降序排一次并截断
        videos.sort(key=lambda v: v["play"], reverse=True)
        return videos[:limit]

    async def _bili_fetch_covers(self, videos: List[Dict]) -> Dict:
        """并发下载 B 站视频封面，返回 {bvid: PIL.Image}；单张失败自动跳过"""
        covers: Dict = {}
        sem = asyncio.Semaphore(4)
        headers = {"User-Agent": BILIBILI_UA, "Referer": BILIBILI_REFERER}

        async def _one(video: Dict):
            bvid = video.get("bvid")
            pic = video.get("pic") or ""
            if not bvid or not pic:
                return None
            async with sem:
                try:
                    session = await self._get_http_session()
                    async with session.get(
                        pic, headers=headers, timeout=8,
                        proxy=self._get_proxy(), allow_redirects=True,
                    ) as resp:
                        if resp.status != 200:
                            return None
                        # 流式累加限流，避免异常响应撑爆内存
                        if (resp.content_length or 0) > MAX_COVER_BYTES:
                            return None
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            buf.extend(chunk)
                            if len(buf) > MAX_COVER_BYTES:
                                return None
                        data = bytes(buf)
                    return (bvid, PILImage.open(BytesIO(data)).convert('RGB'))
                except Exception as e:
                    logger.debug(f"B 站视频封面下载失败 {bvid}: {e}")
                    return None

        results = await asyncio.gather(*[_one(v) for v in videos])
        for r in results:
            if r:
                covers[r[0]] = r[1]
        return covers

    async def _search_bilibili_videos(self, event: AstrMessageEvent,
                                      song_name: str = "", artist_name: str = "",
                                      keyword: str = ""):
        """搜索 B 站视频：渲染候选列表并缓存，供直接发序号发送。

        - 选歌触发：只传 song_name / artist_name（带歌手搜不到时退回只用歌名）
        - 直接搜索：传 keyword（/搜视频 关键词）
        """
        if not keyword and not self._bili_video_enabled():
            # 「选歌搜视频」入口只受该开关控制（与 /搜视频 的开关互不依赖）
            yield event.plain_result("⚠️ 选歌时的 B 站视频搜索已被管理员关闭")
            return

        self._clean_expired_video_cache()
        limit = self._bili_result_limit()

        if keyword:
            search_keyword = keyword
            yield event.plain_result(f"🔍 正在 B 站搜索「{search_keyword}」，请稍候...")
            videos = await self._bili_search_videos(search_keyword, limit)
        else:
            search_keyword = f"{song_name} {artist_name}".strip()
            yield event.plain_result(
                f"🔍 正在 B 站搜索「{search_keyword}」相关视频，请稍候..."
            )
            videos = await self._bili_search_videos(search_keyword, limit)
            if not videos and artist_name:
                # 带歌手搜不到时，退回只用歌名再搜一次
                videos = await self._bili_search_videos(song_name, limit)
                if videos:
                    search_keyword = song_name

        if not videos:
            yield event.plain_result(
                f"😔 未在 B 站找到与「{search_keyword}」相关的视频，可换个关键词再试。"
            )
            return

        expire_minutes = self.config.get("search_cache_expire_minutes", 10)
        self.video_cache[self._get_session_key(event)] = {
            'expire_time': time.time() + expire_minutes * 60,
            'created_at': time.time(),
            'keyword': search_keyword,
            'videos': videos,
        }

        if self._text_mode():
            yield event.plain_result(self._format_videos_text(search_keyword, videos))
            return

        try:
            covers = await self._bili_fetch_covers(videos)
            # 背景与分辨率按入口取各自的独立配置：/搜视频 用 'cmd'，选歌搜视频用 'video'
            size, bg_path = await self._get_bili_image_options("cmd" if keyword else "video")

            def _draw():
                return draw_bilibili_videos_image(
                    search_keyword, videos, covers, size=size, bg_path=bg_path,
                    per_page=BILI_PAGE_SIZE,
                )

            image_list = await asyncio.get_running_loop().run_in_executor(None, _draw)
            # 每页 25 条（5×5），超出自动分成多张图依次发送
            for page_index, image_io in enumerate(image_list, 1):
                image_path = self._bytes_to_tempfile(
                    image_io.getvalue(), SEARCH_IMG_EXT, f"netease_bili{page_index}"
                )
                yield event.image_result(image_path)
                self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生成 B 站视频列表图失败: {e}")
            yield event.plain_result(self._format_videos_text(search_keyword, videos))

    @staticmethod
    def _format_videos_text(keyword: str, videos: List[Dict]) -> str:
        """B 站视频候选 → 纯文本（文本发送模式）"""
        lines = [
            f"🎬 B站视频「{keyword}」共 {len(videos)} 个（按播放量排序）",
            "━━━━━━━━━━━━━━",
        ]
        for i, v in enumerate(videos, 1):
            lines.append(
                f"{i}. {v.get('title', '')}\n"
                f"   UP主：{v.get('author', '')}"
                f" · 播放：{_format_play_count(v.get('play', 0))}"
                f" · 时长：{_format_duration(v.get('duration', 0))}"
            )
        lines.append("直接发送序号即可发送对应视频（如 5）")
        return "\n".join(lines)

    async def _bili_get_video_info(self, bvid: str) -> Optional[Dict]:
        data = await self._bili_get_json("/x/web-interface/view", {"bvid": bvid})
        if not data or data.get("code") != 0:
            return None
        info = data.get("data") or {}
        return info if info.get("cid") else None

    @staticmethod
    def _bili_codec_rank(stream: Dict) -> int:
        """视频编码优先序：H.264(avc) 兼容性最好，优先于 H.265(hev)/AV1"""
        codec = (stream.get("codecs") or "").lower()
        if codec.startswith("avc"):
            return 0
        if codec.startswith(("hev", "hvc")):
            return 1
        if codec.startswith("av01"):
            return 2
        return 3

    @classmethod
    def _bili_pick_streams(cls, video_streams: List[Dict], audio_streams: List[Dict],
                           duration: int, max_bytes: int):
        """挑选体积可控的最高画质视频流 + 常规音轨，返回 (视频URL, 音频URL)"""
        vids = [s for s in video_streams if s.get("baseUrl") or s.get("base_url")]
        if not vids:
            return None
        # 同一清晰度可能有多种编码，只保留兼容性最好的那种（优先 H.264）
        by_id: Dict[int, Dict] = {}
        for stream in vids:
            sid = stream.get("id", 0)
            current = by_id.get(sid)
            if current is None or cls._bili_codec_rank(stream) < cls._bili_codec_rank(current):
                by_id[sid] = stream
        ordered = [by_id[k] for k in sorted(by_id)]  # 清晰度从低到高

        auds = [s for s in audio_streams if s.get("baseUrl") or s.get("base_url")]
        # 音轨只用常规清晰度（64k/132k/192k），避开杜比与 Hi-Res 以免体积失控
        preferred = [s for s in auds if s.get("id") in (30216, 30232, 30280)]
        if preferred:
            audio = max(preferred, key=lambda s: s.get("id", 0))
        elif auds:
            audio = min(auds, key=lambda s: s.get("id", 0))
        else:
            audio = None
        audio_size = int(
            (audio.get("bandwidth", 0) if audio else 0) / 8 * max(duration, 0)
        )

        chosen = ordered[0]
        for stream in ordered:
            est = int(stream.get("bandwidth", 0) / 8 * max(duration, 0)) + audio_size
            if duration <= 0 or est <= max_bytes:
                chosen = stream  # 记录满足体积上限里的最高一档
        v_url = chosen.get("baseUrl") or chosen.get("base_url")
        a_url = (audio.get("baseUrl") or audio.get("base_url")) if audio else None
        return v_url, a_url

    async def _bili_download_media(self, url: str, max_bytes: int) -> Optional[str]:
        """流式下载单个媒体流到临时文件，超限或失败返回 None"""
        if not url:
            return None
        path = os.path.join(
            tempfile.gettempdir(),
            f"{TEMP_FILE_PREFIX}bili_{int(time.time() * 1000)}_{os.getpid()}.m4s"
        )
        try:
            session = await self._get_http_session()
            async with session.get(
                url,
                headers=self._bili_browser_headers(),
                timeout=aiohttp.ClientTimeout(total=180),
                proxy=self._get_proxy(),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"B 站媒体流返回状态码 {resp.status}")
                    return None
                if (resp.content_length or 0) > max_bytes:
                    logger.warning("B 站媒体流超过大小上限，已跳过")
                    return None
                size = 0
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            logger.warning("B 站媒体流超过大小上限，已中止")
                            f.close()
                            self._remove_tempfile(path)
                            return None
                        f.write(chunk)
            if os.path.getsize(path) > 0:
                return path
            self._remove_tempfile(path)
            return None
        except Exception as e:
            logger.warning(f"下载 B 站媒体流失败: {e}")
            self._remove_tempfile(path)
            return None

    async def _bili_merge(self, video_path: str,
                          audio_path: Optional[str]) -> Optional[str]:
        """用 ffmpeg 把 DASH 音视频合并为 mp4（无 ffmpeg 时返回 None）"""
        out_path = os.path.join(
            tempfile.gettempdir(),
            f"{TEMP_FILE_PREFIX}bili_{int(time.time() * 1000)}_{os.getpid()}.mp4"
        )
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path]
        if audio_path:
            cmd += ["-i", audio_path]
        cmd += ["-c", "copy", out_path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
        except FileNotFoundError:
            logger.error("未找到 ffmpeg，无法合并 B 站音视频，请安装 ffmpeg 后重试")
            return None
        except Exception as e:
            logger.error(f"ffmpeg 执行失败: {e}")
            return None
        if proc.returncode != 0 or not os.path.isfile(out_path):
            logger.warning(f"ffmpeg 合并失败: {str(stderr)[:300]}")
            return None
        return out_path

    async def _bili_fetch_video(self, video: Dict, max_bytes: int) -> tuple:
        """下载 B 站视频（DASH 音视频合并），返回 (本地路径, 错误提示)"""
        bvid = video.get("bvid")
        if not bvid:
            return None, "❌ 视频信息缺失"
        info = await self._bili_get_video_info(bvid)
        if not info:
            return None, "❌ 获取视频信息失败"

        data = await self._bili_get_json(
            "/x/player/wbi/playurl",
            {
                "bvid": bvid,
                "cid": info.get("cid"),
                "fnval": 16,   # DASH：音视频分离
                "fnver": 0,
                "fourk": 1,
                "qn": 80,      # 请求 1080P，实际以账号权限为准
            },
            need_wbi=True,
        )
        if not data or data.get("code") != 0:
            return None, "❌ 获取播放地址失败（部分视频需配置 SESSDATA）"

        payload = data.get("data") or {}
        dash = payload.get("dash") or {}
        duration = int(info.get("duration") or 0)

        picked = self._bili_pick_streams(
            dash.get("video") or [], dash.get("audio") or [], duration, max_bytes
        )
        if not picked:
            # 回退：服务端直接给出的合并流
            durl = payload.get("durl") or []
            if durl:
                merged = await self._bili_download_media(durl[0].get("url"), max_bytes)
                return (merged, None) if merged else (None, "❌ 视频流下载失败")
            return None, "❌ 未找到可下载的视频流"

        v_url, a_url = picked
        v_path = None
        a_path = None
        out_path = None
        try:
            v_path = await self._bili_download_media(v_url, max_bytes)
            if not v_path:
                return None, "❌ 视频下载失败或超过大小上限"
            if a_url:
                a_path = await self._bili_download_media(a_url, max_bytes)
            out_path = await self._bili_merge(v_path, a_path)
            if not out_path:
                # 无 ffmpeg 时退回无声视频，至少让用户看到画面
                return v_path, None
            return out_path, None
        finally:
            for tmp in (v_path, a_path):
                if tmp and tmp != out_path:
                    self._remove_tempfile(tmp)

    # ==================== 指令：NovelAI 生图 ====================

    def _nai_enabled(self) -> bool:
        """「/生图」功能开关"""
        return bool(self.config.get("enable_image_gen", False))

    def _nai_cfg(self) -> Dict[str, Any]:
        """解析并收敛生图配置（后台填错也不会直接崩，统一夹紧到安全范围）"""
        try:
            steps = int(self.config.get("nai_steps", 28))
        except (TypeError, ValueError):
            steps = 28
        try:
            scale = float(self.config.get("nai_scale", 5))
        except (TypeError, ValueError):
            scale = 5.0
        try:
            limit = int(self.config.get("nai_daily_limit", 5))
        except (TypeError, ValueError):
            limit = 5
        sampler = str(self.config.get("nai_sampler") or "").strip()
        if sampler not in NAI_SAMPLER_OPTIONS:
            sampler = "k_euler_ancestral"
        w, h = _parse_nai_size(self.config.get("nai_size"))
        return {
            "url": str(self.config.get("nai_api_url") or "").strip() or NAI_DEFAULT_API_URL,
            "key": str(self.config.get("nai_api_key") or "").strip(),
            "model": str(self.config.get("nai_model") or "").strip() or "nai-diffusion-4-5-full",
            "width": w,
            "height": h,
            "steps": min(max(steps, 1), 50),
            "scale": min(max(scale, 0.0), 10.0),
            "sampler": sampler,
            "extra_negative": str(self.config.get("nai_negative_extra") or "").strip(),
            "limit": max(0, limit),
        }

    # ---------- 每日次数限制 ----------

    def _img_quota_state(self, event: AstrMessageEvent) -> Dict[str, Any]:
        """取该用户今日的生图计数（跨天自动重置，条目过多时清理旧记录）"""
        key = self._get_user_id(event) or self._get_session_key(event)
        today = time.strftime("%Y-%m-%d")
        rec = self._img_quota.get(key)
        if not rec or rec.get("date") != today:
            rec = {"date": today, "count": 0}
            self._img_quota[key] = rec
            if len(self._img_quota) > 1000:
                for k in [k for k, v in self._img_quota.items()
                          if v.get("date") != today]:
                    del self._img_quota[k]
        return rec

    def _img_quota_check(self, event: AstrMessageEvent, limit: int) -> Optional[str]:
        """次数校验；limit<=0 表示不限次数。超限返回提示文案，否则 None"""
        if limit <= 0:
            return None
        if self._img_quota_state(event)["count"] >= limit:
            return f"⚠️ 今日生图次数已用完（{limit} 次/天），请明天再试"
        return None

    def _img_quota_commit(self, event: AstrMessageEvent, limit: int) -> None:
        """生图成功后计一次数（失败不计，避免配置/网络问题白扣用户次数）"""
        if limit > 0:
            self._img_quota_state(event)["count"] += 1

    def _img_quota_remaining(self, event: AstrMessageEvent, limit: int) -> int:
        """今日剩余次数；-1 表示不限"""
        if limit <= 0:
            return -1
        return max(0, limit - self._img_quota_state(event)["count"])

    # ---------- 中文描述 -> 英文 tag ----------

    async def _resolve_tag_provider(self):
        """取用于 tag 转换的对话模型：先按后台填的 ID，失败回退当前默认模型"""
        provider_id = str(self.config.get("nai_tag_provider") or "").strip()
        if provider_id:
            prov = None
            try:
                prov = self.context.get_provider_by_id(provider_id)
            except Exception as e:
                logger.warning(f"按 ID 获取对话模型失败: {e}")
            if prov is not None:
                return prov, None
            logger.warning(f"未找到对话模型「{provider_id}」，回退到 AstrBot 当前默认模型")
        try:
            prov = await self.context.get_using_provider_async()
        except Exception as e:
            logger.warning(f"获取默认对话模型失败: {e}")
            prov = None
        if prov is None:
            return None, (
                "❌ 未找到可用的对话模型\n"
                "请在 AstrBot 中配置模型，或在后台「生图 tag 转换模型」下拉里选一个本实例已有的模型"
            )
        return prov, None

    @staticmethod
    def _parse_tag_output(text: str) -> tuple:
        """从大模型输出中解析「正向tag / 负面tag」

        大模型不一定严格按格式回答，这里逐行容错：中/英标签、有无冒号、
        内容写在同一行或折到下一行、markdown 加粗与代码块都支持；
        若整段完全没写标签但本身就是一串英文 tag，则直接当作正向 tag 使用。
        """
        raw = re.sub(r"```[a-zA-Z]*", "", str(text or ""))
        pos_parts: List[str] = []
        neg_parts: List[str] = []
        cur = None      # 当前处于哪个标签块下（'pos' / 'neg'），用于接住折行的内容
        for line in raw.splitlines():
            line = _clean_tag_line(line)
            if not line:
                continue
            kind, rest = _match_tag_label(line)
            if kind:
                cur = kind
                if rest:
                    (pos_parts if kind == "pos" else neg_parts).append(rest)
                continue
            if cur == "pos":
                pos_parts.append(line)
            elif cur == "neg":
                neg_parts.append(line)
        pos = _clean_tag_line(", ".join(pos_parts))
        neg = _clean_tag_line(", ".join(neg_parts))
        if not pos and _looks_like_tag_list(raw):
            pos = _clean_tag_line(re.sub(r"\s+", " ", raw))
        return pos, neg

    @staticmethod
    def _normalize_tags(pos: str, neg: str, extra_negative: str) -> tuple:
        """规整 tag：正向补齐 masterpiece/best quality 打头，负面强制补全固定词"""
        pos = re.sub(r"\s+", " ", pos or "").strip(" ,，")
        if pos and "masterpiece" not in pos.lower():
            pos = f"masterpiece, best quality, {pos}"
        elif not pos:
            pos = "masterpiece, best quality"

        parts = [p.strip() for p in re.sub(r"\s+", " ", neg or "").split(",") if p.strip()]
        lower = {p.lower() for p in parts}
        for word in NAI_REQUIRED_NEGATIVE:
            if word.lower() not in lower:
                parts.append(word)
                lower.add(word.lower())
        for word in str(extra_negative or "").split(","):
            word = word.strip()
            if word and word.lower() not in lower:
                parts.append(word)
                lower.add(word.lower())
        return pos, ", ".join(parts)

    async def _convert_to_tags(self, text: str) -> tuple:
        """调用对话模型把中文描述转成英文绘画 tag，返回 (正向, 负面, 错误文案)"""
        prov, err = await self._resolve_tag_provider()
        if err:
            return "", "", err
        try:
            resp = await prov.text_chat(
                prompt=NAI_TAG_USER_TEMPLATE.format(text=text),
                system_prompt=NAI_TAG_SYSTEM_PROMPT,
            )
        except Exception as e:
            logger.error(f"调用对话模型转换绘画 tag 失败: {e}")
            return "", "", f"❌ 调用对话模型失败\n{_describe_llm_error(e)}"
        # 不同 provider 返回的对象/字段不一致：优先取 completion_text，
        # 部分实现直接返回字符串，这里一并兼容，避免误报"没有返回内容"
        raw = str(getattr(resp, "completion_text", "") or "").strip()
        if not raw and isinstance(resp, str):
            raw = resp.strip()
        if not raw:
            return "", "", "❌ 对话模型没有返回内容，请稍后重试"
        pos, neg = self._parse_tag_output(raw)
        if not pos:
            logger.warning(f"绘画 tag 解析失败，原始输出: {raw[:200]}")
            preview = " ".join(raw.split())[:80]
            return "", "", (
                "❌ 关键词转换结果格式异常\n"
                "通常是当前对话模型没有按约定格式输出，可重试或换个描述；"
                "若持续出现，可在后台「生图 tag 转换模型」下拉里换其他模型\n"
                f"模型原始输出：{preview}"
            )
        return pos, neg, None

    # ---------- NovelAI 接口 ----------

    @staticmethod
    async def _read_capped(resp, max_bytes: int) -> Optional[bytes]:
        """流式读取响应体并限制上限，超限返回 None（防止异常响应撑爆内存）"""
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                return None
        return bytes(buf)

    @staticmethod
    def _extract_image(data: bytes) -> Optional[bytes]:
        """从 API 响应里取出图片字节：兼容 ZIP 包（官方返回格式）与裸 PNG/JPEG"""
        if not data:
            return None
        if data[:2] == b"PK":
            try:
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    for name in sorted(zf.namelist()):
                        if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                            return zf.read(name)
            except Exception as e:
                logger.warning(f"解压 NovelAI 返回的 ZIP 失败: {e}")
            return None
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return data
        if data[:2] == b"\xff\xd8":
            return data
        return None

    @staticmethod
    def _nai_error_msg(status: int, body: bytes) -> str:
        """把接口错误转成好懂的中文提示（优先透出服务端 message）"""
        detail = ""
        try:
            obj = json.loads(body.decode("utf-8", "ignore"))
            if isinstance(obj, dict):
                detail = str(obj.get("message") or obj.get("error") or "").strip()
        except Exception:
            detail = ""
        if status == 401:
            return "❌ NovelAI Token 无效或已过期，请检查后台「生图 API Key」"
        if status == 402:
            return "❌ NovelAI Anlas 额度不足，请先补充额度"
        if status == 429:
            return "⚠️ NovelAI 请求过于频繁（被限流），请稍后再试"
        tail = f"\n{detail}" if detail else ""
        return f"❌ 生图失败（HTTP {status}）{tail}"

    async def _nai_generate(self, positive: str, negative: str,
                            cfg: Dict[str, Any]) -> tuple:
        """调用 NovelAI 生图接口，返回 (图片字节, 错误文案)"""
        payload = {
            "input": positive,
            "model": cfg["model"],
            "action": "generate",
            "parameters": {
                "params_version": 3,
                "width": cfg["width"],
                "height": cfg["height"],
                "scale": cfg["scale"],
                "sampler": cfg["sampler"],
                "steps": cfg["steps"],
                "n_samples": 1,
                "ucPreset": 0,
                "qualityToggle": True,
                "dynamic_thresholding": False,
                "controlnet_strength": 1,
                "legacy": False,
                "add_original_image": False,
                "cfg_rescale": 0,
                "noise_schedule": "native",
                "legacy_v3_extend": False,
                "skip_cfg_above_sigma": None,
                "use_coords": False,
                "seed": random.randint(0, 4294967295),
                "negative_prompt": negative,
                "sm": False,
                "sm_dyn": False,
                # V4.5/V5 必须同时带这两项，缺任意一个服务端会直接返回 500
                "v4_prompt": {
                    "caption": {"base_caption": positive, "char_captions": []},
                    "use_coords": False,
                    "use_order": True,
                },
                "v4_negative_prompt": {
                    "caption": {"base_caption": negative, "char_captions": []},
                    "use_coords": False,
                    "use_order": True,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {cfg['key']}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
        }
        try:
            session = await self._get_http_session()
            async with session.post(
                cfg["url"], json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=NAI_REQUEST_TIMEOUT),
                proxy=self._get_proxy(),
            ) as resp:
                body = await self._read_capped(resp, NAI_MAX_IMAGE_BYTES)
                if body is None:
                    return None, "❌ 生成结果过大，已中止接收"
                if resp.status != 200:
                    logger.warning(f"NovelAI 生图失败 HTTP {resp.status}")
                    return None, self._nai_error_msg(resp.status, body)
            image = self._extract_image(body)
            if not image:
                logger.warning(f"NovelAI 返回内容无法识别，长度 {len(body)}")
                return None, "❌ 未能从接口响应中解析出图片，请检查 API 地址是否正确"
            return image, None
        except asyncio.TimeoutError:
            return None, "⏰ 生图超时，请稍后重试"
        except aiohttp.ClientConnectionError as e:
            host = urllib.parse.urlparse(cfg["url"]).netloc or cfg["url"]
            logger.error(f"NovelAI 生图连接失败 {host}: {e}")
            return None, (
                f"❌ 无法连接到生图接口：{host}\n"
                "请确认后台「生图 API 地址」完整有效；若地址本身没问题\n"
                "（例如域名能 ping 通），多为网络阻断所致，可在后台\n"
                "「HTTP 代理」填写本地代理地址（如 http://127.0.0.1:7890）。"
            )
        except Exception as e:
            logger.error(f"NovelAI 生图请求失败: {e}")
            return None, "❌ 生图请求失败，请稍后重试"

    # ---------- 吐司（TAMS）接口 ----------

    def _tusi_cfg(self) -> Dict[str, Any]:
        """解析吐司（TAMS）生图配置"""
        return {
            "base_url": (str(self.config.get("tusi_base_url") or "").strip()
                         or TUSI_DEFAULT_BASE_URL).rstrip("/"),
            "api_key": str(self.config.get("tusi_api_key") or "").strip(),
            "template_id": str(self.config.get("tusi_template_id") or "").strip(),
            "prompt_field": str(self.config.get("tusi_prompt_field") or "").strip(),
            "negative_field": str(self.config.get("tusi_negative_field") or "").strip(),
        }

    def _tusi_ready(self) -> bool:
        """吐司是否已配置好（API Key 与模板 ID 都填了才可用）"""
        cfg = self._tusi_cfg()
        return bool(cfg["api_key"] and cfg["template_id"])

    @staticmethod
    def _pick_tusi_attr(attrs: List[Dict[str, Any]], explicit: str,
                        keywords: tuple, exclude: tuple = ()) -> Optional[Dict[str, Any]]:
        """在模板字段里定位目标字段：优先用配置指定的名称，否则按关键词自动识别"""
        if explicit:
            target = explicit.lower()
            for attr in attrs:
                if str(attr.get("fieldName") or "").strip() == explicit:
                    return attr
            for attr in attrs:
                if target in str(attr.get("fieldName") or "").lower():
                    return attr
            return None
        for attr in attrs:
            name = str(attr.get("fieldName") or "").lower()
            if not name:
                continue
            if any(k in name for k in keywords) and not any(x in name for x in exclude):
                return attr
        return None

    @staticmethod
    def _tusi_error_msg(status: int, body: bytes) -> str:
        """把吐司接口错误转成可读提示"""
        detail = ""
        try:
            obj = json.loads(body.decode("utf-8", "ignore"))
            if isinstance(obj, dict):
                detail = str(obj.get("message") or obj.get("msg") or obj.get("error") or "").strip()
        except Exception:
            detail = ""
        if status == 401:
            return "❌ 吐司 API Key 无效或已过期，请检查后台「吐司 API Key」"
        if status == 403:
            return "❌ 吐司拒绝访问（403），请确认该应用权限或算力余额"
        if status == 404:
            return "❌ 未找到该吐司模板，请检查后台「吐司模板(ID)」是否正确"
        if status == 429:
            return "⚠️ 吐司请求过于频繁（被限流），请稍后再试"
        tail = f"\n{detail}" if detail else ""
        return f"❌ 吐司生图失败（HTTP {status}）{tail}"

    async def _tusi_download_image(self, session, url: str) -> tuple:
        """下载吐司生成的结果图片，返回 (图片字节, 错误文案)"""
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=TUSI_API_TIMEOUT),
                proxy=self._get_proxy(),
            ) as resp:
                if resp.status != 200:
                    return None, f"❌ 下载吐司生成结果失败（HTTP {resp.status}）"
                body = await self._read_capped(resp, TUSI_MAX_IMAGE_BYTES)
                if body is None:
                    return None, "❌ 生成结果过大，已中止接收"
            if not body:
                return None, "❌ 吐司生成结果为空"
            return body, None
        except Exception as e:
            logger.error(f"下载吐司生成结果失败: {e}")
            return None, "❌ 下载吐司生成结果失败，请稍后重试"

    async def _tusi_wait_job(self, session, base: str, headers: Dict[str, str],
                             job_id: str) -> tuple:
        """轮询吐司作业直到完成，返回 (图片字节, 错误文案)"""
        deadline = time.monotonic() + TUSI_JOB_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(TUSI_POLL_INTERVAL)
            try:
                async with session.get(
                    f"{base}/v1/jobs/{job_id}", headers=headers,
                    timeout=aiohttp.ClientTimeout(total=TUSI_API_TIMEOUT),
                    proxy=self._get_proxy(),
                ) as resp:
                    body = await self._read_capped(resp, TUSI_MAX_JSON_BYTES)
                    if body is None:
                        continue
                    if not 200 <= resp.status < 300:
                        logger.warning(f"吐司查询作业失败 HTTP {resp.status}")
                        return None, self._tusi_error_msg(resp.status, body)
                data = json.loads(body.decode("utf-8", "ignore")) or {}
            except Exception as e:
                # 单次轮询失败不致命，等下一轮继续查
                logger.warning(f"吐司查询作业异常（将继续重试）: {e}")
                continue

            job = data.get("job") or {}
            status = str(job.get("status") or "").upper()
            if status == "SUCCESS":
                images = ((job.get("successInfo") or {}).get("images")) or []
                if not images:
                    return None, "❌ 吐司作业已完成，但没有返回图片"
                url = str((images[0] or {}).get("url") or "").strip()
                if not url:
                    return None, "❌ 吐司返回的图片地址为空"
                return await self._tusi_download_image(session, url)
            if status == "FAILED":
                info = job.get("failedInfo") or job.get("message") or job.get("error") or ""
                detail = (json.dumps(info, ensure_ascii=False)
                          if isinstance(info, (dict, list)) else str(info))
                detail = " ".join(detail.split())[:200]
                logger.warning(f"吐司作业失败: {detail}")
                tail = f"\n{detail}" if detail else ""
                return None, f"❌ 吐司生图失败{tail}"
            # WAITING / RUNNING 等状态：继续等待
        return None, "⏰ 吐司生图等待超时，请稍后重试"

    async def _tusi_generate(self, positive: str, negative: str,
                             cfg: Dict[str, Any]) -> tuple:
        """调用吐司（TAMS）工作流模板接口生图，返回 (图片字节, 错误文案)"""
        base = cfg["base_url"]
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
        }
        try:
            session = await self._get_http_session()
            # 1) 取模板参数定义（各模板字段不同，直接问接口最稳）
            async with session.get(
                f"{base}/v1/workflows/{cfg['template_id']}", headers=headers,
                timeout=aiohttp.ClientTimeout(total=TUSI_API_TIMEOUT),
                proxy=self._get_proxy(),
            ) as resp:
                body = await self._read_capped(resp, TUSI_MAX_JSON_BYTES)
                if body is None:
                    return None, "❌ 吐司模板信息过大，已中止接收"
                if not 200 <= resp.status < 300:
                    logger.warning(f"吐司取模板失败 HTTP {resp.status}")
                    return None, self._tusi_error_msg(resp.status, body)
            try:
                template = json.loads(body.decode("utf-8", "ignore")) or {}
            except Exception as e:
                logger.warning(f"吐司模板响应解析失败: {e}")
                return None, "❌ 吐司返回的模板信息无法解析，请检查接口地址"

            attrs = [dict(a) for a in (((template.get("fields") or {}).get("fieldAttrs")) or [])]
            if not attrs:
                return None, "❌ 该吐司模板没有可填写的参数，请更换「吐司模板(ID)」"

            prompt_attr = self._pick_tusi_attr(
                attrs, cfg["prompt_field"], TUSI_PROMPT_KEYS, TUSI_NEGATIVE_KEYS
            )
            if prompt_attr is None:
                return None, (
                    "❌ 未能识别吐司模板中的提示词字段\n"
                    "请在后台「吐司提示词字段名」中手动填写该字段名"
                )
            prompt_attr["fieldValue"] = positive
            negative_attr = self._pick_tusi_attr(
                attrs, cfg["negative_field"], TUSI_NEGATIVE_KEYS
            )
            if negative_attr is not None:
                negative_attr["fieldValue"] = negative

            # 2) 提交作业
            payload = {
                "requestId": hashlib.md5(
                    f"{cfg['template_id']}{time.time()}".encode("utf-8")
                ).hexdigest(),
                "templateId": cfg["template_id"],
                "fields": {"fieldAttrs": attrs},
            }
            async with session.post(
                f"{base}/v1/jobs/workflow/template", json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=TUSI_API_TIMEOUT),
                proxy=self._get_proxy(),
            ) as resp:
                body = await self._read_capped(resp, TUSI_MAX_JSON_BYTES)
                if body is None:
                    return None, "❌ 吐司返回内容过大，已中止接收"
                if not 200 <= resp.status < 300:
                    logger.warning(f"吐司提交作业失败 HTTP {resp.status}")
                    return None, self._tusi_error_msg(resp.status, body)
            try:
                created = json.loads(body.decode("utf-8", "ignore")) or {}
            except Exception as e:
                logger.warning(f"吐司提交响应解析失败: {e}")
                return None, "❌ 吐司提交作业失败，返回内容无法解析"
            job_id = str(((created.get("job") or {}).get("id")) or "").strip()
            if not job_id:
                logger.warning(f"吐司未返回作业 ID: {str(created)[:200]}")
                return None, "❌ 吐司未返回作业 ID，请稍后重试"

            # 3) 轮询到出图
            return await self._tusi_wait_job(session, base, headers, job_id)
        except aiohttp.ClientConnectionError as e:
            host = urllib.parse.urlparse(base).netloc or base
            logger.error(f"吐司连接失败 {host}: {e}")
            return None, (
                f"❌ 无法连接到吐司接口：{host}\n"
                "请检查后台「吐司接口地址」是否正确；若被网络阻断，"
                "可在后台「HTTP 代理」填写本地代理地址。"
            )
        except asyncio.TimeoutError:
            return None, "⏰ 吐司生图超时，请稍后重试"
        except Exception as e:
            logger.error(f"吐司生图请求失败: {e}")
            return None, "❌ 吐司生图请求失败，请稍后重试"

    # ---------- 指令：/生图 ----------

    @filter.command("生图")
    async def cmd_image_gen(self, event: AstrMessageEvent):
        """/生图 中文描述 → 大模型转英文绘画 tag → 生图并发送

        加「吐司」前缀（/生图 吐司 描述）则改走吐司（TAMS）接口。
        """
        if not self._nai_enabled():
            yield event.plain_result("⚠️「/生图」功能已被管理员关闭")
            return

        raw = self._strip_command(event.message_str, "生图")
        use_tusi, text = _split_tusi_prefix(raw)
        if not text:
            yield event.plain_result(
                "用法：/生图 中文描述\n"
                "      /生图 吐司 中文描述（走吐司接口）\n"
                "示例：/生图 蓝发少女站在樱花树下，微笑，逆光\n"
                "查看配置与可用模型：/生图帮助"
            )
            return
        if len(text) > MAX_NAI_PROMPT_CHARS:
            text = text[:MAX_NAI_PROMPT_CHARS]

        # 按渠道取配置
        nai_cfg = self._nai_cfg()
        tusi_cfg = self._tusi_cfg() if use_tusi else None
        if use_tusi:
            if not (tusi_cfg["api_key"] and tusi_cfg["template_id"]):
                yield event.plain_result(
                    "⚠️ 吐司生图尚未配置完整\n"
                    "请在后台填写「吐司 API Key」与「吐司模板(ID)」，详见 /生图帮助"
                )
                return
        elif not nai_cfg["key"]:
            yield event.plain_result(
                "⚠️ 尚未配置 NovelAI API Key\n请联系管理员在后台插件配置中填写「生图 API Key」"
            )
            return

        denied = self._img_quota_check(event, nai_cfg["limit"])
        if denied:
            yield event.plain_result(denied)
            return

        busy_key = self._get_user_id(event) or self._get_session_key(event)
        if busy_key in self._img_busy:
            yield event.plain_result("⏳ 你上一次生图还在进行中，请稍候再试")
            return

        self._img_busy.add(busy_key)
        try:
            yield event.plain_result("🎨 正在把描述转换成绘画关键词…")
            positive, negative, err = await self._convert_to_tags(text)
            if err:
                yield event.plain_result(err)
                return
            positive, negative = self._normalize_tags(
                positive, negative, nai_cfg["extra_negative"]
            )

            if use_tusi:
                yield event.plain_result(
                    f"🍞 正在提交到吐司（模板 {tusi_cfg['template_id']}），排队/生成中，请稍候…"
                )
                image, err = await self._tusi_generate(positive, negative, tusi_cfg)
            else:
                yield event.plain_result(
                    f"🖌️ 正在生成图片（{nai_cfg['width']}x{nai_cfg['height']} · "
                    f"{nai_cfg['steps']} 步），通常需要十几秒，请稍候…"
                )
                image, err = await self._nai_generate(positive, negative, nai_cfg)
            if err:
                yield event.plain_result(err)
                return

            # 仅在成功后计数，避免网络/配置问题白扣用户次数
            self._img_quota_commit(event, nai_cfg["limit"])
            image_path = self._bytes_to_tempfile(
                image, _sniff_image_suffix(image), "netease_aigen"
            )
            yield event.image_result(image_path)
            self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生图失败: {e}")
            yield event.plain_result("❌ 生图失败，请稍后重试")
        finally:
            self._img_busy.discard(busy_key)

    @filter.command("生图帮助")
    async def cmd_image_gen_help(self, event: AstrMessageEvent):
        """/生图帮助 → 查看生图用法、当前配置与可选用的对话模型 ID"""
        cfg = self._nai_cfg()
        lines = ["🎨 AI 生图", "━━━━━━━━━━━━"]
        if self._nai_enabled():
            lines.append("用法：/生图 中文描述")
            lines.append("示例：/生图 蓝发少女站在樱花树下，微笑，逆光")
            if self._tusi_ready():
                lines.append("走吐司接口：/生图 吐司 中文描述")
        else:
            lines.append("⛔ 当前已被管理员关闭，请在后台插件配置中开启")
        lines.append("")
        lines.append("【NovelAI】")
        lines.append(f"模型：{cfg['model']}")
        lines.append(f"接口：{cfg['url']}")
        lines.append(
            f"尺寸：{cfg['width']}x{cfg['height']} · {cfg['steps']} 步 · "
            f"CFG {cfg['scale']:g} · {cfg['sampler']}"
        )
        lines.append(f"API Key：{'已配置' if cfg['key'] else '未配置'}")

        tusi = self._tusi_cfg()
        tusi_ok = bool(tusi["api_key"] and tusi["template_id"])
        lines.append("")
        lines.append("【吐司】")
        lines.append(f"状态：{'已配置' if tusi_ok else '未配置（需填 API Key 与模板 ID）'}")
        lines.append(f"接口：{tusi['base_url']}")
        lines.append(f"模板：{tusi['template_id'] or '（未填写）'}")

        provider_id = str(self.config.get("nai_tag_provider") or "").strip()
        lines.append("")
        lines.append(f"转换模型：{provider_id or '（AstrBot 当前默认对话模型）'}")
        if cfg["limit"] > 0:
            lines.append(
                f"今日剩余：{self._img_quota_remaining(event, cfg['limit'])}/{cfg['limit']} 次"
            )
        else:
            lines.append("次数限制：不限")

        try:
            providers = list(self.context.get_all_providers())
        except Exception as e:
            logger.warning(f"获取对话模型列表失败: {e}")
            providers = []
        if providers:
            lines.append("")
            lines.append("可选转换模型（在后台「生图 tag 转换模型」下拉里选择）：")
            for i, prov in enumerate(providers[:20], 1):
                try:
                    meta = prov.meta()
                    lines.append(f"{i}. {meta.id}　（{meta.type} / {meta.model}）")
                except Exception:
                    continue

        yield event.plain_result("\n".join(lines))

    # ==================== 指令：创建歌单 ====================

    @filter.command("创建歌单", alias={"创造歌单"})
    async def cmd_create_playlist(self, event: AstrMessageEvent):
        """创建本地歌单文件夹"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        playlist_name = (
            event.message_str.replace("创造歌单", "").replace("创建歌单", "").strip()
        )
        if not playlist_name:
            yield event.plain_result("请输入歌单名称，用法：/创建歌单 歌单名称")
            return

        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if os.path.exists(playlist_dir):
            yield event.plain_result(f"⚠️ 歌单「{playlist_name}」已存在")
            return

        try:
            os.makedirs(playlist_dir, exist_ok=False)
            self._mark_playlist_dir(playlist_dir)
            yield event.plain_result(
                f"✅ 歌单「{playlist_name}」创建成功！\n"
                f"📁 {playlist_name}\n\n"
                f"现在可以用 /选歌 序号 添加歌单 {playlist_name} 下载歌曲了"
            )
        except OSError as e:
            yield event.plain_result(self._err_msg(e, "创建歌单"))

    # ==================== 指令：歌单（列表/播放） ====================

    @filter.command("歌单")
    async def cmd_playlist(self, event: AstrMessageEvent):
        """
        /歌单 歌单名称                      → 生成图片列出所有歌曲
        /歌单 歌单名称 音乐名称              → 播放本地音频文件（语音）
        /歌单 歌单名称 序号                  → 播放指定序号的歌曲（语音）
        /歌单 歌单名称 下载歌曲 序号          → 按序号选歌发送 mp3 文件
        /歌单 歌单名称 下载歌曲 歌曲名         → 联网搜索下载歌曲到歌单并发送 mp3
        """
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        # 只剥离消息开头的唤醒前缀与命令名，避免歌单名中的「歌单」二字被误替换
        # 兼容 AstrBot 传入 "/歌单 xxx" 或 "歌单 xxx" 或 "/歌单/ xxx" 多种情况
        text = self._strip_command(event.message_str, "歌单")

        if not text:
            yield event.plain_result(
                "用法：\n"
                "/歌单 歌单名称 — 查看歌单列表\n"
                "/歌单 歌单名称 歌曲名 — 播放歌单中的歌曲\n"
                "/歌单 歌单名称 序号 — 按序号播放\n"
                "/歌单 歌单名称 下载歌曲 序号 — 按序号发送 mp3 文件\n"
                "/歌单 歌单名称 下载歌曲 歌曲名 — 联网下载并发送文件"
            )
            return

        # 先解析出歌单名 + 剩余文本
        playlist_name, song_name = self._match_playlist_and_song(text)

        # ========== "下载歌曲" 分支 ==========
        if song_name and song_name.startswith("下载歌曲"):
            after = song_name.replace("下载歌曲", "", 1).strip()
            if not playlist_name:
                yield event.plain_result("⚠️ 请指定目标歌单，用法：/歌单 歌单名 下载歌曲 序号/歌曲名")
                return
            if not after:
                yield event.plain_result("⚠️ 请指定序号或歌曲名")
                return

            playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
            if _pdir_err:
                yield event.plain_result(_pdir_err)
                return
            if not os.path.isdir(playlist_dir):
                yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
                return

            # 列出歌单内文件
            try:
                all_files = sorted([
                    f for f in os.listdir(playlist_dir)
                    if os.path.splitext(f)[1].lower() in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg')
                ])
            except OSError as e:
                yield event.plain_result(self._err_msg(e, "读取歌单"))
                return

            if not all_files:
                yield event.plain_result(f"📭 歌单「{playlist_name}」是空的")
                return

            # 如果"下载歌曲"后面是数字序号 → 从本地歌单按序号发送
            if after.isdigit():
                if len(after) > 10:
                    yield event.plain_result(f"⚠️ 序号无效，请使用 1-{len(all_files)} 之间的数字")
                    return
                idx = int(after)
                if idx < 1 or idx > len(all_files):
                    yield event.plain_result(f"⚠️ 序号 {idx} 超出范围，当前共 {len(all_files)} 首")
                    return
                matched = all_files[idx - 1]
                audio_path = os.path.join(playlist_dir, matched)
                yield event.plain_result(
                    f"🎵 正在发送（歌单第 {idx} 首）：{os.path.splitext(matched)[0]}"
                )
                try:
                    yield event.chain_result([File(name=os.path.basename(audio_path), file=audio_path)])
                except Exception as e:
                    yield event.plain_result(self._err_msg(e, "发送文件"))
                # 附上该歌曲的留言（图片或文本）
                async for result in self._emit_comments(event, playlist_dir, matched):
                    yield result
                return

            # 否则视为联网搜索关键词
            async for result in self._download_song_to_playlist(event, playlist_name, after):
                yield result
            return

        # ========== 不带"下载歌曲"分支 ==========
        if not playlist_name:
            yield event.plain_result(f"❌ 未找到匹配的歌单，请检查名称。")
            return

        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        # ===== 模式1：列出歌单 =====
        if not song_name:
            try:
                files = sorted([
                    f for f in os.listdir(playlist_dir)
                    if os.path.splitext(f)[1].lower() in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg')
                ])
            except OSError as e:
                yield event.plain_result(self._err_msg(e, "读取歌单"))
                return

            if not files:
                yield event.plain_result(f"📭 歌单「{playlist_name}」是空的")
                return

            if self._text_mode():
                lines = [f"📋 歌单「{playlist_name}」共 {len(files)} 首\n"]
                for i, f in enumerate(files, 1):
                    lines.append(f"{i}. {os.path.splitext(f)[0]}")
                yield event.plain_result("\n".join(lines))
                return
            try:
                def _draw():
                    return draw_playlist_image(playlist_name, files, _get_music_root(self.config))
                image_io = await asyncio.get_running_loop().run_in_executor(None, _draw)
                image_path = self._bytes_to_tempfile(image_io.getvalue(), ".png", "netease_playlist")
                yield event.image_result(image_path)
                self._schedule_tempfile_cleanup(image_path)
            except Exception as e:
                # 图片渲染失败时回退到文字列表
                lines = [f"📋 歌单「{playlist_name}」共 {len(files)} 首\n"]
                for i, f in enumerate(files, 1):
                    lines.append(f"{i}. {os.path.splitext(f)[0]}")
                yield event.plain_result("\n".join(lines))
            return

        # ===== 模式2：播放歌单中的歌曲 =====
        try:
            files = sorted([
                f for f in os.listdir(playlist_dir)
                if os.path.splitext(f)[1].lower() in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg')
            ])
        except OSError as e:
            yield event.plain_result(self._err_msg(e, "读取歌单"))
            return

        if not files:
            yield event.plain_result(f"📭 歌单「{playlist_name}」是空的")
            return

        # 解析 matched 文件
        matched = None

        # 情况 A：纯数字 → 按序号播放
        if song_name.isdigit():
            if len(song_name) > 10:
                yield event.plain_result(f"⚠️ 序号无效，请使用 1-{len(files)} 之间的数字或歌曲名")
                return
            idx = int(song_name)
            if idx < 1 or idx > len(files):
                yield event.plain_result(f"⚠️ 序号 {idx} 超出范围，当前共 {len(files)} 首")
                return
            matched = files[idx - 1]

        # 情况 B：歌曲名 → 模糊匹配
        else:
            for f in files:
                display_name = os.path.splitext(f)[0]
                if song_name.lower() in display_name.lower():
                    matched = f
                    break
            if not matched:
                yield event.plain_result(
                    f"❌ 在歌单「{playlist_name}」中未找到「{song_name}」\n"
                    f"可用歌曲：{', '.join(os.path.splitext(f)[0] for f in files[:10])}"
                )
                return

        audio_path = os.path.join(playlist_dir, matched)
        yield event.plain_result(f"🎵 正在播放：{os.path.splitext(matched)[0]}")

        try:
            yield event.chain_result([Record(file=audio_path)])
        except Exception:
            try:
                yield event.chain_result([File(name=os.path.basename(audio_path), file=audio_path)])
            except Exception as e:
                yield event.plain_result(self._err_msg(e, "播放"))
        # 附上该歌曲的留言（图片或文本）
        async for result in self._emit_comments(event, playlist_dir, matched):
            yield result

    async def _download_song_to_playlist(self, event, playlist_name: str, keyword: str):
        """
        联网搜索关键词，下载第一首歌曲到指定歌单目录，并发送 mp3 文件。
        """
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在，请先用 /创建歌单 创建")
            return

        # 必须是本插件创建的歌单，防止向音乐根目录下的其它目录写入文件
        marker_err = self._require_plugin_playlist(playlist_name, playlist_dir)
        if marker_err:
            yield event.plain_result(marker_err)
            return

        # 歌单所有权校验
        denied = self._check_admin(playlist_name, playlist_dir, event)
        if denied:
            yield event.plain_result(denied)
            return

        yield event.plain_result(f"🔍 正在搜索「{keyword}」，请稍候...")

        songs = await self._search_songs(keyword, limit=5)
        if not songs:
            yield event.plain_result(f"❌ 未找到「{keyword}」相关歌曲")
            return

        song = songs[0]
        song_id = song.get('id')
        song_name = song.get('name', '未知歌曲')
        artists = song.get('ar') or song.get('artists') or []
        artist_name = artists[0].get('name', '未知歌手') if artists else '未知歌手'

        yield event.plain_result(f"🎵 正在下载「{artist_name} - {song_name}」...")

        try:
            quality = QUALITY_MAP.get(
                self.config.get("audio_quality", "higher"), "higher"
            )
            audio_url = await self._get_song_url(song_id, quality)
            if not audio_url:
                for fallback in ['standard', 'higher']:
                    audio_url = await self._get_song_url(song_id, fallback)
                    if audio_url:
                        break

            if not audio_url:
                yield event.plain_result("❌ 无法获取该歌曲的音频地址，下载失败")
                return

            audio_data, audio_format = await self._download_audio(audio_url)
            if not audio_data:
                logger.warning(f"音频下载失败 song_id={song_id} url={audio_url}")
                yield event.plain_result("❌ 音频下载失败，请稍后重试")
                return

            safe_name = self._sanitize_filename(f"{artist_name} - {song_name}")
            save_path = os.path.join(playlist_dir, f"{safe_name}.{audio_format}")

            try:
                with open(save_path, 'wb') as f:
                    f.write(audio_data)
            except OSError as e:
                yield event.plain_result(self._err_msg(e, "保存文件"))
                return

            # 封面自动保存到歌单文件夹（渲染歌单图时直接读本地）
            await self._save_song_cover(song, playlist_dir, safe_name)

            yield event.plain_result(
                f"✅ 已下载并保存到歌单「{playlist_name}」\n"
                f"🎵 {artist_name} - {song_name}\n"
                f"📁 {playlist_name}/{os.path.basename(save_path)}"
            )

            try:
                yield event.chain_result([File(name=os.path.basename(save_path), file=save_path)])
            except Exception as e:
                yield event.plain_result(self._err_msg(e, "发送文件"))
            # 附上该歌曲的留言（新歌曲通常没有留言）
            async for result in self._emit_comments(event, playlist_dir, save_path):
                yield result

        except Exception as e:
            logger.error(f"下载歌曲到歌单失败: {e}")
            yield event.plain_result("❌ 下载失败，请稍后重试")

    # ==================== 指令：歌单列表 ====================

    @filter.command("歌单列表")
    async def cmd_playlist_list(self, event: AstrMessageEvent):
        """列出声明的音乐根目录下的全部歌单文件夹"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return

        root = _get_music_root(self.config)
        try:
            folders = sorted(
                f for f in os.listdir(root)
                if os.path.isdir(os.path.join(root, f))
                and self._validate_playlist_name(f) is None
            )
        except OSError as e:
            yield event.plain_result(self._err_msg(e, "读取歌单列表"))
            return

        if not folders:
            yield event.plain_result(
                "📭 还没有任何歌单\n"
                "发送 /创建歌单 歌单名 新建一个吧"
            )
            return

        if self._text_mode():
            lines = [f"🎵 歌单列表（共 {len(folders)} 个）", "━━━━━━━━━━━━━━"]
            for i, name in enumerate(folders, 1):
                lines.append(f"{i}. {name}")
            lines.append("")
            lines.append("发送 /歌单 歌单名 查看歌单内容")
            yield event.plain_result("\n".join(lines))
            return

        try:
            def _draw():
                return draw_playlist_list_image(folders)

            image_io = await asyncio.get_running_loop().run_in_executor(None, _draw)
            image_path = self._bytes_to_tempfile(
                image_io.getvalue(), ".png", "netease_pl_list"
            )
            yield event.image_result(image_path)
            self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生成歌单列表图片失败: {e}")
            lines = [f"🎵 歌单列表（共 {len(folders)} 个）", "━━━━━━━━━━━━━━"]
            for i, name in enumerate(folders, 1):
                lines.append(f"{i}. {name}")
            lines.append("")
            lines.append("发送 /歌单 歌单名 查看歌单内容")
            yield event.plain_result("\n".join(lines))

    # ==================== 指令：歌单占用 ====================

    @filter.command("歌单占用")
    async def cmd_playlist_usage(self, event: AstrMessageEvent):
        """统计各歌单文件夹与音乐根目录的总占用大小"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return

        root = _get_music_root(self.config)

        def _collect() -> tuple:
            """遍历根目录，返回 (各歌单大小列表, 总大小, 总文件数)"""
            items = []
            total_size = 0
            total_files = 0
            try:
                folders = sorted(
                    f for f in os.listdir(root)
                    if os.path.isdir(os.path.join(root, f))
                    and self._validate_playlist_name(f) is None
                )
            except OSError:
                return items, total_size, total_files
            for name in folders:
                size = 0
                count = 0
                for dirpath, _dirnames, filenames in os.walk(os.path.join(root, name)):
                    for fn in filenames:
                        fp = os.path.join(dirpath, fn)
                        try:
                            size += os.path.getsize(fp)
                            count += 1
                        except OSError:
                            continue
                items.append((name, size, count))
                total_size += size
                total_files += count
            return items, total_size, total_files

        try:
            items, total_size, total_files = await asyncio.get_running_loop().run_in_executor(None, _collect)
        except Exception as e:
            yield event.plain_result(self._err_msg(e, "统计歌单占用"))
            return

        if not items:
            yield event.plain_result(
                "📭 还没有任何歌单\n"
                "发送 /创建歌单 歌单名 新建一个吧"
            )
            return

        items.sort(key=lambda x: x[1], reverse=True)
        usage_items = [
            {
                "name": name,
                "size": size,
                "count": count,
                "pct": (size / total_size * 100) if total_size else 0,
            }
            for name, size, count in items
        ]

        if self._text_mode():
            lines = [
                f"💾 歌单占用统计（共 {len(items)} 个，{total_files} 个文件）",
                f"总占用：{self._format_size(total_size)}",
                "━━━━━━━━━━━━━━",
            ]
            for i, (name, size, count) in enumerate(items, 1):
                pct = (size / total_size * 100) if total_size else 0
                lines.append(f"{i}. {name}")
                lines.append(f"    {self._format_size(size)}（{count} 个文件，{pct:.1f}%）")
            yield event.plain_result("\n".join(lines))
            return

        try:
            def _draw():
                return draw_playlist_usage_image(usage_items, total_size, total_files)

            image_io = await asyncio.get_running_loop().run_in_executor(None, _draw)
            image_path = self._bytes_to_tempfile(
                image_io.getvalue(), ".png", "netease_pl_usage"
            )
            yield event.image_result(image_path)
            self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生成歌单占用图片失败: {e}")
            lines = [
                f"💾 歌单占用统计（共 {len(items)} 个，{total_files} 个文件）",
                f"总占用：{self._format_size(total_size)}",
                "━━━━━━━━━━━━━━",
            ]
            for i, (name, size, count) in enumerate(items, 1):
                pct = (size / total_size * 100) if total_size else 0
                lines.append(f"{i}. {name}")
                lines.append(f"    {self._format_size(size)}（{count} 个文件，{pct:.1f}%）")
            yield event.plain_result("\n".join(lines))

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        """字节数格式化为易读字符串"""
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} {unit}"
            size /= 1024
        return f"{size:.2f} TB"

    # ==================== 指令：留言 ====================

    @filter.command("留言")
    async def cmd_comment(self, event: AstrMessageEvent):
        """
        /留言 歌单名 序号 内容      → 给歌单中指定序号的歌曲留言
        /留言 歌单名 歌曲名 内容    → 给匹配歌曲留言
        /留言 歌单名 序号           → 查看该歌曲的留言（图片）
        """
        _off = self._feature_off_msg("comment")
        if _off:
            yield event.plain_result(_off)
            return
        raw = event.message_str
        text = raw.replace("留言", " ").strip()
        while text.startswith("/"):
            text = text[1:].strip()
        text = re.sub(r'\s+', ' ', text)

        if not text:
            yield event.plain_result(
                "用法：\n"
                "/留言 歌单名 序号 内容 — 给指定歌曲留言\n"
                "/留言 歌单名 歌曲名 内容 — 给匹配歌曲留言\n"
                "/留言 歌单名 序号 — 查看该歌曲的留言\n"
                "/留言 歌单名 公开 — 开启/关闭公开留言（所有人可写）\n\n"
                "示例：/留言 陌拜QwQ 1 这首歌太好听了！"
            )
            return

        playlist_name, rest = self._match_playlist_and_song(text)
        if not playlist_name:
            yield event.plain_result("❌ 未找到匹配的歌单，请检查名称。")
            return

        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        # ===== 留言公开开关：/留言 歌单名 公开（可反复切换）=====
        if rest and rest.strip() == "公开":
            denied = self._check_admin(playlist_name, playlist_dir, event)
            if denied:
                yield event.plain_result(denied)
                return
            async with self._binding_lock:
                binding = self._load_binding(playlist_dir)
                public = not bool(binding.get('comment_public'))
                binding['comment_public'] = public
                saved = self._save_binding(playlist_dir, binding)
            if not saved:
                yield event.plain_result("❌ 设置失败（磁盘写入异常）")
                return
            if public:
                yield event.plain_result(
                    f"🌐 歌单「{playlist_name}」已开启公开留言\n"
                    f"现在所有人都可以给该歌单的歌曲留言\n"
                    f"再次发送 /留言 {playlist_name} 公开 可关闭"
                )
            else:
                yield event.plain_result(
                    f"🔒 歌单「{playlist_name}」已关闭公开留言\n"
                    f"现在仅歌单主与成员可以留言"
                )
            return

        try:
            files = sorted([
                f for f in os.listdir(playlist_dir)
                if os.path.splitext(f)[1].lower() in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg')
            ])
        except OSError as e:
            yield event.plain_result(self._err_msg(e, "读取歌单"))
            return

        if not files:
            yield event.plain_result(f"📭 歌单「{playlist_name}」是空的")
            return

        if not rest:
            yield event.plain_result("❌ 请指定歌曲序号或名称。")
            return

        parts = rest.split(None, 1)
        selector = parts[0]
        content = parts[1].strip() if len(parts) > 1 else None

        # 留言内容长度上限，避免超长文本导致渲染卡死与文件膨胀（防 DoS）
        if content is not None and len(content) > 50:
            yield event.plain_result("❌ 留言内容过长（最多 50 字）")
            return

        # 匹配歌曲文件
        matched = None
        if selector.isdigit():
            if len(selector) > 10:
                yield event.plain_result(f"⚠️ 序号无效，请使用 1-{len(files)} 之间的数字或歌曲名")
                return
            idx = int(selector)
            if idx < 1 or idx > len(files):
                yield event.plain_result(f"⚠️ 序号 {idx} 超出范围，当前共 {len(files)} 首")
                return
            matched = files[idx - 1]
        else:
            for f in files:
                if selector.lower() in os.path.splitext(f)[0].lower():
                    matched = f
                    break
            if not matched:
                yield event.plain_result(f"❌ 在歌单「{playlist_name}」中未找到「{selector}」")
                return

        # 无内容 → 查看留言
        if content is None:
            sent = False
            async for result in self._emit_comments(event, playlist_dir, matched):
                sent = True
                yield result
            if not sent:
                yield event.plain_result(
                    f"💬 歌曲「{os.path.splitext(matched)[0]}」还没有留言\n"
                    f"发送 /留言 {playlist_name} {selector} 内容 添加留言"
                )
            return

        # 添加留言：开启公开留言后所有人可写；否则仅歌单主/成员可写
        # （未绑定歌单会提示先 /绑定，不再默认放开）
        if not self._load_binding(playlist_dir).get('comment_public'):
            denied = self._check_admin(playlist_name, playlist_dir, event)
            if denied:
                yield event.plain_result(denied)
                return

        # 添加留言
        comments = self._load_comments(playlist_dir)
        key = os.path.basename(matched)
        if key not in comments:
            comments[key] = []
        # 单曲留言条数上限，避免留言刷爆导致文件无限增长（防 DoS）
        if len(comments[key]) >= 100:
            yield event.plain_result("❌ 该歌曲留言已达上限（100 条）")
            return
        comments[key].append({
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "user": self._get_user_name(event),
            "content": content,
        })
        ok = self._save_comments(playlist_dir, comments)
        if not ok:
            yield event.plain_result("❌ 留言保存失败（磁盘写入异常）")
            return

        yield event.plain_result(
            f"✅ 留言成功！\n"
            f"🎵 {os.path.splitext(matched)[0]}\n"
            f"💬 「{content}」"
        )
        # 顺便展示留言（图片或文本）
        async for result in self._emit_comments(event, playlist_dir, matched):
            yield result

    # ==================== 删除辅助 ====================

    def _resolve_song(self, text: str) -> tuple:
        """
        解析歌单 + 歌曲选择器（序号/名称）。
        返回 (playlist_name, playlist_dir, files, matched, selector, rest, error_msg)。
        error_msg 非空表示失败原因；matched 为匹配到的音频文件名。
        """
        playlist_name, rest = self._match_playlist_and_song(text)
        if not playlist_name:
            return (None, None, [], None, None, None, "❌ 未找到匹配的歌单，请检查名称。")
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            return (None, None, [], None, None, None, _pdir_err)
        if not os.path.isdir(playlist_dir):
            return (None, None, [], None, None, None, f"❌ 歌单「{playlist_name}」不存在")
        try:
            files = sorted([
                f for f in os.listdir(playlist_dir)
                if os.path.splitext(f)[1].lower() in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg')
            ])
        except OSError as e:
            return (None, None, [], None, None, None, self._err_msg(e, "读取歌单"))
        if not files:
            return (playlist_name, playlist_dir, [], None, None, None, f"📭 歌单「{playlist_name}」是空的")
        if not rest:
            return (playlist_name, playlist_dir, files, None, None, None, "❌ 请指定歌曲序号或名称。")

        selector = rest.split(None, 1)[0]
        matched = None
        if selector.isdigit():
            if len(selector) > 10:
                return (
                    playlist_name, playlist_dir, files, None, selector, rest,
                    f"⚠️ 序号无效，请使用 1-{len(files)} 之间的数字或歌曲名"
                )
            idx = int(selector)
            if idx < 1 or idx > len(files):
                return (
                    playlist_name, playlist_dir, files, None, selector, rest,
                    f"⚠️ 序号 {idx} 超出范围，当前共 {len(files)} 首"
                )
            matched = files[idx - 1]
        else:
            for f in files:
                if selector.lower() in os.path.splitext(f)[0].lower():
                    matched = f
                    break
            if not matched:
                return (
                    playlist_name, playlist_dir, files, None, selector, rest,
                    f"❌ 在歌单「{playlist_name}」中未找到「{selector}」"
                )
        return (playlist_name, playlist_dir, files, matched, selector, rest, None)

    # ==================== 指令：删除 ====================

    @filter.command("删除留言")
    async def cmd_delete_comment(self, event: AstrMessageEvent):
        """
        /删除留言 歌单名 序号           → 删除该歌曲全部留言
        /删除留言 歌单名 序号 留言序号  → 删除指定的一条留言
        """
        _off = self._feature_off_msg("comment")
        if _off:
            yield event.plain_result(_off)
            return
        raw = event.message_str
        text = raw.replace("删除留言", " ").strip()
        while text.startswith("/"):
            text = text[1:].strip()
        text = re.sub(r'\s+', ' ', text)

        if not text:
            yield event.plain_result(
                "用法：\n"
                "/删除留言 歌单名 序号 — 删除该歌曲全部留言\n"
                "/删除留言 歌单名 序号 留言序号 — 删除指定一条留言\n\n"
                "示例：/删除留言 陌拜QwQ 1\n"
                "示例：/删除留言 陌拜QwQ 1 2"
            )
            return

        playlist_name, playlist_dir, files, matched, selector, rest, err = self._resolve_song(text)
        if err:
            yield event.plain_result(err)
            return

        # 删除类操作需歌单已绑定
        bound_err = self._require_bound(playlist_name, playlist_dir)
        if bound_err:
            yield event.plain_result(bound_err)
            return

        # 歌单所有权校验：删除留言仅歌单主/成员可用
        denied = self._check_admin(playlist_name, playlist_dir, event)
        if denied:
            yield event.plain_result(denied)
            return

        comments = self._load_comments(playlist_dir)
        key = os.path.basename(matched)
        items = comments.get(key, [])
        if not items:
            yield event.plain_result(f"💬 歌曲「{os.path.splitext(matched)[0]}」暂无留言")
            return

        # 删除指定一条
        tail = rest[len(selector):].strip()
        if tail:
            ci_str = tail.split(None, 1)[0]
            if not ci_str.isdigit() or len(ci_str) > 10:
                yield event.plain_result("❌ 留言序号必须是数字，例如 /删除留言 歌单名 1 2")
                return
            ci = int(ci_str)
            if ci < 1 or ci > len(items):
                yield event.plain_result(f"⚠️ 留言序号 {ci} 超出范围，当前共 {len(items)} 条")
                return
            removed = items.pop(ci - 1)
            if not items:
                comments.pop(key, None)
            ok = self._save_comments(playlist_dir, comments)
            if not ok:
                yield event.plain_result("❌ 删除留言失败（磁盘写入异常）")
                return
            yield event.plain_result(
                f"✅ 已删除第 {ci} 条留言：\n"
                f"👤 {removed.get('user', '')} · {removed.get('time', '')}\n"
                f"💬 {removed.get('content', '')}"
            )
            async for result in self._emit_comments(event, playlist_dir, matched):
                yield result
            return

        # 删除全部留言
        comments.pop(key, None)
        ok = self._save_comments(playlist_dir, comments)
        if not ok:
            yield event.plain_result("❌ 删除留言失败（磁盘写入异常）")
            return
        yield event.plain_result(
            f"✅ 已删除歌曲「{os.path.splitext(matched)[0]}」的全部 {len(items)} 条留言"
        )

    @filter.command("删除歌曲")
    async def cmd_delete_song(self, event: AstrMessageEvent):
        """
        /删除歌曲 歌单名 序号  → 删除指定序号歌曲
        /删除歌曲 歌单名 歌名  → 删除匹配名称的歌曲
        """
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        raw = event.message_str
        text = raw.replace("删除歌曲", " ").strip()
        while text.startswith("/"):
            text = text[1:].strip()
        text = re.sub(r'\s+', ' ', text)

        if not text:
            yield event.plain_result(
                "用法：\n"
                "/删除歌曲 歌单名 序号 — 删除指定序号歌曲\n"
                "/删除歌曲 歌单名 歌名 — 删除匹配名称的歌曲\n\n"
                "示例：/删除歌曲 陌拜QwQ 1"
            )
            return

        playlist_name, playlist_dir, files, matched, selector, rest, err = self._resolve_song(text)
        if err:
            yield event.plain_result(err)
            return

        # 删除类操作需歌单已绑定
        bound_err = self._require_bound(playlist_name, playlist_dir)
        if bound_err:
            yield event.plain_result(bound_err)
            return

        # 歌单所有权校验：删除歌曲仅歌单主/成员可用
        denied = self._check_admin(playlist_name, playlist_dir, event)
        if denied:
            yield event.plain_result(denied)
            return

        full_path = os.path.join(playlist_dir, matched)
        try:
            os.remove(full_path)
        except OSError as e:
            yield event.plain_result(self._err_msg(e, "删除文件"))
            return

        # 同步清理该歌曲的留言
        comments = self._load_comments(playlist_dir)
        key = os.path.basename(matched)
        if key in comments:
            comments.pop(key, None)
            self._save_comments(playlist_dir, comments)

        yield event.plain_result(
            f"✅ 已删除歌曲：{os.path.splitext(matched)[0]}\n"
            f"📁 {playlist_name}/{matched}"
        )

    @filter.command("删除歌单")
    async def cmd_delete_playlist(self, event: AstrMessageEvent):
        """
        /删除歌单 歌单名  → 删除整个歌单文件夹（含歌曲与留言）
        """
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        raw = event.message_str
        text = raw.replace("删除歌单", " ").strip()
        while text.startswith("/"):
            text = text[1:].strip()
        text = re.sub(r'\s+', ' ', text)

        if not text:
            yield event.plain_result(
                "用法：\n"
                "/删除歌单 歌单名 — 删除整个歌单（含歌曲和留言）\n\n"
                "示例：/删除歌单 陌拜QwQ"
            )
            return

        playlist_name, _ = self._match_playlist_and_song(text)
        if not playlist_name:
            yield event.plain_result("❌ 未找到匹配的歌单，请检查名称。")
            return
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        # 删除类操作需歌单已绑定
        bound_err = self._require_bound(playlist_name, playlist_dir)
        if bound_err:
            yield event.plain_result(bound_err)
            return

        # 必须为插件创建的歌单（带标记文件），防止误删用户其它目录
        marker_err = self._require_plugin_playlist(playlist_name, playlist_dir)
        if marker_err:
            yield event.plain_result(marker_err)
            return

        # 歌单所有权校验：删除歌单仅歌单主/成员可用
        denied = self._check_admin(playlist_name, playlist_dir, event)
        if denied:
            yield event.plain_result(denied)
            return

        try:
            shutil.rmtree(playlist_dir)
        except OSError as e:
            yield event.plain_result(self._err_msg(e, "删除歌单"))
            return
        yield event.plain_result(f"✅ 已删除歌单「{playlist_name}」")

    # ==================== 指令：歌单绑定 ====================

    @filter.command("绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        """/绑定 歌单名 → 绑定歌单，成为歌单主"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        text = self._strip_command(event.message_str, "绑定")
        if not text:
            yield event.plain_result(
                "用法：\n"
                "/绑定 歌单名 — 绑定歌单（成为歌单主）\n"
                "/绑定邀请 歌单名 @人 — 邀请他人共同管理\n"
                "/同意绑定 歌单名 — 接受邀请\n"
                "/绑定查看 歌单名 — 查看绑定成员\n"
                "/解绑 歌单名 @人 — 歌单主移除成员\n\n"
                "示例：/绑定 陌拜QwQ"
            )
            return

        playlist_name, _ = self._match_playlist_and_song(text)
        if not playlist_name:
            yield event.plain_result("❌ 未找到匹配的歌单，请检查名称。")
            return
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        # 仅允许绑定本插件创建的歌单，避免把任意目录纳入管理
        marker_err = self._require_plugin_playlist(playlist_name, playlist_dir)
        if marker_err:
            yield event.plain_result(marker_err)
            return

        uid = self._get_user_id(event)
        if not uid:
            yield event.plain_result("❌ 无法获取你的用户 ID，绑定失败")
            return
        name = self._get_user_name(event)

        async with self._binding_lock:
            binding = self._load_binding(playlist_dir)
            owner = binding.get('owner') or {}
            if owner.get('user_id'):
                if owner.get('user_id') == uid:
                    err = f"ℹ️ 歌单「{playlist_name}」已经是你绑定的了"
                else:
                    err = (
                        f"❌ 歌单「{playlist_name}」已被 {owner.get('name', '未知用户')} 绑定\n"
                        f"发送 /绑定查看 {playlist_name} 查看成员"
                    )
            else:
                binding['owner'] = {
                    "user_id": uid,
                    "name": name,
                    "bound_at": time.strftime("%Y-%m-%d %H:%M"),
                }
                binding.setdefault('members', [])
                binding.setdefault('pending', [])
                err = None if self._save_binding(playlist_dir, binding) else "❌ 绑定失败（磁盘写入异常）"

        if err:
            yield event.plain_result(err)
            return

        yield event.plain_result(
            f"✅ 已绑定歌单「{playlist_name}」\n"
            f"👑 歌单主：{name}\n"
            f"绑定后，只有歌单主与成员可以添加/删除歌曲、删除留言\n"
            f"邀请他人：/绑定邀请 {playlist_name} @某人"
        )
        async for result in self._emit_binding(event, playlist_dir, playlist_name):
            yield result

    @filter.command("绑定邀请")
    async def cmd_bind_invite(self, event: AstrMessageEvent):
        """/绑定邀请 歌单名 @人 → 邀请他人共同管理（3 分钟内 /同意绑定 生效）"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        text = self._strip_command(event.message_str, "绑定邀请")
        # 去掉 @ 占位文本，只保留歌单名
        cleaned = re.sub(r'\[(At|CQ:at)[^\]]*\]', ' ', text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        target_id, target_name = self._extract_at_target(event)
        if not target_id:
            yield event.plain_result(
                "用法：/绑定邀请 歌单名 @某人\n"
                "被邀请人需在 3 分钟内发送 /同意绑定 歌单名 确认"
            )
            return

        uid = self._get_user_id(event)
        playlist_name = None
        if cleaned:
            playlist_name, _ = self._match_playlist_and_song(cleaned)
        if not playlist_name:
            # 未指定歌单时，若该用户只绑定了一个歌单则默认使用
            owned = self._owned_playlists(uid)
            if len(owned) == 1:
                playlist_name = owned[0]
        if not playlist_name:
            yield event.plain_result(
                "⚠️ 请指定歌单，用法：/绑定邀请 歌单名 @某人\n"
                "（若你已绑定歌单但无法识别，请补充歌单名称）"
            )
            return

        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        async with self._binding_lock:
            binding = self._load_binding(playlist_dir)
            owner = binding.get('owner') or {}
            if owner.get('user_id') != uid:
                err = (
                    f"❌ 只有歌单主可以邀请成员\n"
                    f"当前歌单主：{owner.get('name', '未知用户')}"
                )
            elif target_id == uid:
                err = "❌ 不能邀请自己"
            elif target_id == owner.get('user_id') or any(
                m.get('user_id') == target_id for m in binding.get('members', [])
            ):
                err = "ℹ️ 该用户已经是歌单成员了"
            else:
                binding = self._clean_pending(binding)
                pending = [p for p in binding.get('pending', []) if p.get('user_id') != target_id]
                pending.append({
                    "user_id": target_id,
                    "name": target_name or target_id,
                    "invited_by": uid,
                    "invited_by_name": self._get_user_name(event),
                    "expire_at": time.time() + INVITE_TTL_SECONDS,
                })
                binding['pending'] = pending
                err = None if self._save_binding(playlist_dir, binding) else "❌ 邀请失败（磁盘写入异常）"

        if err:
            yield event.plain_result(err)
            return

        tip = (
            f" 你被邀请共同管理歌单「{playlist_name}」，"
            f"请在 3 分钟内发送 /同意绑定 {playlist_name} 确认"
        )
        if At is not None:
            yield event.chain_result([
                At(qq=target_id, name=target_name or ""),
                Plain(tip),
            ])
        else:
            yield event.plain_result(f"✅ 已邀请 {target_name or target_id}。{tip}")

    @filter.command("同意绑定")
    async def cmd_accept_bind(self, event: AstrMessageEvent):
        """/同意绑定 歌单名 → 接受邀请，成为歌单成员"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        text = self._strip_command(event.message_str, "同意绑定")
        if not text:
            yield event.plain_result("用法：/同意绑定 歌单名")
            return

        playlist_name, _ = self._match_playlist_and_song(text)
        if not playlist_name:
            yield event.plain_result("❌ 未找到匹配的歌单，请检查名称。")
            return
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        uid = self._get_user_id(event)
        async with self._binding_lock:
            binding = self._clean_pending(self._load_binding(playlist_dir))
            pending = binding.get('pending', [])
            hit = next((p for p in pending if p.get('user_id') == uid), None)
            if not hit:
                err = f"❌ 没有找到你针对歌单「{playlist_name}」的有效邀请（可能已过期）"
            else:
                binding['pending'] = [p for p in pending if p.get('user_id') != uid]
                binding.setdefault('members', []).append({
                    "user_id": uid,
                    "name": self._get_user_name(event),
                    "invited": True,
                    "invited_by": hit.get('invited_by_name', ''),
                    "joined_at": time.strftime("%Y-%m-%d %H:%M"),
                })
                err = None if self._save_binding(playlist_dir, binding) else "❌ 加入失败（磁盘写入异常）"

        if err:
            yield event.plain_result(err)
            return

        yield event.plain_result(
            f"✅ 已加入歌单「{playlist_name}」，现在可以一起管理歌曲了\n"
            f"👑 歌单主：{self._owner_name(binding)}"
        )
        async for result in self._emit_binding(event, playlist_dir, playlist_name):
            yield result

    @filter.command("绑定查看")
    async def cmd_bind_view(self, event: AstrMessageEvent):
        """/绑定查看 歌单名 → 查看歌单主与成员（图片）"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        text = self._strip_command(event.message_str, "绑定查看")
        if not text:
            yield event.plain_result("用法：/绑定查看 歌单名")
            return

        playlist_name, _ = self._match_playlist_and_song(text)
        if not playlist_name:
            yield event.plain_result("❌ 未找到匹配的歌单，请检查名称。")
            return
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        if not owner.get('user_id'):
            yield event.plain_result(
                f"ℹ️ 歌单「{playlist_name}」尚未绑定\n"
                f"绑定后只有歌单主与成员可以添加/删除内容\n"
                f"发送 /绑定 {playlist_name} 成为歌单主"
            )
            return

        sent = False
        async for result in self._emit_binding(event, playlist_dir, playlist_name):
            sent = True
            yield result
        if sent:
            return
        # 兜底文字版
        lines = [f"👑 {owner.get('name', '未知用户')}（歌单主）"]
        for m in binding.get('members', []):
            lines.append(f"👤 {m.get('name', '未知用户')}（邀请用户）")
        yield event.plain_result(
            f"📋 歌单「{playlist_name}」绑定成员（{len(lines)} 人）\n"
            + "\n".join(lines)
        )

    @filter.command("解绑")
    async def cmd_unbind(self, event: AstrMessageEvent):
        """/解绑 歌单名 @人 → 歌单主移除成员"""
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        text = self._strip_command(event.message_str, "解绑")
        cleaned = re.sub(r'\[(At|CQ:at)[^\]]*\]', ' ', text)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        target_id, target_name = self._extract_at_target(event)

        if not cleaned or not target_id:
            yield event.plain_result(
                "用法：/解绑 歌单名 @某人（仅歌单主可用）\n"
                "示例：/解绑 陌拜QwQ @小明"
            )
            return

        playlist_name, _ = self._match_playlist_and_song(cleaned)
        if not playlist_name:
            yield event.plain_result("❌ 未找到匹配的歌单，请检查名称。")
            return
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        uid = self._get_user_id(event)
        async with self._binding_lock:
            binding = self._load_binding(playlist_dir)
            owner = binding.get('owner') or {}
            members = binding.get('members', [])
            target = next((m for m in members if m.get('user_id') == target_id), None)
            removed_name = None
            if owner.get('user_id') != uid:
                err = (
                    f"❌ 只有歌单主可以移除成员\n"
                    f"当前歌单主：{owner.get('name', '未知用户')}"
                )
            elif not target:
                err = "❌ 该用户不是歌单成员"
            else:
                binding['members'] = [m for m in members if m.get('user_id') != target_id]
                removed_name = target.get('name', target_name or target_id)
                err = None if self._save_binding(playlist_dir, binding) else "❌ 移除失败（磁盘写入异常）"

        if err:
            yield event.plain_result(err)
            return

        yield event.plain_result(
            f"✅ 已移除成员：{removed_name}\n"
            f"📁 歌单「{playlist_name}」"
        )
        async for result in self._emit_binding(event, playlist_dir, playlist_name):
            yield result

    # ==================== 指令：补封面 ====================

    @filter.command("补封面")
    async def cmd_backfill_covers(self, event: AstrMessageEvent):
        """
        /补封面 歌单名 → 为歌单中缺失本地封面的歌曲联网补齐（cover_*.jpg）
        """
        _off = self._feature_off_msg("playlist")
        if _off:
            yield event.plain_result(_off)
            return
        text = event.message_str.replace("补封面", "").strip()
        while text.startswith("/"):
            text = text[1:].strip()
        text = re.sub(r'\s+', ' ', text)

        if not text:
            yield event.plain_result(
                "用法：\n"
                "/补封面 歌单名 — 为歌单中已有歌曲补齐本地封面\n\n"
                "示例：/补封面 陌拜QwQ"
            )
            return

        playlist_name, _ = self._match_playlist_and_song(text)
        if not playlist_name:
            yield event.plain_result("❌ 未找到匹配的歌单，请检查名称。")
            return
        playlist_dir, _pdir_err = self._resolve_playlist_dir(playlist_name)
        if _pdir_err:
            yield event.plain_result(_pdir_err)
            return
        if not os.path.isdir(playlist_dir):
            yield event.plain_result(f"❌ 歌单「{playlist_name}」不存在")
            return

        # 歌单所有权校验：补封面仅歌单主/成员可用
        denied = self._check_admin(playlist_name, playlist_dir, event)
        if denied:
            yield event.plain_result(denied)
            return

        try:
            files = sorted([
                f for f in os.listdir(playlist_dir)
                if os.path.splitext(f)[1].lower()
                in ('.mp3', '.m4a', '.flac', '.aac', '.wav', '.ogg')
            ])
        except OSError as e:
            yield event.plain_result(self._err_msg(e, "读取歌单"))
            return

        if not files:
            yield event.plain_result(f"📭 歌单「{playlist_name}」是空的")
            return

        need = [
            f for f in files
            if not os.path.exists(
                os.path.join(playlist_dir, "cover_" + os.path.splitext(f)[0] + ".jpg")
            )
        ]
        if not need:
            yield event.plain_result(
                f"✅ 歌单「{playlist_name}」所有歌曲都已有本地封面，无需补齐"
            )
            return

        yield event.plain_result(
            f"🖼️ 歌单「{playlist_name}」共 {len(files)} 首，"
            f"{len(need)} 首缺封面，开始联网补齐，请稍候..."
        )

        ok = 0
        failed = 0
        for f in need:
            base = os.path.splitext(f)[0]
            keyword = base.replace(' - ', ' ')
            try:
                songs = await self._search_songs(keyword, limit=1)
                if not songs:
                    failed += 1
                    continue
                if await self._save_song_cover(songs[0], playlist_dir, base):
                    ok += 1
                else:
                    failed += 1
            except Exception as e:
                logger.debug(f"补封面失败 {f}: {e}")
                failed += 1

        yield event.plain_result(
            f"✅ 封面补齐完成：成功 {ok} 首，失败 {failed} 首\n"
            f"发送 /歌单 {playlist_name} 查看效果"
        )

    # ==================== 指令：导入 Cookie ====================

    @filter.command("导入cookie")
    async def cmd_import_cookie(self, event: AstrMessageEvent):
        """
        导入浏览器 Cookie 以获取网易云会员权限。
        
        用法: /导入cookie cookie1=value1; cookie2=value2; ...
              /导入cookie  (不带参数查看教程)
        """
        _admin = self._require_admin(event)
        if _admin:
            yield event.plain_result(_admin)
            return
        text = event.message_str.replace("导入cookie", "").strip()
        
        if not text:
            guide = (
                "📖 **网易云 Cookie 导入教程**\n\n"
                "1️⃣ 浏览器打开 music.163.com 并登录你的会员账号\n"
                "2️⃣ 按 F12 打开开发者工具 → 点 Application(应用程序)\n"
                "3️⃣ 左侧 Cookies 展开 → 点 https://music.163.com\n"
                "4️⃣ 找到 `MUSIC_U` 这一行，复制它的值\n"
                "5️⃣ 发送: /导入cookie MUSIC_U=你复制的值\n\n"
                "💡 可以一次导入多个 Cookie，用分号分隔：\n"
                "   /导入cookie MUSIC_U=xxx; NMTID=yyy; appver=9.1.60\n\n"
                "✅ 导入后即可播放会员音质歌曲！"
            )
            yield event.plain_result(guide)
            return

        try:
            # 解析 cookie 字符串
            new_cookies: Dict[str, str] = {}
            for part in text.split(';'):
                part = part.strip()
                if '=' in part:
                    key, value = part.split('=', 1)
                    # 剔除控制字符，防止 CRLF 头注入等问题
                    key = key.strip().replace('\r', '').replace('\n', '').replace('\x00', '')
                    value = value.strip().replace('\r', '').replace('\n', '').replace('\x00', '')
                    if key:
                        new_cookies[key] = value

            if not new_cookies:
                yield event.plain_result(
                    "❌ Cookie 解析失败。正确格式: key1=value1; key2=value2"
                )
                return

            # 合并 cookies
            self._cookies.update(new_cookies)

            # 更新 http session 的 cookies
            if self._http_session and not self._http_session.closed:
                for k, v in new_cookies.items():
                    self._http_session.cookie_jar.update_cookies(
                        {k: v},
                        response_url=aiohttp.client.URL(NETEASE)
                    )

            self._save_cookies()

            # 反馈
            cookie_names = ', '.join(new_cookies.keys())
            yield event.plain_result(
                f"✅ Cookie 导入成功！\n"
                f"已导入: {cookie_names}\n"
                f"现在可以搜索并播放会员音质的歌曲了 🎉\n"
                f"（发送 /点歌 晴天 试试看）"
            )

        except Exception as e:
            logger.error(f"Cookie 导入失败: {e}")
            yield event.plain_result("❌ Cookie 导入失败，请检查格式后重试")

    @filter.command("查看cookie")
    async def cmd_view_cookie(self, event: AstrMessageEvent):
        """查看当前已导入的 Cookie 状态"""
        if not self._cookies:
            yield event.plain_result(
                "📭 当前没有导入任何 Cookie。\n"
                "发送 /导入cookie 查看导入教程。"
            )
            return

        has_music_u = bool(self._cookies.get('MUSIC_U'))
        status = "✅ 已登录（有会员权限）" if has_music_u else "⚠️ 仅导入了部分 Cookie"

        # 只反馈状态与数量，不回显 Cookie 名称，避免在群内泄露登录信息
        yield event.plain_result(
            f"🎵 Cookie 状态\n"
            f"━━━━━━━━━━━━━━\n"
            f"状态: {status}\n"
            f"Cookie 数量: {len(self._cookies)}"
        )

    @filter.command("清除cookie")
    async def cmd_clear_cookie(self, event: AstrMessageEvent):
        """清除所有已导入的 Cookie"""
        _admin = self._require_admin(event)
        if _admin:
            yield event.plain_result(_admin)
            return
        self._cookies = {}
        self._user_info = {}
        if self._http_session and not self._http_session.closed:
            self._http_session.cookie_jar.clear()
        if os.path.exists(self.cookie_file):
            try:
                os.remove(self.cookie_file)
            except OSError as e:
                logger.warning(f"删除 Cookie 文件失败: {e}")
        yield event.plain_result("✅ 已清除所有 Cookie。")

    # ==================== 歌曲 API ====================

    async def _search_songs(self, keyword: str, limit: int = 10) -> List[Dict]:
        """搜索歌曲，返回原始歌曲列表"""
        try:
            data = await self._api_get('/api/cloudsearch/pc', {
                's': keyword,
                'type': 1,
                'limit': limit,
                'offset': 0
            })
            if not data or data.get('code') != 200:
                return []
            return data.get('result', {}).get('songs', [])
        except Exception as e:
            logger.error(f"搜索歌曲失败: {e}")
            return []

    async def _fetch_covers(self, songs: List[Dict]) -> Dict:
        """
        并发下载歌曲封面，返回 {song_id: PIL.Image}。
        单个封面失败不影响整体，自动跳过。
        """
        covers: Dict = {}
        sem = asyncio.Semaphore(4)

        async def _one(song: Dict):
            sid = song.get('id')
            if not sid:
                return None
            pic = (song.get('al') or {}).get('picUrl') or ''
            if not pic:
                return None
            url = pic.replace('http://', 'https://')
            async with sem:
                try:
                    session = await self._get_http_session()
                    async with session.get(url, timeout=6, proxy=self._get_proxy()) as resp:
                        if resp.status != 200:
                            return None
                        # 流式读取并限制大小：content_length 在 chunked 编码下为空，
                        # 必须靠累加校验兜底，防止异常响应撑爆内存
                        if (resp.content_length or 0) > MAX_COVER_BYTES:
                            return None
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            buf.extend(chunk)
                            if len(buf) > MAX_COVER_BYTES:
                                logger.debug(f"封面超过大小上限，已跳过 {sid}")
                                return None
                        data = bytes(buf)
                    img = PILImage.open(BytesIO(data))
                    img = img.convert('RGB')
                    return (sid, img)
                except Exception as e:
                    logger.debug(f"封面下载失败 {sid}: {e}")
                    return None

        results = await asyncio.gather(*[_one(s) for s in songs])
        for r in results:
            if r:
                covers[r[0]] = r[1]
        return covers

    async def _save_song_cover(self, song: Dict, playlist_dir: str, safe_name: str) -> Optional[str]:
        """
        下载歌曲封面并保存到歌单文件夹（cover_<safe_name>.jpg），
        渲染歌单图片时直接读本地文件，避免每次联网。失败记录日志并跳过。
        """
        # 兼容网易云两种字段格式：al/album, picUrl/pic
        al = song.get('al') or song.get('album') or {}
        pic = al.get('picUrl') or al.get('pic') or ''
        if not pic:
            logger.warning(f"封面保存失败: 歌曲数据中无封面字段 song={song.get('id')} name={song.get('name')}")
            return None
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
                'Referer': NETEASE,
            }
            session = await self._get_http_session()
            async with session.get(pic.replace('http://', 'https://'), headers=headers,
                                   timeout=10, proxy=self._get_proxy()) as resp:
                if resp.status != 200:
                    logger.warning(f"封面下载失败: HTTP {resp.status} url={pic}")
                    return None
                # 流式读取并限制大小，防止异常响应撑爆内存
                if (resp.content_length or 0) > MAX_COVER_BYTES:
                    logger.warning(f"封面过大，已跳过 url={pic}")
                    return None
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > MAX_COVER_BYTES:
                        logger.warning(f"封面超过大小上限，已中止 url={pic}")
                        return None
                cover_data = bytes(buf)
            cover_path = os.path.join(playlist_dir, f"cover_{safe_name}.jpg")
            with open(cover_path, 'wb') as f:
                f.write(cover_data)
            logger.info(f"封面已保存: {cover_path} ({len(cover_data)} bytes)")
            return cover_path
        except Exception as e:
            logger.warning(f"封面保存失败: {e} url={pic}")
            return None

    async def _get_song_url(self, song_id: int, level: str = "higher") -> Optional[str]:
        """获取歌曲播放 URL"""
        try:
            data = await self._api_post_form('/api/song/enhance/player/url/v1', {
                'ids': f'[{song_id}]',
                'level': level,
                'encodeType': 'mp3',
                'immerseType': 'c51',
                'cp': 'm1',
                'network': 'WiFi',
                'appver': '9.1.60'
            })

            if data and data.get('code') == 200:
                songs = data.get('data', [])
                if songs and songs[0].get('url'):
                    return songs[0]['url']
                else:
                    # 详细日志
                    if songs:
                        info = songs[0]
                        logger.warning(
                            f"歌曲 {song_id} URL 不可用: "
                            f"code={info.get('code')}, fee={info.get('fee')}, "
                            f"level={info.get('level')}, msg={info.get('message','')}"
                        )
            return None
        except Exception as e:
            logger.error(f"获取歌曲 URL 失败: {e}")
            return None

    # ==================== 辅助方法 ====================

    @staticmethod
    def _err_msg(e: Exception, action: str) -> str:
        """统一异常处理：异常细节写入日志，仅返回通用文案给用户"""
        logger.warning(f"{action}失败: {e}")
        return f"❌ {action}失败，请稍后重试"

    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """缓存按「会话 + 用户」隔离，避免同群多人互相看到/点错搜索结果"""
        return f"session_{event.session_id}_user_{self._get_user_id(event)}"

    def _clean_expired_cache(self):
        now = time.time()
        expired = [
            k for k, v in self.search_cache.items()
            if v.get('expire_time', 0) < now
        ]
        for k in expired:
            del self.search_cache[k]
        # 容量上限：超出时按最早过期顺序淘汰（每项持有完整歌曲列表，避免无界堆积）
        overflow = len(self.search_cache) - MAX_SEARCH_CACHE_ENTRIES
        if overflow > 0:
            oldest = sorted(
                self.search_cache,
                key=lambda k: self.search_cache[k].get('expire_time', 0)
            )
            for k in oldest[:overflow]:
                del self.search_cache[k]

    async def _download_audio(self, url: str) -> tuple:
        proxy = self._get_proxy()
        try:
            fmt = "mp3"
            path_part = url.split('?')[0]
            if '.' in path_part:
                ext = path_part.rsplit('.', 1)[-1].lower()
                if ext in ('mp3', 'm4a', 'flac', 'aac', 'wav'):
                    fmt = ext

            session = await self._get_http_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=60),
                proxy=proxy,
                allow_redirects=True,
                headers=DEFAULT_HEADERS
            ) as resp:
                if resp.status != 200:
                    return None, ''
                # 流式读取并限制单文件上限，避免超大文件撑爆内存
                length = resp.content_length or 0
                if length > MAX_DOWNLOAD_BYTES:
                    logger.warning(f"音频文件过大（{length} 字节），已跳过下载")
                    return None, ''
                chunks = []
                size = 0
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        logger.warning("音频文件超过大小上限，已中止下载")
                        return None, ''
                    chunks.append(chunk)
                return b''.join(chunks), fmt
        except Exception as e:
            logger.error(f"下载音频失败: {e}")
            return None, ''

    # 临时文件清理策略（仅清理本插件产生的文件，且保留 1 小时）
    TEMP_CLEANUP_INTERVAL = 3600  # 两次扫描的最小间隔（秒），避免频繁遍历目录
    TEMP_FILE_MAX_AGE = 3600      # 文件保留时长（秒），超过才删除

    def _cleanup_temp_files(self):
        """节流清理系统临时目录中由本插件产生、且修改时间超过 1 小时的文件"""
        now = time.time()
        if now - self._last_temp_cleanup < self.TEMP_CLEANUP_INTERVAL:
            return
        self._last_temp_cleanup = now
        try:
            tmp_dir = tempfile.gettempdir()
            for name in os.listdir(tmp_dir):
                # 严格按前缀匹配，绝不触碰其它程序的文件
                if not name.startswith(TEMP_FILE_PREFIX):
                    continue
                path = os.path.join(tmp_dir, name)
                try:
                    if (os.path.isfile(path)
                            and now - os.path.getmtime(path) > self.TEMP_FILE_MAX_AGE):
                        os.remove(path)
                except OSError:
                    continue
        except OSError:
            pass

    def _bytes_to_tempfile(self, data: bytes, suffix: str, prefix: str = "file") -> str:
        self._cleanup_temp_files()
        # 调用方传入的 tag 形如 netease_search1，这里统一收敛到 TEMP_FILE_PREFIX 命名空间
        tag = prefix[len("netease_"):] if prefix.startswith("netease_") else prefix
        tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"{TEMP_FILE_PREFIX}{tag}_{int(time.time() * 1000)}{suffix}"
        )
        with open(tmp_path, 'wb') as f:
            f.write(data)
        return tmp_path

    @staticmethod
    def _remove_tempfile(path: str) -> None:
        """删除本插件产生的临时文件（仅限本插件前缀，避免误删其它程序文件）"""
        if not path:
            return
        try:
            name = os.path.basename(path)
            if name.startswith(TEMP_FILE_PREFIX) and os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass

    def _schedule_tempfile_cleanup(self, path: str, delay: int = 180) -> None:
        """延迟删除临时文件：等到框架真正发送完毕后再清理，避免高峰期堆积"""
        if not path:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(delay, self._remove_tempfile, path)
        except RuntimeError:
            pass

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """清理文件名中的非法字符"""
        # 剔除控制字符（\x00-\x1f 及 DEL）
        name = ''.join(ch for ch in name if ch >= ' ' and ch != '\x7f')
        for char in '\\/:*?"<>|':
            name = name.replace(char, '_')
        name = name.strip()
        # 去掉结尾的空格与点号（Windows 不允许结尾为空格或点）
        name = name.rstrip(' .')
        if not name or name in ('.', '..'):
            name = '_'
        # Windows 保留设备名（不区分大小写，含带扩展名的情况）加下划线前缀
        if name.split('.', 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            name = '_' + name
        return name

    def _match_playlist_and_song(self, text: str) -> tuple:
        """
        智能匹配歌单名和歌曲名。
        遍历 D:\\music 下的歌单文件夹，按名称长度从长到短匹配。
        返回 (playlist_name, song_name)，song_name 为 None 表示仅列出歌单。
        """
        try:
            root = _get_music_root(self.config)
            folders = [
                f for f in os.listdir(root)
                if os.path.isdir(os.path.join(root, f))
            ]
        except OSError:
            return (None, None)

        # 按长度从长到短排序，优先匹配更长的歌单名
        folders.sort(key=len, reverse=True)

        for folder in folders:
            if text == folder:
                return (folder, None)
            if text.startswith(folder + " "):
                song = text[len(folder):].strip()
                if song:
                    return (folder, song)

        # 没有精确匹配，尝试用第一个词作为歌单名
        parts = text.split(None, 1)
        if len(parts) == 2:
            first_word = parts[0]
            if first_word in folders:
                return (first_word, parts[1].strip())

        # 只有一个词且匹配歌单名
        if text in folders:
            return (text, None)

        return (None, None)

    # ==================== 歌单绑定 ====================

    @staticmethod
    def _strip_command(text: str, command: str) -> str:
        """去掉消息开头的唤醒前缀与命令名，返回参数文本"""
        t = (text or "").strip()
        while t.startswith("/"):
            t = t[1:].strip()
        if t.startswith(command):
            t = t[len(command):].strip()
        while t.startswith("/"):
            t = t[1:].strip()
        return re.sub(r'\s+', ' ', t)

    @staticmethod
    def _binding_file(playlist_dir: str) -> str:
        """绑定信息文件路径（存放在歌单文件夹内）"""
        return os.path.join(playlist_dir, "binding.json")

    def _load_binding(self, playlist_dir: str) -> Dict:
        """读取绑定信息 {owner:{user_id,name}, members:[...], pending:[...]}"""
        try:
            with open(self._binding_file(playlist_dir), encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_binding(self, playlist_dir: str, data: Dict) -> bool:
        """原子保存绑定信息（先写临时文件再替换，避免进程中断导致文件损坏）"""
        try:
            target = self._binding_file(playlist_dir)
            tmp = target + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
            return True
        except OSError as e:
            logger.error(f"保存绑定信息失败: {e}")
            return False

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取发送者唯一 ID（QQ 号）"""
        try:
            sender = event.message_obj.sender
            for attr in ('user_id', 'id'):
                val = getattr(sender, attr, None)
                if val:
                    return str(val)
        except Exception:
            pass
        return ""

    def _extract_at_target(self, event: AstrMessageEvent) -> tuple:
        """提取消息中被 @ 的用户，返回 (user_id, name)"""
        if At is None:
            return (None, None)
        try:
            for comp in event.message_obj.message:
                if isinstance(comp, At):
                    uid = str(getattr(comp, 'qq', '') or '')
                    if uid:
                        return (uid, str(getattr(comp, 'name', '') or ''))
        except Exception:
            pass
        return (None, None)

    @staticmethod
    def _is_admin(binding: Dict, uid: str) -> bool:
        """歌单管理员判定；歌单未绑定时无人是管理员（需先绑定）"""
        owner = (binding or {}).get('owner') or {}
        if not owner.get('user_id'):
            return False
        if owner.get('user_id') == uid:
            return True
        for m in (binding or {}).get('members', []):
            if m.get('user_id') == uid:
                return True
        return False

    @staticmethod
    def _owner_name(binding: Dict) -> str:
        return ((binding or {}).get('owner') or {}).get('name', '未知用户')

    def _permission_denied(self, playlist_name: str, binding: Dict) -> str:
        return (
            f"🔒 歌单「{playlist_name}」已被 {self._owner_name(binding)} 绑定\n"
            f"仅绑定成员可以添加/删除内容\n"
            f"发送 /绑定查看 {playlist_name} 查看成员"
        )

    @staticmethod
    def _validate_playlist_name(name: str) -> Optional[str]:
        """校验歌单名合法性，非法时返回错误提示，合法返回 None"""
        if not name or not name.strip():
            return "❌ 歌单名不能为空"
        n = name.strip()
        if len(n) > 15:
            return "❌ 歌单名过长（最多 15 个字符）"
        if re.search(r'[\\/:*?"<>|]', n) or '..' in n:
            return (
                "❌ 歌单名包含非法字符\n"
                "不能包含 \\ / : * ? \" < > | 等符号，也不能包含 ..\n"
                "请只使用中文、字母、数字、空格、下划线或连字符"
            )
        if n.endswith(('.', ' ')):
            return "❌ 歌单名不能以点号或空格结尾"
        if n.split('.', 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            return "❌ 歌单名不能使用 Windows 保留名称（CON / PRN / AUX / NUL / COM1-9 / LPT1-9）"
        return None

    def _resolve_playlist_dir(self, name: str) -> tuple:
        """校验并解析歌单目录，返回 (目录路径, 错误提示)，合法时错误提示为 None"""
        err = self._validate_playlist_name(name)
        if err:
            return None, err
        root = _get_music_root(self.config)
        pdir = os.path.join(root, name.strip())
        # 二次保险：确认解析结果确实位于音乐根目录内部。
        # 用 realpath 穿透符号链接/目录 junction，再用 normcase 统一大小写（Windows）
        try:
            real_root = os.path.normcase(os.path.realpath(root))
            real_dir = os.path.normcase(os.path.realpath(pdir))
            if os.path.commonpath([real_dir, real_root]) != real_root:
                return None, "❌ 歌单名非法，不能跳出音乐根目录"
        except ValueError:
            return None, "❌ 歌单名非法"
        return pdir, None

    def _mark_playlist_dir(self, playlist_dir: str) -> None:
        """为插件创建的歌单写入标记文件，用于区分「本插件创建」与用户其它目录"""
        try:
            marker = os.path.join(playlist_dir, PLAYLIST_MARKER)
            with open(marker, 'w', encoding='utf-8') as f:
                f.write("astrbot_plugin_netease_music playlist")
        except OSError as e:
            logger.warning(f"写入歌单标记文件失败: {e}")

    @staticmethod
    def _is_plugin_playlist(playlist_dir: str) -> bool:
        """判断目录是否为插件创建的歌单（带标记文件）"""
        try:
            return os.path.isfile(os.path.join(playlist_dir, PLAYLIST_MARKER))
        except OSError:
            return False

    def _require_plugin_playlist(self, playlist_name: str, playlist_dir: str) -> Optional[str]:
        """删除类操作前置校验：必须是本插件创建的歌单，防止误删用户其它目录"""
        if not self._is_plugin_playlist(playlist_dir):
            return (
                f"⛔ 出于安全考虑，只能删除由本插件创建的歌单\n"
                f"「{playlist_name}」缺少歌单标记，已拒绝操作"
            )
        return None

    def _feature_off_msg(self, feature: str) -> Optional[str]:
        """功能开关校验：关闭时返回提示文案，开启返回 None"""
        if feature == "playlist" and not self.config.get("enable_playlist", True):
            return "⚠️ 歌单功能已被管理员关闭"
        if feature == "comment" and not self.config.get("enable_comment", True):
            return "⚠️ 留言功能已被管理员关闭"
        return None

    def _require_admin(self, event: AstrMessageEvent) -> Optional[str]:
        """AstrBot 全局管理员校验（admins_id）；非管理员返回提示文案，是则返回 None"""
        try:
            if event.is_admin():
                return None
        except Exception as e:
            logger.warning(f"管理员校验失败: {e}")
        return "🔒 该指令仅限 AstrBot 管理员使用"

    def _require_bound(self, playlist_name: str, playlist_dir: str) -> Optional[str]:
        """未绑定歌单不允许执行删除类操作，返回提示文案；已绑定返回 None"""
        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        if not owner.get('user_id'):
            return f"⚠️ 该歌单尚未绑定，请先发送 /绑定 {playlist_name} 成为歌单主后再操作"
        return None

    def _check_admin(self, playlist_name: str, playlist_dir: str, event) -> Optional[str]:
        """校验管理权限，无权限时返回提示文案，有权限返回 None"""
        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        # 未绑定歌单不再默认放开，需先绑定后再管理
        if not owner.get('user_id'):
            return (
                f"⚠️ 歌单「{playlist_name}」尚未绑定，"
                f"请先发送 /绑定 {playlist_name} 成为歌单主后再操作"
            )
        if self._is_admin(binding, self._get_user_id(event)):
            return None
        return self._permission_denied(playlist_name, binding)

    def _clean_pending(self, binding: Dict) -> Dict:
        """剔除已过期的邀请"""
        now = time.time()
        binding['pending'] = [
            p for p in binding.get('pending', [])
            if float(p.get('expire_at', 0)) > now
        ]
        return binding

    def _owned_playlists(self, uid: str) -> List[str]:
        """该用户绑定的全部歌单名"""
        result = []
        root = _get_music_root(self.config)
        try:
            folders = [
                f for f in os.listdir(root)
                if os.path.isdir(os.path.join(root, f))
            ]
        except OSError:
            return result
        for f in folders:
            b = self._load_binding(os.path.join(root, f))
            if ((b.get('owner') or {}).get('user_id')) == uid:
                result.append(f)
        return result

    async def _build_binding_image(self, playlist_dir: str, playlist_name: str) -> Optional[str]:
        """渲染歌单绑定成员图片，失败返回 None"""
        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        if not owner.get('user_id'):
            return None
        members = [{"name": owner.get('name', '未知用户'), "role": "owner"}]
        for m in binding.get('members', []):
            members.append({"name": m.get('name', '未知用户'), "role": "member"})

        def _draw():
            return draw_binding_image(playlist_name, members)

        try:
            image_io = await asyncio.get_running_loop().run_in_executor(None, _draw)
            path = self._bytes_to_tempfile(image_io.getvalue(), ".png", "netease_binding")
            # 统一在此调度清理，调用方无需再关心，避免漏删导致临时文件堆积
            self._schedule_tempfile_cleanup(path)
            return path
        except Exception as e:
            logger.error(f"生成绑定成员图片失败: {e}")
            return None

    # ==================== 留言存储 ====================

    @staticmethod
    def _comments_file(playlist_dir: str) -> str:
        """留言文件路径（存放在歌单文件夹内）"""
        return os.path.join(playlist_dir, "comments.json")

    def _load_comments(self, playlist_dir: str) -> Dict:
        """读取歌单留言数据 {文件名: [{time, user, content}]}"""
        path = self._comments_file(playlist_dir)
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_comments(self, playlist_dir: str, data: Dict) -> bool:
        """保存留言数据到歌单文件夹"""
        path = self._comments_file(playlist_dir)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except OSError as e:
            logger.error(f"保存留言失败: {e}")
            return False

    def _get_user_name(self, event: AstrMessageEvent) -> str:
        """获取留言用户昵称（QQ 昵称）"""
        try:
            sender = event.message_obj.sender
            # AstrBot 的 sender 昵称字段是 nickname，其他平台可能不同，逐个尝试
            for attr in ('nickname', 'user_nickname', 'user_name', 'name'):
                val = getattr(sender, attr, None)
                if val:
                    return str(val)
            uid = getattr(sender, 'user_id', None)
            if uid:
                return str(uid)
            return "匿名"
        except Exception:
            return "匿名"

    async def _build_comments_image(self, playlist_dir: str, filename: str) -> Optional[str]:
        """
        若歌曲存在留言则渲染留言图片并返回临时图片路径，无留言返回 None。
        """
        try:
            comments = self._load_comments(playlist_dir)
            key = os.path.basename(filename)
            items = comments.get(key, [])
            if not items:
                return None
            song_title = os.path.splitext(key)[0]

            def _draw():
                return draw_comments_image(song_title, items)

            image_io = await asyncio.get_running_loop().run_in_executor(None, _draw)
            path = self._bytes_to_tempfile(image_io.getvalue(), ".png", "netease_comments")
            # 统一在此调度清理，调用方无需再关心，避免漏删导致临时文件堆积
            self._schedule_tempfile_cleanup(path)
            return path
        except Exception as e:
            logger.error(f"生成留言图片失败: {e}")
            return None

    # ==================== 文本发送模式 ====================

    def _text_mode(self) -> bool:
        """是否启用「全部以文本发送」（开启后不再输出图片，改用纯文本）"""
        return bool(self.config.get("send_as_text", False))

    def _help_text(self) -> str:
        """帮助信息纯文本版（文本发送模式与图片渲染失败时共用）"""
        lines = [
            "🎵 点歌指令表",
            "搜索后直接发序号（如 1）即可点播",
            "━━━━━━━━━━━━",
            "【点歌】/点歌 /选歌",
            "【歌单】/创建歌单 /歌单列表 /歌单占用 /歌单 /补封面",
            "【留言】/留言",
            "【管理】/删除留言 /删除歌曲 /删除歌单",
            "【绑定】/绑定 /绑定邀请 /同意绑定 /绑定查看 /解绑",
            "【账号】/导入cookie /查看cookie /清除cookie",
        ]
        if self._bili_video_enabled():
            lines.append("【视频】选歌时加「搜索视频」（如 1 搜索视频）→ 再发序号")
        if self._bili_search_cmd_enabled():
            lines.append("【视频】/搜视频 关键词 → 再发序号")
        if self._playlist_search_enabled():
            lines.append("【搜歌单】/搜歌单 关键词 或 /搜歌单 歌单链接")
        if self._nai_enabled():
            lines.append("【生图】/生图 中文描述 · /生图帮助")
        lines += [
            "━━━━━━━━━━━━",
            "【示例】",
            "听歌：/点歌 晴天 → 再发 1",
            "建歌单：/创建歌单 我的歌单",
            "存歌到歌单：/点歌 晴天 → 再发 1 添加歌单 我的歌单",
            "听歌单里的歌：/歌单 我的歌单 → 再发 1",
            "给歌留言：/留言 我的歌单 1 这首歌真好听",
            "登录会员：/导入cookie",
        ]
        if self._bili_search_cmd_enabled():
            lines.append("搜B站视频：/搜视频 关键词 → 再发 5")
        if self._bili_video_enabled():
            lines.append("按歌搜视频：/点歌 歌名 → 再发 1 搜索视频 → 再发 5")
        if self._playlist_search_enabled():
            lines.append("搜/下载歌单：/搜歌单 关键词 → 再发 1 看歌曲 → 发 1 下载歌单")
        if self._nai_enabled():
            lines.append("AI 生图：/生图 蓝发少女站在樱花树下，微笑")
        return "\n".join(lines)

    @staticmethod
    def _format_search_text(keyword: str, songs: List[Dict]) -> str:
        """搜索结果 → 纯文本列表（最多展示 50 条，避免消息过长）"""
        total = len(songs)
        show = songs[:50]
        lines = [f"🔍 搜索结果「{keyword}」共 {total} 首", "━━━━━━━━━━━━━━"]
        for i, s in enumerate(show, 1):
            name = s.get('name', '未知歌曲')
            artists = s.get('ar') or s.get('artists') or []
            artist = "/".join(a.get('name', '') for a in artists[:3]) if artists else '未知歌手'
            lines.append(f"{i}. {name} — {artist}")
        if total > len(show):
            lines.append(f"... 共 {total} 首，仅显示前 {len(show)} 首，可缩小关键词")
        lines.append("")
        lines.append("发送 序号 点播（如 1）；发送 序号 添加歌单 歌单名 可下载到歌单")
        return "\n".join(lines)

    @staticmethod
    def _format_playlists_text(keyword: str, playlists: List[Dict]) -> str:
        """歌单搜索结果 → 纯文本"""
        total = len(playlists)
        lines = [f"🎧 歌单搜索「{keyword}」共 {total} 个", "━━━━━━━━━━━━━━"]
        for i, p in enumerate(playlists, 1):
            lines.append(
                f"{i}. {p.get('name', '未知歌单')}\n"
                f"   {p.get('track_count', 0)} 首 · {p.get('creator', '未知')}"
            )
        lines.append("")
        lines.append("发送 序号 查看歌单歌曲；发送 序号 下载歌单 可整单下载")
        return "\n".join(lines)

    @staticmethod
    def _format_playlist_songs_text(playlist_name: str, songs: List[Dict]) -> str:
        """歌单歌曲 → 纯文本（最多展示 50 条，避免消息过长）"""
        total = len(songs)
        show = songs[:50]
        lines = [f"🎧 歌单「{playlist_name}」共 {total} 首", "━━━━━━━━━━━━━━"]
        for i, s in enumerate(show, 1):
            name = s.get('name', '未知歌曲')
            artists = s.get('ar') or s.get('artists') or []
            artist = "/".join(a.get('name', '') for a in artists[:3]) if artists else '未知歌手'
            lines.append(f"{i}. {name} — {artist}")
        if total > len(show):
            lines.append(f"... 共 {total} 首，仅显示前 {len(show)} 首")
        lines.append("")
        lines.append("发送 序号 即可发送对应歌曲的音频")
        return "\n".join(lines)

    def _format_comments_text(self, playlist_dir: str, filename: str) -> str:
        """某首歌的留言 → 纯文本；无留言返回空串"""
        items = self._load_comments(playlist_dir).get(os.path.basename(filename), [])
        if not items:
            return ""
        title = os.path.splitext(os.path.basename(filename))[0]
        lines = [f"💬 歌曲「{title}」的留言（共 {len(items)} 条）", "━━━━━━━━━━━━━━"]
        for i, c in enumerate(items, 1):
            lines.append(f"{i}. {c.get('user', '匿名')} · {c.get('time', '')}")
            lines.append(f"    {c.get('content', '')}")
        return "\n".join(lines)

    def _format_binding_text(self, playlist_dir: str, playlist_name: str) -> str:
        """歌单绑定成员 → 纯文本；未绑定返回空串"""
        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        if not owner.get('user_id'):
            return ""
        members = binding.get('members', [])
        lines = [
            f"🔗 歌单「{playlist_name}」绑定信息",
            "━━━━━━━━━━━━━━",
            f"👑 歌单主：{owner.get('name', '未知用户')}",
        ]
        if members:
            lines.append(f"👥 成员（{len(members)}）：")
            for m in members:
                lines.append(f"  · {m.get('name', '未知用户')}")
        else:
            lines.append("👥 暂无其他成员")
        return "\n".join(lines)

    async def _emit_comments(self, event, playlist_dir: str, filename: str):
        """按发送模式输出某首歌的留言（默认图片，文本模式为纯文本）"""
        if self._text_mode():
            text = self._format_comments_text(playlist_dir, filename)
            if text:
                yield event.plain_result(text)
            return
        img = await self._build_comments_image(playlist_dir, filename)
        if img:
            yield event.image_result(img)

    async def _emit_binding(self, event, playlist_dir: str, playlist_name: str):
        """按发送模式输出歌单绑定信息（默认图片，文本模式为纯文本）"""
        if self._text_mode():
            text = self._format_binding_text(playlist_dir, playlist_name)
            if text:
                yield event.plain_result(text)
            return
        img = await self._build_binding_image(playlist_dir, playlist_name)
        if img:
            yield event.image_result(img)
