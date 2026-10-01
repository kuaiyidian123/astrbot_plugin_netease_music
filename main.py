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
import asyncio
import tempfile
import shutil
from io import BytesIO
from typing import Dict, List, Optional, Any

# ============= AstrBot API =============
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Image, Record, File
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
    SEARCH_PAGE_SIZE,
    SEARCH_IMG_EXT,
    HELP_IMG_EXT,
    parse_image_size,
)


# ============ 常量 ============
DEFAULT_CONFIG = {
    "search_cache_expire_minutes": 10,
    "max_search_results": 100,
    "search_image_size": "4320x2236",
    "help_image_size": "4320x2236",
    "enable_random_bg_search": False,
    "enable_random_bg_help": False,
    "random_bg_api": "https://uapis.cn/api/v1/random/image?type=pc",
    "random_bg_cache_minutes": 30,
    "allow_unlogged_search": True,
    "audio_quality": "higher",
    "send_method": "auto",
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
# 临时文件命名前缀（独立命名空间，避免与其它程序同前缀文件互相误删）
TEMP_FILE_PREFIX = "netease_music_"

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


@register(
    "astrbot_plugin_netease_music",
    "kuaiyidian123",
    "网易云点歌插件，支持 Cookie 导入登录，搜索歌曲并返回图片列表，选择后以语音发送",
    "1.3.5",
    "https://github.com/kuaiyidian123/astrbot_plugin_netease_music"
)
class NeteaseMusicPlugin(Star):
    """网易云音乐点歌插件"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)

        user_config = dict(config) if config else {}
        self.config = {**DEFAULT_CONFIG, **user_config}

        # 持久化目录
        data_dir = os.path.join(
            os.getcwd(), 'data', 'plugin_data', 'astrbot_plugin_netease_music'
        )
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir

        self.cookie_file = os.path.join(data_dir, 'cookies.json')
        self.search_cache: Dict[str, Dict[str, Any]] = {}
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
        size = parse_image_size(self.config.get("search_image_size"))
        bg_path = await self._get_random_bg_path("search")
        if not bg_path:
            bg_path = _resolve_config_image(self.config.get("search_bg_image"), self.data_dir)
        return size, bg_path

    async def _get_help_image_options(self):
        """帮助图 (分辨率, 背景图路径)，背景优先级同搜索结果图"""
        size = parse_image_size(self.config.get("help_image_size"))
        bg_path = await self._get_random_bg_path("help")
        if not bg_path:
            bg_path = _resolve_config_image(self.config.get("help_bg_image"), self.data_dir)
        return size, bg_path

    # ==================== 随机背景图 ====================

    def _random_bg_cache_path(self, kind: str) -> str:
        """随机背景图的本地缓存文件路径（search / help 各自独立）"""
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

        kind: 'search' 或 'help'，两者开关与缓存均独立，因此可分别控制、背景互不相同。
        未开启随机图、接口异常或图片无法解析时返回 None（由调用方回退到固定背景图）。
        """
        switch_key = "enable_random_bg_search" if kind == "search" else "enable_random_bg_help"
        if not self.config.get(switch_key, False):
            return None
        api = str(self.config.get("random_bg_api") or "").strip()
        if not api:
            return None
        try:
            ttl = max(0, int(self.config.get("random_bg_cache_minutes", 30)))
        except (TypeError, ValueError):
            ttl = 30
        cache_path = self._random_bg_cache_path(kind)

        def _fresh() -> bool:
            # ttl 为 0 表示每次出图都重新获取；否则在有效期内直接复用缓存
            if ttl <= 0 or not os.path.isfile(cache_path):
                return False
            try:
                return time.time() - os.path.getmtime(cache_path) < ttl * 60
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
            params = params or {}
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
            params = params or {}
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
        """列出所有功能与用法，渲染成图片发送"""
        try:
            size, bg_path = await self._get_help_image_options()

            def _draw():
                return draw_help_image(_get_music_root(self.config), size=size, bg_path=bg_path)

            image_io = await asyncio.get_running_loop().run_in_executor(None, _draw)
            image_path = self._bytes_to_tempfile(
                image_io.getvalue(), HELP_IMG_EXT, "netease_help"
            )
            yield event.image_result(image_path)
            self._schedule_tempfile_cleanup(image_path)
        except Exception as e:
            logger.error(f"生成帮助图片失败: {e}")
            # 兜底：文字版帮助
            yield event.plain_result(
                "🎵 网易云点歌插件功能一览\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "【点歌播放】\n"
                "/点歌 歌名 — 搜索歌曲\n"
                "/选歌 序号 — 播放搜索结果\n"
                "/选歌 序号 添加歌单 歌单名 — 下载到歌单\n"
                "【本地歌单】\n"
                "/创建歌单 歌单名 — 新建歌单\n"
                "/歌单列表 — 列出全部歌单\n"
                "/歌单占用 — 查看歌单占用大小\n"
                "/歌单 歌单名 — 查看歌单歌曲\n"
                "/歌单 歌单名 序号/歌名 — 播放(语音)\n"
                "/歌单 歌单名 下载歌曲 序号/歌名 — 发mp3\n"
                "/补封面 歌单名 — 为已有歌曲补齐本地封面\n"
                "【歌曲留言】\n"
                "/留言 歌单名 序号 内容 — 留言\n"
                "/留言 歌单名 序号 — 查看留言\n"
                "/留言 歌单名 公开 — 开启/关闭公开留言\n"
                "【删除管理】\n"
                "/删除留言 歌单名 序号 [留言序号] — 删留言\n"
                "/删除歌曲 歌单名 序号/歌名 — 删歌曲\n"
                "/删除歌单 歌单名 — 删整个歌单\n"
                "【歌单绑定】\n"
                "/绑定 歌单名 — 绑定歌单，成为歌单主\n"
                "/绑定邀请 歌单名 @人 — 邀请他人共同管理\n"
                "/同意绑定 歌单名 — 接受邀请(3分钟内)\n"
                "/绑定查看 歌单名 — 查看绑定成员\n"
                "/解绑 歌单名 @人 — 歌单主移除成员\n"
                "【Cookie】\n"
                "/导入cookie — 教程\n"
                "/导入cookie MUSIC_U=值 — 导入\n"
                "/查看cookie — 状态\n"
                "/清除cookie — 清除"
            )

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

            # 并发下载歌曲封面（失败自动跳过）
            covers = await self._fetch_covers(display_songs)

            size, bg_path = await self._get_search_image_options()

            def _draw():
                return draw_search_result_image(keyword, display_songs, None, covers,
                                                size=size, bg_path=bg_path)
            image_list = await asyncio.get_running_loop().run_in_executor(None, _draw)

            # 缓存搜索结果
            session_key = self._get_session_key(event)
            expire_minutes = self.config.get("search_cache_expire_minutes", 10)
            self.search_cache[session_key] = {
                'expire_time': time.time() + expire_minutes * 60,
                'selected': set(),
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
        self._clean_expired_cache()

        text = event.message_str.replace("选歌", "").strip()

        # 解析：可能是 "序号" 或 "序号 添加歌单 歌单名称"
        parts = text.split(None, 2)
        download_playlist = None

        if len(parts) >= 3 and parts[1] == "添加歌单":
            try:
                index = int(parts[0])
            except ValueError:
                yield event.plain_result("⚠️ 用法：/选歌 序号 或 /选歌 序号 添加歌单 歌单名")
                return
            download_playlist = parts[2].strip()
        else:
            try:
                index = int(text)
            except ValueError:
                yield event.plain_result(
                    "⚠️ 请输入有效的歌曲序号\n"
                    "用法：/选歌 序号\n"
                    "下载到歌单：/选歌 序号 添加歌单 歌单名"
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

                # 歌单所有权校验：已绑定歌单仅歌单主/成员可添加歌曲
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
                # ===== 直接播放模式 =====
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
                    yield event.chain_result([File(name=os.path.basename(audio_path), file=audio_path)])
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

            cache_data.setdefault('selected', set()).add(index)
        except Exception as e:
            logger.error(f"选歌失败: {e}")
            yield event.plain_result("❌ 选歌失败，请稍后重试")

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
        raw = event.message_str
        # 兼容 AstrBot 传入 "/歌单 xxx" 或 "歌单 xxx" 或 "/歌单/ xxx" 多种情况
        text = raw.replace("/歌单", " ").replace("歌单", " ").strip()
        # 清理残留的前导斜杠和多余空格
        while text.startswith("/"):
            text = text[1:].strip()
        text = re.sub(r'\s+', ' ', text)

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
                # 附上该歌曲的留言图片
                img = await self._build_comments_image(playlist_dir, matched)
                if img:
                    yield event.image_result(img)
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
        # 附上该歌曲的留言图片
        img = await self._build_comments_image(playlist_dir, matched)
        if img:
            yield event.image_result(img)

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
            # 附上该歌曲的留言图片（新歌曲通常没有留言）
            img = await self._build_comments_image(playlist_dir, save_path)
            if img:
                yield event.image_result(img)

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
            binding = self._load_binding(playlist_dir)
            public = not bool(binding.get('comment_public'))
            binding['comment_public'] = public
            if not self._save_binding(playlist_dir, binding):
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
            img = await self._build_comments_image(playlist_dir, matched)
            if img:
                yield event.image_result(img)
            else:
                yield event.plain_result(
                    f"💬 歌曲「{os.path.splitext(matched)[0]}」还没有留言\n"
                    f"发送 /留言 {playlist_name} {selector} 内容 添加留言"
                )
            return

        # 添加留言：开启公开留言后所有人可写；否则仅歌单主/成员可写
        # （未绑定歌单 _is_admin 恒为真，等效公开，与其它指令一致）
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
        # 顺便展示留言图片
        img = await self._build_comments_image(playlist_dir, matched)
        if img:
            yield event.image_result(img)

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
            img = await self._build_comments_image(playlist_dir, matched)
            if img:
                yield event.image_result(img)
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

        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        if owner.get('user_id'):
            if owner.get('user_id') == uid:
                yield event.plain_result(f"ℹ️ 歌单「{playlist_name}」已经是你绑定的了")
            else:
                yield event.plain_result(
                    f"❌ 歌单「{playlist_name}」已被 {owner.get('name', '未知用户')} 绑定\n"
                    f"发送 /绑定查看 {playlist_name} 查看成员"
                )
            return

        binding['owner'] = {
            "user_id": uid,
            "name": name,
            "bound_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        binding.setdefault('members', [])
        binding.setdefault('pending', [])
        if not self._save_binding(playlist_dir, binding):
            yield event.plain_result("❌ 绑定失败（磁盘写入异常）")
            return

        yield event.plain_result(
            f"✅ 已绑定歌单「{playlist_name}」\n"
            f"👑 歌单主：{name}\n"
            f"绑定后，只有歌单主与成员可以添加/删除歌曲、删除留言\n"
            f"邀请他人：/绑定邀请 {playlist_name} @某人"
        )
        img = await self._build_binding_image(playlist_dir, playlist_name)
        if img:
            yield event.image_result(img)

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

        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        if owner.get('user_id') != uid:
            yield event.plain_result(
                f"❌ 只有歌单主可以邀请成员\n"
                f"当前歌单主：{owner.get('name', '未知用户')}"
            )
            return
        if target_id == uid:
            yield event.plain_result("❌ 不能邀请自己")
            return
        if target_id == owner.get('user_id') or any(
            m.get('user_id') == target_id for m in binding.get('members', [])
        ):
            yield event.plain_result("ℹ️ 该用户已经是歌单成员了")
            return

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
        if not self._save_binding(playlist_dir, binding):
            yield event.plain_result("❌ 邀请失败（磁盘写入异常）")
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
        binding = self._clean_pending(self._load_binding(playlist_dir))
        pending = binding.get('pending', [])
        hit = next((p for p in pending if p.get('user_id') == uid), None)
        if not hit:
            yield event.plain_result(
                f"❌ 没有找到你针对歌单「{playlist_name}」的有效邀请（可能已过期）"
            )
            return

        binding['pending'] = [p for p in pending if p.get('user_id') != uid]
        binding.setdefault('members', []).append({
            "user_id": uid,
            "name": self._get_user_name(event),
            "invited": True,
            "invited_by": hit.get('invited_by_name', ''),
            "joined_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        if not self._save_binding(playlist_dir, binding):
            yield event.plain_result("❌ 加入失败（磁盘写入异常）")
            return

        yield event.plain_result(
            f"✅ 已加入歌单「{playlist_name}」，现在可以一起管理歌曲了\n"
            f"👑 歌单主：{self._owner_name(binding)}"
        )
        img = await self._build_binding_image(playlist_dir, playlist_name)
        if img:
            yield event.image_result(img)

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
                f"ℹ️ 歌单「{playlist_name}」尚未绑定，任何人都可管理\n"
                f"发送 /绑定 {playlist_name} 成为歌单主"
            )
            return

        img = await self._build_binding_image(playlist_dir, playlist_name)
        if img:
            yield event.image_result(img)
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
        binding = self._load_binding(playlist_dir)
        owner = binding.get('owner') or {}
        if owner.get('user_id') != uid:
            yield event.plain_result(
                f"❌ 只有歌单主可以移除成员\n"
                f"当前歌单主：{owner.get('name', '未知用户')}"
            )
            return

        members = binding.get('members', [])
        target = next((m for m in members if m.get('user_id') == target_id), None)
        if not target:
            yield event.plain_result("❌ 该用户不是歌单成员")
            return

        binding['members'] = [m for m in members if m.get('user_id') != target_id]
        if not self._save_binding(playlist_dir, binding):
            yield event.plain_result("❌ 移除失败（磁盘写入异常）")
            return

        yield event.plain_result(
            f"✅ 已移除成员：{target.get('name', target_name or target_id)}\n"
            f"📁 歌单「{playlist_name}」"
        )
        img = await self._build_binding_image(playlist_dir, playlist_name)
        if img:
            yield event.image_result(img)

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
                        # 封面图较小，但仍设上限防止异常响应撑爆内存
                        if (resp.content_length or 0) > 10 * 1024 * 1024:
                            return None
                        data = await resp.read()
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
            folders = [
                f for f in os.listdir(_get_music_root(self.config))
                if os.path.isdir(os.path.join(_get_music_root(self.config), f))
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
        """歌单管理员判定；歌单未绑定时不做任何限制"""
        owner = (binding or {}).get('owner') or {}
        if not owner.get('user_id'):
            return True
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
