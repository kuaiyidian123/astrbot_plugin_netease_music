"""
图片绘制工具模块
负责生成搜索结果列表图片 / 本地歌单图片 / 帮助图片 / 留言图片
v2 视觉升级：渐变背景 + 光晕 + 卡片深度 + 徽章序号
"""

import os
import threading
from io import BytesIO
from typing import List, Dict, Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance, ImageStat

try:
    from astrbot.api import logger
except Exception:                        # 独立运行 / 单元测试时回退标准库日志
    import logging
    logger = logging.getLogger(__name__)


# ==================== 颜色常量 ====================
COLOR_PRIMARY = (232, 82, 114)             # 网易红（提亮）
COLOR_PRIMARY_LIGHT = (255, 150, 172)      # 浅红（渐变用）
COLOR_ACCENT = (255, 188, 102)             # 金色强调
BG_TOP = (26, 28, 44)                      # 背景顶部（深蓝紫）
BG_BOTTOM = (12, 13, 19)                   # 背景底部（近黑）
GLOW_TOP = (232, 82, 114)                  # 顶部光晕色
GLOW_BOTTOM = (76, 56, 148)                # 底部光晕色
COLOR_CARD_BG = (42, 45, 62)               # 卡片背景
COLOR_CARD_BG_ALT = (49, 52, 72)           # 交替卡片背景
COLOR_CARD_BORDER = (76, 80, 104)          # 卡片描边
COLOR_TEXT_PRIMARY = (246, 247, 251)       # 主文字
COLOR_TEXT_SECONDARY = (188, 192, 208)     # 次文字
COLOR_TEXT_DIM = (142, 146, 164)           # 弱文字
COLOR_INDEX_BG = (66, 70, 92)              # 普通序号底

# ============ 「白色磨砂玻璃」底板上的文字配色（浅色主题） ============
FROST_TEXT_PRIMARY = (26, 28, 36)          # 主文字（近黑）
FROST_TEXT_SECONDARY = (56, 62, 78)        # 次文字（深灰）
FROST_TEXT_DIM = (84, 90, 106)             # 弱文字
FROST_ACCENT = (170, 112, 16)              # 金色强调（浅底上加深以保证可读）
FROST_COVER_RING = (150, 155, 172)         # 封面描边（浅底上加深）

# 插件资源目录
ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
# /点歌 搜索结果图背景图（存在则使用，不存在回退渐变背景）
SEARCH_BG_PATH = os.path.join(ASSETS_DIR, "bg_search.png")
# /点歌帮助 帮助图背景图（与搜索图使用不同的图）
HELP_BG_PATH = os.path.join(ASSETS_DIR, "bg_help.png")


# ==================== 字体 ====================
_FONT_CACHE: Dict[tuple, ImageFont.FreeTypeFont] = {}
_FONT_LOCK = threading.Lock()      # 多线程出图时避免并发重复加载
_FONT_WARNED: set = set()          # 已告警过的字号，避免每次出图刷屏

# 优先尝试的常见中文字库绝对路径（Windows / macOS，命中即用，最快）
_FONT_PATHS_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
]
_FONT_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]

# 系统字体目录（硬编码路径全部失效时，扫描这里找任意中文字库，覆盖 Linux/Docker/精简版 Windows）
_FONT_SCAN_DIRS = [
    "C:/Windows/Fonts",
    os.path.expanduser("~/.fonts"),
    os.path.expanduser("~/.local/share/fonts"),
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
]

# 中文字库文件名关键字：越靠前优先级越高（匹配时已转小写并去空格）
_FONT_HINTS = [
    "msyh", "simhei", "simsun", "microsoftyahei",
    "pingfang", "hiragino", "stheiti", "heiti",
    "notosanscjksc", "notosanscjk", "notoserifcjk",
    "sourcehansanssc", "sourcehansans", "sourcehanserif",
    "wenquanyimicrohei", "wenquanyizenhei", "wqy",
    "droidsansfallback", "arphic", "uming", "ukai",
]
_FONT_EXTS = (".ttf", ".ttc", ".otf", ".otc")
_FONT_HEAVY_WORDS = ("bold", "black", "heavy", "semibold")

_FONT_FILES_CACHE: Optional[List[str]] = None   # 扫描到的中文字体文件（按优先级排序，仅扫一次）

# 插件内置字体目录（系统无中文字体时兜底，如 Docker 镜像）
_BUNDLED_FONT_DIR = os.path.join(ASSETS_DIR, "fonts")


def _bundled_fonts(bold: bool) -> List[str]:
    """列出插件内置字体文件，优先匹配请求的字重（粗体/常规）"""
    if not os.path.isdir(_BUNDLED_FONT_DIR):
        return []
    files = [
        os.path.join(_BUNDLED_FONT_DIR, fn)
        for fn in sorted(os.listdir(_BUNDLED_FONT_DIR))
        if fn.lower().endswith(_FONT_EXTS)
    ]

    def _rank(p: str) -> int:
        heavy = any(k in os.path.basename(p).lower() for k in _FONT_HEAVY_WORDS)
        return 0 if heavy == bold else 1

    return sorted(files, key=_rank)


def _scan_font_files() -> List[str]:
    """扫描系统字体目录，返回按「中文字体优先级」排序的字体文件路径（结果缓存）

    仅在硬编码路径全部失效时调用（Linux / Docker / 精简版 Windows 等）。
    """
    global _FONT_FILES_CACHE
    if _FONT_FILES_CACHE is not None:
        return _FONT_FILES_CACHE

    scored: List[tuple] = []
    seen = set()
    for d in _FONT_SCAN_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if not fn.lower().endswith(_FONT_EXTS):
                    continue
                low = fn.lower().replace(" ", "")
                score = 0
                for i, hint in enumerate(_FONT_HINTS):
                    if hint in low:
                        score = len(_FONT_HINTS) - i     # 越靠前分越高
                        break
                if score == 0:
                    continue
                full = os.path.join(root, fn)
                if full in seen:
                    continue
                seen.add(full)
                scored.append((score, full))

    scored.sort(key=lambda x: -x[0])
    _FONT_FILES_CACHE = [p for _s, p in scored]
    return _FONT_FILES_CACHE


def _load_font(size: int, bold: bool) -> Optional[ImageFont.FreeTypeFont]:
    """真正加载一次字体；所有候选字库都失败时返回 None，并把原因写入日志"""
    last_err = None

    # 1) 常见路径优先（命中即用，最快）
    for path in (_FONT_PATHS_BOLD if bold else _FONT_PATHS):
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:      # 文件被占用 / 内存不足等临时性失败
            last_err = e

    # 2) 插件内置中文字体（Docker 等无系统字体环境兜底）
    for path in _bundled_fonts(bold):
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            last_err = e

    # 3) 仍失败 → 扫描系统字体目录兜底（Linux / 精简版 Windows 等）
    def _rank(p: str) -> int:
        low = os.path.basename(p).lower()
        heavy = any(k in low for k in _FONT_HEAVY_WORDS)
        return 0 if heavy == bold else 1     # 优先匹配请求的字重（粗体/常规）

    for path in sorted(_scan_font_files(), key=_rank):
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            last_err = e

    key = (size, bold)
    if key not in _FONT_WARNED:
        _FONT_WARNED.add(key)
        detail = f"{type(last_err).__name__}: {last_err}" if last_err else "未找到任何中文字库文件"
        logger.warning(
            f"字体加载失败（size={size} bold={bold}）：{detail}；"
            f"请确认系统已安装中文字体（Linux 可安装 fonts-noto-cjk / fonts-wqy-zenhei）"
        )
    return None


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载字体（带缓存），bold=True 时优先使用粗体字库

    加载失败时**不写入缓存**：字库可能因临时占用或内存压力短暂不可用，
    若把失败结果缓存下来，一次抖动就会让之后所有出图都退化成方块字（要重启才恢复）。
    """
    try:
        size = max(1, int(size))
    except (TypeError, ValueError):
        size = 12
    key = (size, bold)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    with _FONT_LOCK:
        cached = _FONT_CACHE.get(key)
        if cached is not None:
            return cached
        font = _load_font(size, bold)
        if font is not None:
            _FONT_CACHE[key] = font
            return font

    # 兜底字体（不支持中文，仅保证不崩）：不缓存，下次调用自动重试真实字库
    try:
        return ImageFont.load_default(size=size)
    except TypeError:               # 旧版 Pillow 的 load_default 不接受 size
        return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    """测量文本宽度"""
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _truncate_text(text: str, max_width: int, font, draw: ImageDraw.ImageDraw) -> str:
    """截断文本以适应最大宽度"""
    if not text:
        return ""
    # 先做字符数粗截断，避免超长文本逐字符测量导致 O(n^2) 卡死（防 DoS）
    if len(text) > 200:
        text = text[:200]
    if _text_width(draw, text, font) <= max_width:
        return text

    ellipsis = "..."
    for i in range(len(text), 0, -1):
        truncated = text[:i] + ellipsis
        if _text_width(draw, truncated, font) <= max_width:
            return truncated
    return text[:1] + ellipsis


def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
    """按像素宽度自动换行（支持中英文混排）"""
    # 先做字符数粗截断，避免超长文本逐字符测量导致 O(n^2) 卡死（防 DoS）
    if len(text) > 1000:
        text = text[:1000]
    lines = []
    current = ""
    for ch in text:
        if _text_width(draw, current + ch, font) <= max_width:
            current += ch
        else:
            if current:
                lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines


def _lerp(c1: tuple, c2: tuple, t: float) -> tuple:
    """两个 RGB 颜色的线性插值"""
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


# ==================== 布局常量 ====================
PADDING = 20
CARD_HEIGHT = 72
CARD_GAP = 10
CARD_START_Y = 110          # 第一张卡片顶部
FOOTER_HEIGHT = 100
TOTAL_WIDTH = 600

# 歌单根目录（与 main.py 的 MUSIC_ROOT 一致）
MUSIC_ROOT = r"D:\music"
PLUGIN_VERSION = "1.4.2"  # 插件版本号（每次更新/修改递增）


# ==================== 背景 ====================
def _make_bg(w: int, h: int, top: tuple, bottom: tuple) -> Image.Image:
    """生成垂直渐变背景"""
    grad = Image.new('RGB', (1, max(1, h)))
    gd = ImageDraw.Draw(grad)
    for y in range(h):
        gd.line([(0, y), (0, y)], fill=_lerp(top, bottom, y / max(1, h - 1)))
    return grad.resize((w, h))


def _make_canvas(w: int, h: int) -> Image.Image:
    """创建渐变背景 + 上下光晕的画布"""
    img = _make_bg(w, h, BG_TOP, BG_BOTTOM)
    overlay = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    # 顶部红色光晕
    od.ellipse([-120, -190, w + 120, 120], fill=(*GLOW_TOP, 42))
    # 底部紫色光晕
    od.ellipse([-80, h - 170, w + 80, h + 70], fill=(*GLOW_BOTTOM, 36))
    img = Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')
    return img


_CANVAS_BG_CACHE: Dict[tuple, Image.Image] = {}


def _make_canvas_with_bg(
    w: int,
    h: int,
    bg_path: str,
    blur: int = 18,
    darken: int = 110,
    fade: int = 90,
) -> Image.Image:
    """背景图效果：整图模糊铺满打底 + 顶部叠加清晰完整原图

    文件缺失/异常时回退渐变背景。
    结果按（尺寸+背景文件修改时间+参数）缓存，多页出图时只计算一次。
    blur: 底层模糊半径；darken: 底层压暗透明度；fade: 顶部清晰图底部淡出高度
    """
    if not bg_path or not os.path.isfile(bg_path):
        return _make_canvas(w, h)
    try:
        mtime = os.path.getmtime(bg_path)
    except OSError:
        mtime = 0
    cache_key = (w, h, bg_path, mtime, blur, darken, fade)
    cached = _CANVAS_BG_CACHE.get(cache_key)
    if cached is not None:
        return cached.copy()
    try:
        orig = Image.open(bg_path).convert('RGB')
    except Exception:
        return _make_canvas(w, h)

    # 0) 大图先降到「画布 2 倍宽」以内，避免无意义的超大图缩放
    max_src_w = max(w * 2, 1600)
    if orig.width > max_src_w:
        ratio = max_src_w / orig.width
        orig = orig.resize((max_src_w, max(1, int(orig.height * ratio + 0.5))), Image.LANCZOS)

    # 1) 底层：等比铺满 + 高斯模糊 + 压暗，保证无留白且不喧宾夺主
    scale = max(w / orig.width, h / orig.height)
    nw = max(1, int(orig.width * scale + 0.5))
    nh = max(1, int(orig.height * scale + 0.5))
    cover = orig.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    cover = cover.crop((left, top, left + w, top + h))
    # 模糊在小图上做再放大：效果接近但快得多（大画布尤其明显）
    sh = 4
    small = cover.resize((max(1, w // sh), max(1, h // sh)), Image.LANCZOS)
    base = small.filter(ImageFilter.GaussianBlur(max(1, blur // sh)))
    base = base.resize((w, h), Image.LANCZOS)
    # 压暗用亮度查表（point 级操作），比整幅 RGBA 合成快得多
    darken = max(0, min(255, darken))
    if darken > 0:
        base = ImageEnhance.Brightness(base).enhance(1 - darken / 255)

    # 2) 顶层：按画布宽度等比缩放的清晰原图，贴顶部，底部渐变淡出融入模糊底
    sharp_h = min(h, max(1, int(orig.height * (w / orig.width) + 0.5)))
    sharp = orig.resize((w, sharp_h), Image.LANCZOS)
    mask = Image.new('L', (w, sharp_h), 255)
    fd = min(fade, sharp_h)
    if fd > 0:
        # 底部淡出：先在 1px 宽的窄条上逐行取色，再一次性放大铺满
        col = Image.new('L', (1, fd))
        cd = ImageDraw.Draw(col)
        for i in range(fd):
            cd.point((0, i), fill=int(255 * (1 - i / fd)))
        mask.paste(col.resize((w, fd), Image.BILINEAR), (0, sharp_h - fd))
    base.paste(sharp, (0, 0), mask)

    # 3) 顶部标题区再压一层轻微暗色渐变，保证白色标题可读
    header_h = min(150, h)
    if header_h > 0:
        col = Image.new('L', (1, header_h))
        cd = ImageDraw.Draw(col)
        for y in range(header_h):
            cd.point((0, y), fill=int(115 * (1 - y / header_h)))
        base.paste(Image.new('RGB', (w, header_h), (0, 0, 0)),
                   (0, 0), col.resize((w, header_h), Image.BILINEAR))

    if len(_CANVAS_BG_CACHE) > 3:
        _CANVAS_BG_CACHE.clear()
    _CANVAS_BG_CACHE[cache_key] = base
    return base.copy()


def _frost_text(draw: ImageDraw.ImageDraw, xy, text: str, font,
                fill=FROST_TEXT_PRIMARY, stroke: Optional[float] = None,
                stroke_fill: tuple = (255, 255, 255)) -> None:
    """磨砂层上写文字：字芯 + 对比色描边

    磨砂层近乎全透明，背景明暗不受控，靠描边勾勒字形保证可读。
    """
    w = int(round(FROST_STROKE if stroke is None else stroke))
    if w > 0:
        draw.text(xy, text, font=font, fill=fill,
                  stroke_width=w, stroke_fill=stroke_fill)
    else:
        draw.text(xy, text, font=font, fill=fill)


def _frost_palette(img: Image.Image, box, s: float = 1.0) -> tuple:
    """按内容区背景的实际亮度自适应选择文字配色

    背景图直接透出（不叠任何材质），既可能很亮也可能很暗，
    故先量一下该区域的亮度再决定用深字还是浅字，并配一条对比色描边，
    避免文字与背景细节糊在一起。
    返回 (primary, secondary, dim, accent, stroke_w, stroke_fill, row_alt)。
    """
    try:
        mean = ImageStat.Stat(img.crop(box).convert('L')).mean[0]
    except Exception:
        mean = 255.0
    if mean >= 140:
        # 亮底：深字 + 细白描边
        return (FROST_TEXT_PRIMARY, FROST_TEXT_SECONDARY, FROST_TEXT_DIM,
                FROST_ACCENT, max(1, int(round(FROST_STROKE * s * 0.6))),
                (255, 255, 255), (0, 0, 0, 20))
    # 暗底：白字 + 深色描边
    return (COLOR_TEXT_PRIMARY, COLOR_TEXT_SECONDARY, COLOR_TEXT_DIM,
            COLOR_ACCENT, max(1, int(round(FROST_STROKE * s))), (0, 0, 0),
            (255, 255, 255, 26))


# ==================== 通用组件 ====================
def _draw_top_accent(draw: ImageDraw.ImageDraw, width: int):
    """顶部渐变红条"""
    strip_h = 6
    for x in range(width):
        t = x / width
        draw.line([(x, 0), (x, strip_h)], fill=_lerp(COLOR_PRIMARY, COLOR_PRIMARY_LIGHT, t))


def _draw_gradient_line(draw, x0: int, x1: int, y: int, c1: tuple, c2: tuple,
                        width: int = 1, img: Image.Image = None):
    """水平渐变线

    传入 img 时用「2px 渐变条放大后粘贴」实现，避免大画布上逐像素绘制；
    未传 img 时回退逐像素绘制（小图开销可忽略）。
    """
    span = x1 - x0
    if span <= 0:
        return
    if img is not None:
        seed = Image.new('RGB', (2, 1))
        seed.putpixel((0, 0), tuple(int(v) for v in c1))
        seed.putpixel((1, 0), tuple(int(v) for v in c2))
        img.paste(seed.resize((span, width), Image.BILINEAR),
                  (x0, y, x0 + span, y + width))
        return
    for x in range(x0, x1):
        t = (x - x0) / max(1, span)
        draw.line([(x, y), (x, y + width - 1)], fill=_lerp(c1, c2, t))


def _draw_vinyl(draw, cx: int, cy: int, r: int, hole_color: tuple = None):
    """绘制小唱片图标"""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                 fill=(38, 41, 58), outline=(86, 90, 112), width=2)
    hr = max(2, r // 4)
    draw.ellipse([cx - hr, cy - hr, cx + hr, cy + hr],
                 fill=hole_color or COLOR_PRIMARY)


def _draw_pill(draw, x0: int, y0: int, text: str, font, bg: tuple, fg: tuple):
    """圆角胶囊（右缘 = x0 + 宽度）"""
    tw = _text_width(draw, text, font)
    pad_x = 12
    h = 26
    x1 = x0 + tw + pad_x * 2
    draw.rounded_rectangle([x0, y0, x1, y0 + h], radius=h // 2, fill=bg)
    draw.text((x0 + pad_x, y0 + 3), text, font=font, fill=fg)
    return x1


def _draw_header(
    draw: ImageDraw.ImageDraw,
    title: str,
    subtitle: str,
    count: int,
    count_label: str = "首"
) -> None:
    """绘制标题区：唱片图标+标题 / 计数胶囊 / 副标题 / 渐变分割线"""
    _draw_top_accent(draw, TOTAL_WIDTH)

    # 唱片图标 + 标题
    font_title = _get_font(28, bold=True)
    _draw_vinyl(draw, PADDING + 14, 36, 13)
    title_x = PADDING + 36
    draw.text((title_x, 20), title, font=font_title, fill=COLOR_TEXT_PRIMARY)

    # 计数胶囊（右上）
    font_count = _get_font(13, bold=True)
    _draw_pill(
        draw, TOTAL_WIDTH - PADDING - 1, 22,
        f"{count} {count_label}", font_count,
        COLOR_PRIMARY, (255, 255, 255)
    )

    # 副标题
    font_sub = _get_font(15)
    sub_max_width = TOTAL_WIDTH - PADDING * 2 - 110
    sub_text = _truncate_text(subtitle, sub_max_width, font_sub, draw)
    draw.text((title_x, 64), sub_text, font=font_sub, fill=COLOR_PRIMARY_LIGHT)

    # 渐变分割线
    _draw_gradient_line(
        draw, PADDING, TOTAL_WIDTH - PADDING, 98,
        COLOR_PRIMARY, (76, 56, 148)
    )


def _round_cover(cover: Image.Image, size: int, radius: int) -> Image.Image:
    """把封面缩放为 size×size 并切出圆角（返回 RGBA，可直接 paste）"""
    im = cover.resize((size, size), Image.LANCZOS).convert('RGBA')
    mask = Image.new('L', (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size, size], radius=radius, fill=255)
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(im, (0, 0), mask)
    return out


def _draw_card(
    draw: ImageDraw.ImageDraw,
    img: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    index: int,
    song_title: str,
    detail_text: str,
    highlight: bool = False,
    cover: Optional[Image.Image] = None
) -> None:
    """绘制单张歌曲卡片：封面 + 序号徽章 + 文字"""
    # 卡片背景（交替色）
    bg = COLOR_CARD_BG_ALT if index % 2 == 1 else COLOR_CARD_BG
    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=12,
        fill=bg,
        outline=COLOR_CARD_BORDER,
        width=1
    )

    # 左侧强调条（高亮前3首用红→金渐变）
    bar_x = x + 4
    bar_w = 4
    if highlight:
        for i in range(bar_w):
            t = i / bar_w
            draw.rounded_rectangle(
                [bar_x + i, y + 12, bar_x + i + 1, y + h - 12],
                radius=1,
                fill=_lerp(COLOR_PRIMARY, COLOR_ACCENT, t)
            )
    else:
        draw.rounded_rectangle(
            [bar_x, y + 12, bar_x + bar_w, y + h - 12],
            radius=1,
            fill=(66, 70, 92)
        )

    cover_size = 56
    if cover is not None:
        # 序号徽章（封面左侧独立小圆，不遮挡封面）
        badge_r = 12
        bx = x + 22
        by = y + h // 2
        draw.ellipse(
            [bx - badge_r, by - badge_r, bx + badge_r, by + badge_r],
            fill=COLOR_PRIMARY if highlight else COLOR_INDEX_BG,
            outline=COLOR_ACCENT if highlight else (84, 88, 112),
            width=1
        )
        index_text = str(index + 1)
        font_index = _get_font(13, bold=True)
        iw = _text_width(draw, index_text, font_index)
        ih = (draw.textbbox((0, 0), index_text, font=font_index)[3] -
              draw.textbbox((0, 0), index_text, font=font_index)[1])
        draw.text(
            (bx - iw // 2, by - ih // 2 - 1),
            index_text,
            font=font_index,
            fill=(255, 255, 255)
        )

        # 左侧圆角封面
        cover_x = x + 44
        cover_y = y + (h - cover_size) // 2
        img.paste(
            _round_cover(cover, cover_size, 8),
            (cover_x, cover_y),
            _round_cover(cover, cover_size, 8)
        )
        # 封面描边
        draw.rounded_rectangle(
            [cover_x - 1, cover_y - 1,
             cover_x + cover_size + 1, cover_y + cover_size + 1],
            radius=9,
            outline=COLOR_CARD_BORDER,
            width=2
        )

        text_x = cover_x + cover_size + 14
        text_max_width = w - (text_x - x) - 18
    else:
        # 无封面：保留原大序号圆
        font_index = _get_font(20, bold=True)
        cx = x + 38
        cy = y + h // 2
        r = 19
        if highlight:
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COLOR_PRIMARY)
            draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                         outline=COLOR_ACCENT, width=1)
        else:
            draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                         fill=COLOR_INDEX_BG, outline=(84, 88, 112), width=1)
        index_text = str(index + 1)
        iw = _text_width(draw, index_text, font_index)
        ih = (draw.textbbox((0, 0), index_text, font=font_index)[3] -
              draw.textbbox((0, 0), index_text, font=font_index)[1])
        draw.text(
            (cx - iw // 2, cy - ih // 2 - 2),
            index_text,
            font=font_index,
            fill=(255, 255, 255)
        )
        text_x = x + 74
        text_max_width = w - 88

    font_song = _get_font(18, bold=True)
    song_display = _truncate_text(song_title, text_max_width, font_song, draw)
    draw.text((text_x, y + 12), song_display, font=font_song, fill=COLOR_TEXT_PRIMARY)

    if detail_text:
        font_detail = _get_font(13)
        detail_display = _truncate_text(detail_text, text_max_width, font_detail, draw)
        draw.text((text_x, y + 42), detail_display, font=font_detail, fill=COLOR_TEXT_SECONDARY)


def _draw_footer(
    draw: ImageDraw.ImageDraw,
    hint_text: str,
    footer_y: int,
    width: int = TOTAL_WIDTH
) -> None:
    """绘制底部：渐变分隔线 + 胶囊提示 + 版本号（width 可自定义画布宽度）"""
    _draw_gradient_line(
        draw, PADDING, width - PADDING, footer_y,
        COLOR_PRIMARY, (76, 56, 148)
    )

    font_footer = _get_font(13)
    tw = _text_width(draw, hint_text, font_footer)
    w = tw + 28
    h = 30
    x0 = (width - w) // 2
    y0 = footer_y + 12
    draw.rounded_rectangle(
        [x0, y0, x0 + w, y0 + h], radius=h // 2,
        fill=(34, 37, 52), outline=COLOR_CARD_BORDER, width=1
    )
    draw.text((x0 + 14, y0 + 5), hint_text, font=font_footer, fill=COLOR_TEXT_DIM)

    # 版本号
    font_ver = _get_font(16, bold=True)
    ver_text = f"网易云点歌 v{PLUGIN_VERSION}"
    vw = _text_width(draw, ver_text, font_ver)
    draw.text(
        ((width - vw) // 2, y0 + h + 8),
        ver_text, font=font_ver, fill=COLOR_TEXT_SECONDARY
    )


def _make_placeholder_cover(index: int, size: int = 56) -> Image.Image:
    """生成渐变占位封面（本地歌单歌曲无网络封面时使用）"""
    hue = (index % 5) / 5
    c1 = _lerp((150, 90, 190), (232, 82, 114), hue)
    c2 = _lerp((52, 46, 88), (110, 40, 70), hue)
    img = _make_bg(size, size, c1, c2)
    d = ImageDraw.Draw(img)
    _draw_vinyl(d, size // 2, size // 2, size // 3)
    return img


def _song_detail_from_dict(song: Dict) -> tuple:
    """从搜索接口歌曲字典提取 (歌名, 详情文本)。兼容 ar/al 与 artists/album 字段"""
    name = song.get('name', '未知歌曲')
    artists = song.get('ar') or song.get('artists', [])
    artist_names = "/".join([a.get('name', '') for a in artists[:3]]) if artists else '未知歌手'
    album = (song.get('al') or song.get('album', {})).get('name', '') or ''
    if album:
        detail = f"{artist_names}  ·  {album}"
    else:
        detail = artist_names
    return name, detail


# ==================== 搜索图片（横版三列） ====================
SEARCH_IMG_W = 4320          # 横版搜索图宽度
SEARCH_IMG_H = 2236          # 横版搜索图高度
SEARCH_PAGE_SIZE = 50        # 每张图显示条数
SEARCH_COLUMNS = 3           # 横版最大列数
SEARCH_BASE_COL_W = 1340     # 横版 3 列时的基准列宽（字号以此为缩放基准）
SEARCH_JPEG_QUALITY = 92     # 输出 JPEG 质量（配合 4:4:4 采样，文字清晰且体积小）
SEARCH_IMG_EXT = ".jpg"      # 输出文件后缀（JPEG 编码速度与体积远优于 PNG）
HELP_COLUMNS = 3             # 帮助图列数
HELP_IMG_EXT = ".jpg"        # 帮助图输出后缀
# 帮助图背景额外压暗比例（0~1，越大背景越"实"、文字越清晰；0 表示不额外压暗）
# 帮助图条目多、字号小，背景（尤其是随机照片）透出来会明显影响阅读，故单独加深
HELP_BG_SCRIM = 0.35
# 内容区不叠任何材质，背景图完整透出；文字可读性由
# 「按背景亮度自适应配色 + 对比色描边」保证（见 _frost_palette / _frost_text）。
# 文字描边宽度系数（按缩放换算），保证文字压在任意背景上都可读
FROST_STROKE = 2.0

# 自定义分辨率的合法范围（超出范围视为非法，回退默认值）
SIZE_MIN_W, SIZE_MAX_W = 640, 7680
SIZE_MIN_H, SIZE_MAX_H = 480, 4320


def parse_image_size(value, default: tuple = (SEARCH_IMG_W, SEARCH_IMG_H)) -> tuple:
    """解析「宽x高」分辨率字符串，返回 (w, h)

    兼容 4320x2236 / 4320X2236 / 4320*2236 / 4320 2236 / 4320×2236 等写法；
    留空、格式错误或超出允许范围时回退 default。
    """
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            w, h = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return default
    else:
        text = str(value or "").strip().lower()
        if not text:
            return default
        for sep in ('*', ',', '，', '×', ' ', '\t'):
            text = text.replace(sep, 'x')
        parts = [p for p in text.split('x') if p]
        if len(parts) != 2:
            return default
        try:
            w, h = int(parts[0]), int(parts[1])
        except ValueError:
            return default
    if not (SIZE_MIN_W <= w <= SIZE_MAX_W and SIZE_MIN_H <= h <= SIZE_MAX_H):
        return default
    return (w, h)


def _scale(v: float, s: float) -> int:
    """按缩放系数换算尺寸/偏移，至少为 1"""
    return max(1, int(round(v * s)))


def _pick_columns(w: int, h: int, max_columns: int) -> int:
    """按画布宽高比选择列数

    横版用满列数；竖屏自动减一列，保证每列有足够宽度容纳文字，
    否则竖屏下 3 列会把文字挤到极小、下方留下大片空白。
    """
    if max_columns <= 1:
        return 1
    return max_columns if w / max(1, h) >= 1.4 else max(1, max_columns - 1)


def _bg_canvas_size(bg_path: Optional[str]) -> Optional[tuple]:
    """按背景图的实际分辨率得到画布尺寸（超过上限时等比缩小）

    用于「分辨率留空 = 跟随背景图」：背景图 1:1 铺满、不被裁切，文字再按该尺寸等比缩放。
    背景图缺失或无法读取时返回 None（由调用方回退默认尺寸）。
    """
    if not bg_path or not os.path.isfile(bg_path):
        return None
    try:
        with Image.open(bg_path) as im:
            w, h = im.size
    except Exception:
        return None
    if w <= 0 or h <= 0:
        return None
    ratio = min(1.0, SIZE_MAX_W / w, SIZE_MAX_H / h)
    if ratio < 1.0:
        w, h = max(1, int(w * ratio)), max(1, int(h * ratio))
    return (w, h)


_ROUND_MASK_CACHE: Dict[int, Image.Image] = {}


def _get_round_mask(size: int) -> Image.Image:
    """圆形遮罩（按尺寸缓存，避免逐行重复创建）"""
    mask = _ROUND_MASK_CACHE.get(size)
    if mask is None:
        mask = Image.new('L', (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        if len(_ROUND_MASK_CACHE) > 8:
            _ROUND_MASK_CACHE.clear()
        _ROUND_MASK_CACHE[size] = mask
    return mask


def _paste_round_cover(base: Image.Image, cover: Optional[Image.Image],
                       x: int, y: int, size: int, fallback_index: int,
                       draw: Optional[ImageDraw.ImageDraw] = None,
                       ring: tuple = FROST_COVER_RING) -> None:
    """在 (x, y) 处贴一张圆形封面；cover 为空时用渐变占位封面"""
    if cover is None:
        cover = _make_placeholder_cover(fallback_index, size)
    c = cover.convert('RGB').resize((size, size), Image.LANCZOS)
    base.paste(c, (x, y), _get_round_mask(size))
    (draw or ImageDraw.Draw(base)).ellipse(
        (x, y, x + size - 1, y + size - 1),
        outline=ring, width=2
    )


def _draw_search_page(keyword: str, page_songs: List[Dict], cover_map: Dict,
                      start_index: int, page_no: int, total_pages: int,
                      total_count: int, size: Optional[tuple] = None,
                      bg_path: Optional[str] = None) -> BytesIO:
    """绘制单张横版搜索图（三列紧凑列表）

    size: (宽, 高)；为 None（分辨率留空）时跟随背景图实际分辨率，无背景图则用默认 4320x2236。
    bg_path: 背景图路径，默认插件 assets/bg_search.png。
    列数随画布宽高比自适应（竖屏自动减列），字号按「每列宽度」等比缩放，保证换分辨率后版式一致不溢出。
    """
    bg = bg_path or SEARCH_BG_PATH
    W, H = size or _bg_canvas_size(bg) or (SEARCH_IMG_W, SEARCH_IMG_H)
    cols = _pick_columns(W, H, SEARCH_COLUMNS)
    # 版式基准按画布宽/高比例换算（横版 4320x2236 时与原版式完全一致）
    pw, ph = W / SEARCH_IMG_W, H / SEARCH_IMG_H
    pad_x = _scale(90, pw)
    title_h = _scale(210, ph)
    footer_h = _scale(150, ph)
    col_gap = _scale(60, pw)
    col_w = (W - pad_x * 2 - col_gap * (cols - 1)) // cols
    # 缩放系数按「每列宽度」决定：竖屏列宽更大 → 文字自动放大
    s = col_w / SEARCH_BASE_COL_W
    # 压暗（保证文字可读）在背景生成阶段一次完成，并被缓存复用
    img = _make_canvas_with_bg(W, H, bg, darken=170, fade=_scale(90, s))

    rows_per_col = max(1, (SEARCH_PAGE_SIZE + cols - 1) // cols)
    body_top = title_h
    row_h = (H - title_h - footer_h) // rows_per_col

    # 背景图直接透出（不叠任何材质）；按列表区亮度自适应选文字配色
    list_inset = _scale(46, s)
    panel_box = (list_inset, title_h, W - list_inset, H - footer_h)
    tp, ts, td, ta, stroke_w, stroke_fill, row_alt = _frost_palette(img, panel_box, s)

    # 隔行交替底纹：随底色深浅自动切换薄纱方向
    sd = ImageDraw.Draw(img, 'RGBA')
    for col in range(cols):
        cx = pad_x + col * (col_w + col_gap)
        for row in range(1, rows_per_col, 2):
            cy = body_top + row * row_h
            sd.rounded_rectangle([cx - _scale(14, s), cy + _scale(6, s),
                                  cx + col_w + _scale(14, s), cy + row_h - _scale(8, s)],
                                 radius=_scale(18, s), fill=row_alt)
    draw = ImageDraw.Draw(img)

    # 标题区
    font_title = _get_font(_scale(75, s), bold=True)
    font_sub = _get_font(_scale(40, s))
    draw.text((pad_x, _scale(58, s)), "搜索结果", font=font_title, fill=COLOR_TEXT_PRIMARY)
    tw = _text_width(draw, "搜索结果", font_title)
    sub = _truncate_text(f"「{keyword}」共 {total_count} 首", _scale(1400, s), font_sub, draw)
    draw.text((pad_x + tw + _scale(36, s), _scale(80, s)), sub,
              font=font_sub, fill=COLOR_TEXT_SECONDARY)

    if total_pages > 1:
        font_page = _get_font(_scale(35, s), bold=True)
        page_txt = f"第 {page_no} / {total_pages} 页"
        pw = _text_width(draw, page_txt, font_page)
        pill_h = _scale(52, s)
        pill_y0 = _scale(66, s)
        pill_x0 = W - pad_x - pw - _scale(44, s)
        draw.rounded_rectangle([pill_x0, pill_y0, W - pad_x, pill_y0 + pill_h],
                               radius=pill_h // 2, fill=COLOR_PRIMARY)
        draw.text((pill_x0 + _scale(22, s), pill_y0 + (pill_h - _scale(34, s)) // 2), page_txt,
                  font=font_page, fill=(255, 255, 255))

    # 列表行
    font_idx = _get_font(_scale(45, s), bold=True)
    font_name = _get_font(_scale(45, s), bold=True)
    font_artist = _get_font(_scale(33, s))
    cover_size = max(_scale(40, s), int(row_h * 0.62))

    for i, song in enumerate(page_songs):
        col = i // rows_per_col
        row = i % rows_per_col
        cx = pad_x + col * (col_w + col_gap)
        cy = body_top + row * row_h
        seq = start_index + i + 1

        # 序号（前三名强调，右对齐）
        idx_color = ta if seq <= 3 else td
        idx_txt = str(seq)
        iw = _text_width(draw, idx_txt, font_idx)
        _frost_text(draw, (cx + _scale(96, s) - iw, cy + (row_h - _scale(56, s)) // 2),
                    idx_txt, font_idx, fill=idx_color, stroke=stroke_w,
                    stroke_fill=stroke_fill)

        # 圆形封面
        cov_x = cx + _scale(120, s)
        cov_y = cy + (row_h - cover_size) // 2
        _paste_round_cover(img, cover_map.get(song.get('id')), cov_x, cov_y,
                           cover_size, i, draw)

        # 歌名 / 歌手
        text_x = cov_x + cover_size + _scale(30, s)
        max_w = cx + col_w - text_x
        name, detail = _song_detail_from_dict(song)
        _frost_text(draw, (text_x, cy + row_h // 2 - _scale(50, s)),
                    _truncate_text(name, max_w, font_name, draw),
                    font_name, fill=tp, stroke=stroke_w, stroke_fill=stroke_fill)
        _frost_text(draw, (text_x, cy + row_h // 2 + _scale(10, s)),
                    _truncate_text(detail, max_w, font_artist, draw),
                    font_artist, fill=ts, stroke=stroke_w, stroke_fill=stroke_fill)

    # 底部提示
    foot_y = H - footer_h + _scale(18, s)
    font_hint = _get_font(_scale(38, s))
    end_index = start_index + len(page_songs)
    hint = f"发送 /选歌 <序号> 点播歌曲　本页序号 {start_index + 1}-{end_index}"
    draw.text((pad_x, foot_y + _scale(30, s)), hint, font=font_hint, fill=COLOR_TEXT_SECONDARY)
    ver = f"v{PLUGIN_VERSION}"
    vw = _text_width(draw, ver, font_hint)
    draw.text((W - pad_x - vw, foot_y + _scale(30, s)), ver,
              font=font_hint, fill=COLOR_TEXT_DIM)

    output = BytesIO()
    img.save(output, format='JPEG', quality=SEARCH_JPEG_QUALITY,
             subsampling=0, optimize=False)
    output.seek(0)
    return output


def draw_search_result_image(
    keyword: str,
    songs: List[Dict],
    max_display: Optional[int] = None,
    covers: Optional[Dict] = None,
    size: Optional[tuple] = None,
    bg_path: Optional[str] = None
) -> List[BytesIO]:
    """绘制横版搜索结果图（每页 SEARCH_PAGE_SIZE 首、三列），返回图片列表（自动分页）

    covers: {song_id: PIL.Image} 封面映射
    size: (宽, 高) 自定义分辨率，None 用默认 4320x2236
    bg_path: 自定义背景图路径，None 用插件 assets/bg_search.png
    """
    if max_display is not None:
        songs = songs[:max_display]
    cover_map = covers or {}
    pages = [songs[i:i + SEARCH_PAGE_SIZE]
             for i in range(0, len(songs), SEARCH_PAGE_SIZE)] or [[]]
    total = len(songs)
    return [
        _draw_search_page(keyword, page_songs, cover_map,
                          (pno - 1) * SEARCH_PAGE_SIZE, pno, len(pages), total,
                          size=size, bg_path=bg_path)
        for pno, page_songs in enumerate(pages, 1)
    ]


# ==================== 歌单图片 ====================
def draw_playlist_image(playlist_name: str, files: List[str], music_root: str = MUSIC_ROOT) -> BytesIO:
    """绘制本地歌单列表图片"""
    display_count = len(files)

    total_height = (CARD_START_Y
                    + display_count * (CARD_HEIGHT + CARD_GAP)
                    + FOOTER_HEIGHT)

    img = _make_canvas(TOTAL_WIDTH, total_height)
    draw = ImageDraw.Draw(img)

    _draw_header(draw, "本地歌单", f"「{playlist_name}」", display_count)

    card_x = PADDING
    card_w = TOTAL_WIDTH - PADDING * 2
    for idx in range(display_count):
        filename = files[idx]
        card_y = CARD_START_Y + idx * (CARD_HEIGHT + CARD_GAP)

        # 优先读取歌单文件夹内的本地封面（cover_<歌名>.jpg），无则用占位封面
        cover = None
        cover_path = os.path.join(
            music_root, playlist_name,
            "cover_" + os.path.splitext(filename)[0] + ".jpg"
        )
        if os.path.exists(cover_path):
            try:
                cover = Image.open(cover_path).convert('RGB')
            except Exception:
                cover = None
        if cover is None:
            cover = _make_placeholder_cover(idx)

        display_name = os.path.splitext(filename)[0]
        if ' - ' in display_name:
            artist_str, song_str = display_name.split(' - ', 1)
            detail = f"{artist_str}"
        else:
            song_str = display_name
            detail = ""

        _draw_card(
            draw, img, card_x, card_y, card_w, CARD_HEIGHT,
            idx, song_str, detail,
            highlight=(idx < 3),
            cover=cover
        )

    footer_y = CARD_START_Y + display_count * (CARD_HEIGHT + CARD_GAP) + 4
    _draw_footer(draw, "/歌单 歌单名 序号 播放 · /留言 歌单名 序号 内容 留言", footer_y)

    output = BytesIO()
    img.save(output, format='PNG')
    output.seek(0)
    return output


# ==================== 歌单列表图片 ====================
def draw_playlist_list_image(playlists: List[str]) -> BytesIO:
    """绘制全部歌单列表图片"""
    padding = PADDING
    content_w = TOTAL_WIDTH - padding * 2
    header_h = 104
    footer_h = 98
    row_h = 62
    gap = 10

    font_name = _get_font(18, bold=True)
    font_idx = _get_font(15, bold=True)

    n = max(1, len(playlists))
    total_height = header_h + footer_h + n * row_h + gap * max(0, n - 1)

    img = _make_canvas(TOTAL_WIDTH, total_height)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, "歌单列表", "音乐根目录下的全部歌单", len(playlists), "个")

    y = header_h
    for i, name in enumerate(playlists):
        bg = COLOR_CARD_BG_ALT if i % 2 == 1 else COLOR_CARD_BG
        draw.rounded_rectangle(
            [padding, y, padding + content_w, y + row_h],
            radius=12, fill=bg, outline=COLOR_CARD_BORDER, width=1
        )
        # 左侧红金渐变强调条
        for j in range(3):
            t = j / 2
            draw.rounded_rectangle(
                [padding + 4 + j, y + 10, padding + 5 + j, y + row_h - 10],
                radius=1, fill=_lerp(COLOR_PRIMARY, COLOR_ACCENT, t)
            )
        # 序号圆标
        cx = padding + 28
        cy = y + row_h // 2
        r = 14
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COLOR_PRIMARY)
        num = str(i + 1)
        nw = _text_width(draw, num, font_idx)
        draw.text((cx - nw // 2, cy - 9), num, font=font_idx, fill=(255, 255, 255))

        # 歌单名
        name_x = cx + r + 14
        name_max = padding + content_w - 16 - name_x
        name_text = _truncate_text(name, name_max, font_name, draw)
        draw.text((name_x, cy - 12), name_text, font=font_name, fill=COLOR_TEXT_PRIMARY)

        y += row_h + gap

    _draw_footer(draw, "发送 /歌单 歌单名 查看歌单内容", y - gap + 4)

    output = BytesIO()
    img.save(output, format='PNG')
    output.seek(0)
    return output


# ==================== 歌单占用图片 ====================
def _format_size(num_bytes: int) -> str:
    """字节数格式化为易读字符串"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def draw_playlist_usage_image(items: List[Dict], total_size: int, total_files: int) -> BytesIO:
    """
    绘制歌单占用统计图片。
    items: [{"name": str, "size": int, "count": int, "pct": float}]，建议已按 size 降序
    """
    padding = PADDING
    content_w = TOTAL_WIDTH - padding * 2
    header_h = 104
    summary_h = 56
    footer_h = 98
    row_h = 66
    gap = 10

    font_name = _get_font(17, bold=True)
    font_meta = _get_font(13)
    font_num = _get_font(15, bold=True)
    font_pill = _get_font(14, bold=True)

    n = max(1, len(items))
    total_height = (header_h + summary_h + footer_h
                    + n * row_h + gap * max(0, n - 1))

    img = _make_canvas(TOTAL_WIDTH, total_height)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, "歌单占用", "各歌单磁盘占用统计", len(items), "个")

    # 汇总条
    y = header_h
    sum_h = summary_h - 8
    draw.rounded_rectangle(
        [padding, y, padding + content_w, y + sum_h],
        radius=12, fill=(52, 40, 60), outline=COLOR_CARD_BORDER, width=1
    )
    font_sum = _get_font(16, bold=True)
    sum_text = f"总占用 {_format_size(total_size)}  ·  共 {total_files} 个文件"
    sw = _text_width(draw, sum_text, font_sum)
    draw.text(
        (padding + (content_w - sw) // 2, y + (sum_h - 20) // 2),
        sum_text, font=font_sum, fill=COLOR_ACCENT
    )
    y += summary_h

    for i, it in enumerate(items):
        bg = COLOR_CARD_BG_ALT if i % 2 == 1 else COLOR_CARD_BG
        draw.rounded_rectangle(
            [padding, y, padding + content_w, y + row_h],
            radius=12, fill=bg, outline=COLOR_CARD_BORDER, width=1
        )
        for j in range(3):
            t = j / 2
            draw.rounded_rectangle(
                [padding + 4 + j, y + 10, padding + 5 + j, y + row_h - 10],
                radius=1, fill=_lerp(COLOR_PRIMARY, COLOR_ACCENT, t)
            )
        # 序号圆标
        cx = padding + 28
        cy = y + row_h // 2
        r = 14
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=COLOR_PRIMARY)
        num = str(i + 1)
        nw = _text_width(draw, num, font_num)
        draw.text((cx - nw // 2, cy - 9), num, font=font_num, fill=(255, 255, 255))

        # 右侧大小胶囊
        size_text = _format_size(it.get("size", 0))
        size_w = _text_width(draw, size_text, font_pill)
        pill_x = padding + content_w - 16 - (size_w + 24)
        _draw_pill(draw, pill_x, cy - 13, size_text, font_pill, COLOR_PRIMARY, (255, 255, 255))

        # 名称 + 明细
        name_x = cx + r + 14
        name_max = pill_x - name_x - 10
        name_text = _truncate_text(it.get("name", "未知"), name_max, font_name, draw)
        draw.text((name_x, cy - 20), name_text, font=font_name, fill=COLOR_TEXT_PRIMARY)
        meta_text = f"{it.get('count', 0)} 个文件 · {it.get('pct', 0):.1f}%"
        meta_text = _truncate_text(meta_text, name_max, font_meta, draw)
        draw.text((name_x, cy + 2), meta_text, font=font_meta, fill=COLOR_TEXT_SECONDARY)

        y += row_h + gap

    _draw_footer(draw, "发送 /歌单列表 查看全部歌单", y - gap + 4)

    output = BytesIO()
    img.save(output, format='PNG')
    output.seek(0)
    return output


# ==================== 帮助图片 ====================
def draw_help_image(music_root: str = MUSIC_ROOT, size: Optional[tuple] = None,
                    bg_path: Optional[str] = None) -> BytesIO:
    """绘制使用帮助图片

    size: (宽, 高)，默认 4320x2236；bg_path: 背景图路径，默认插件 assets/bg_help.png。
    """
    sections = [
        ("点歌播放", [
            ("/点歌 歌名", "搜索歌曲，返回歌曲图片列表"),
            ("/选歌 序号", "播放搜索结果中的第 N 首"),
            ("/选歌 序号 添加歌单 歌单名", "把搜索到的歌曲下载到本地歌单"),
        ]),
        ("本地歌单", [
            ("/创建歌单 歌单名", f"在 {music_root} 下新建歌单文件夹"),
            ("/歌单列表", "列出全部歌单文件夹"),
            ("/歌单占用", "查看各歌单占用磁盘大小"),
            ("/歌单 歌单名", "生成图片列出歌单全部歌曲"),
            ("/歌单 歌单名 序号/歌名", "播放本地歌曲（语音消息）"),
            ("/歌单 歌单名 下载歌曲 序号", "按序号发送该歌曲 mp3 文件"),
            ("/歌单 歌单名 下载歌曲 歌名", "联网搜索下载到歌单并发送 mp3"),
            ("/补封面 歌单名", "为歌单已有歌曲补齐本地封面"),
        ]),
        ("歌曲留言", [
            ("/留言 歌单名 序号 内容", "给歌单中的歌曲留言"),
            ("/留言 歌单名 序号", "查看该歌曲的留言图片"),
            ("/留言 歌单名 公开", "开启/关闭公开留言（所有人可写）"),
        ]),
        ("删除管理", [
            ("/删除留言 歌单名 序号", "删除该歌曲的全部留言"),
            ("/删除留言 歌单名 序号 留言序号", "删除指定一条留言"),
            ("/删除歌曲 歌单名 序号/歌名", "删除歌单中的歌曲"),
            ("/删除歌单 歌单名", "删除整个歌单（含歌曲留言）"),
        ]),
        ("歌单绑定", [
            ("/绑定 歌单名", "绑定歌单，成为歌单主"),
            ("/绑定邀请 歌单名 @人", "邀请他人在 3 分钟内确认共同管理"),
            ("/同意绑定 歌单名", "接受邀请，成为歌单成员"),
            ("/绑定查看 歌单名", "查看歌单主与成员"),
            ("/解绑 歌单名 @人", "歌单主移除成员"),
        ]),
        ("Cookie 登录", [
            ("/导入cookie", "查看 Cookie 导入教程"),
            ("/导入cookie MUSIC_U=值", "导入会员 Cookie 解锁高音质"),
            ("/查看cookie", "查看当前登录状态"),
            ("/清除cookie", "清除所有 Cookie"),
        ]),
    ]

    # 布局：与搜索图同尺寸同风格，但使用独立背景图 bg_help.png
    bg = bg_path or HELP_BG_PATH
    W, H = size or _bg_canvas_size(bg) or (SEARCH_IMG_W, SEARCH_IMG_H)
    cols = _pick_columns(W, H, HELP_COLUMNS)
    # 版式基准按画布宽/高比例换算（横版 4320x2236 时与原版式完全一致）
    pw, ph = W / SEARCH_IMG_W, H / SEARCH_IMG_H
    pad_x = _scale(90, pw)
    title_h = _scale(210, ph)
    footer_h = _scale(150, ph)
    col_gap = _scale(60, pw)
    col_w = (W - pad_x * 2 - col_gap * (cols - 1)) // cols
    # 缩放系数按「每列宽度」决定：竖屏列宽更大 → 文字自动放大
    s = col_w / SEARCH_BASE_COL_W
    img = _make_canvas_with_bg(W, H, bg, darken=170, fade=_scale(90, s))
    # 背景再压暗一层，降低背景"透明度"，保证密集文字的可读性
    if HELP_BG_SCRIM > 0:
        img = ImageEnhance.Brightness(img).enhance(max(0.0, 1.0 - HELP_BG_SCRIM))
    draw = ImageDraw.Draw(img)

    blocks_top = title_h
    avail_h = H - title_h - footer_h

    # 背景图直接透出（不叠任何材质）；按三列区亮度自适应选文字配色
    inset = _scale(36, s)
    panel_box = (inset, title_h, W - inset, H - footer_h)
    tp, ts, td, ta, stroke_w, stroke_fill, _row_alt = _frost_palette(img, panel_box, s)

    font_title = _get_font(_scale(75, s), bold=True)
    font_sub = _get_font(_scale(40, s))

    # 按分区顺序整体分组到各列（分区不跨列，阅读顺序连续），
    # 再由最长的一列反推块高，保证不溢出画布
    per_col = (len(sections) + cols - 1) // cols
    groups = [sections[i:i + per_col] for i in range(0, len(sections), per_col)]
    columns = []
    for grp in groups:
        blks = []
        for sec_title, items in grp:
            blks.append(('section', sec_title, ''))
            for cmd, desc in items:
                blks.append(('item', cmd, desc))
        columns.append(blks)
    # 块高上限按画布高度换算（竖屏不被 s 压小），并用最长的列铺满可用高度
    block_h = min(_scale(160, ph), avail_h // max(len(c) for c in columns))
    # 块内偏移按块高比例换算，字号随块高缩放，避免块高变化时文字重叠
    y_sec_rect_top = int(block_h * 0.152)
    y_sec_rect_bot = int(block_h * 0.639)
    y_sec_text = int(block_h * 0.139)
    y_sec_line = int(block_h * 0.667)
    y_cmd = int(block_h * 0.097)
    y_desc = int(block_h * 0.528)
    # 字号先由块高决定，再受列宽限制（防止窄列里文字超宽被截断）
    width_cap = max(10, int((col_w - _scale(50, s)) / 15))
    font_sec = _get_font(min(width_cap, max(_scale(33, s), int(block_h * 0.37))), bold=True)
    font_cmd = _get_font(min(width_cap, max(_scale(28, s), int(block_h * 0.33))), bold=True)
    font_desc = _get_font(min(width_cap, max(_scale(21, s), int(block_h * 0.21))))

    # 标题区
    draw.text((pad_x, _scale(58, s)), "点歌帮助", font=font_title, fill=COLOR_TEXT_PRIMARY)
    tw = _text_width(draw, "点歌帮助", font_title)
    draw.text((pad_x + tw + _scale(32, s), _scale(82, s)),
              "网易云音乐点歌插件 · 全部指令与用法",
              font=font_sub, fill=COLOR_TEXT_SECONDARY)
    # 分栏绘制：分区标题块（色条+金色标题+渐变线）、条目块（指令+说明）
    for col_idx in range(len(columns)):
        x = pad_x + col_idx * (col_w + col_gap)
        for bi, blk in enumerate(columns[col_idx]):
            y = blocks_top + bi * block_h
            if blk[0] == 'section':
                draw.rectangle([x, y + y_sec_rect_top, x + _scale(8, s), y + y_sec_rect_bot],
                               fill=COLOR_PRIMARY)
                _frost_text(draw, (x + _scale(26, s), y + y_sec_text), blk[1],
                            font_sec, fill=ta, stroke=stroke_w,
                            stroke_fill=stroke_fill)
                _draw_gradient_line(draw, x + _scale(26, s), x + col_w - _scale(40, s),
                                    y + y_sec_line, COLOR_PRIMARY, (76, 56, 148),
                                    width=_scale(2, s), img=img)
            else:
                cmd_text = _truncate_text(blk[1], col_w - _scale(50, s), font_cmd, draw)
                _frost_text(draw, (x + _scale(26, s), y + y_cmd), cmd_text,
                            font_cmd, fill=tp, stroke=stroke_w,
                            stroke_fill=stroke_fill)
                desc_text = _truncate_text(blk[2], col_w - _scale(50, s), font_desc, draw)
                _frost_text(draw, (x + _scale(26, s), y + y_desc), desc_text,
                            font_desc, fill=ts, stroke=stroke_w,
                            stroke_fill=stroke_fill)

    # 底部提示
    foot_y = H - footer_h + _scale(18, s)
    font_hint = _get_font(_scale(38, s))
    draw.text((pad_x, foot_y + _scale(30, s)), "发送 /点歌帮助 随时查看本帮助",
              font=font_hint, fill=COLOR_TEXT_SECONDARY)
    ver = f"v{PLUGIN_VERSION}"
    vw = _text_width(draw, ver, font_hint)
    draw.text((W - pad_x - vw, foot_y + _scale(30, s)), ver,
              font=font_hint, fill=COLOR_TEXT_DIM)

    output = BytesIO()
    img.save(output, format='JPEG', quality=SEARCH_JPEG_QUALITY,
             subsampling=0, optimize=False)
    output.seek(0)
    return output


# ==================== 留言图片 ====================
def draw_comments_image(song_title: str, comments: List[Dict]) -> BytesIO:
    """绘制歌曲留言图片"""
    padding = PADDING
    content_w = TOTAL_WIDTH - padding * 2
    card_pad = 14
    header_h = 104
    footer_h = 92

    font_title = _get_font(26, bold=True)
    font_sub = _get_font(15)
    font_user = _get_font(15, bold=True)
    font_content = _get_font(16)
    font_time = _get_font(11)

    tmp_img = Image.new('RGB', (10, 10))
    tmp_draw = ImageDraw.Draw(tmp_img)
    text_max = content_w - card_pad * 2 - 16

    card_heights = []
    for c in comments:
        content = c.get('content', '') or ''
        lines = _wrap_text(content, font_content, text_max, tmp_draw)
        n = max(1, len(lines))
        card_heights.append(card_pad * 2 + 22 + 6 + n * 22)

    gap = 10
    total_height = (
        header_h + footer_h
        + sum(card_heights)
        + gap * max(0, len(card_heights) - 1)
    )

    img = _make_canvas(TOTAL_WIDTH, total_height)
    draw = ImageDraw.Draw(img)

    # 头部
    _draw_top_accent(draw, TOTAL_WIDTH)
    _draw_vinyl(draw, padding + 14, 36, 12)
    draw.text((padding + 34, 20), "歌曲留言", font=font_title, fill=COLOR_TEXT_PRIMARY)

    sub = _truncate_text(
        f"「{song_title}」",
        TOTAL_WIDTH - padding * 2 - 110,
        font_sub,
        draw
    )
    draw.text((padding + 34, 64), sub, font=font_sub, fill=COLOR_PRIMARY_LIGHT)

    # 留言数胶囊
    _draw_pill(
        draw, TOTAL_WIDTH - padding - 1, 22,
        f"{len(comments)} 条", font_sub,
        COLOR_PRIMARY, (255, 255, 255)
    )

    _draw_gradient_line(
        draw, padding, TOTAL_WIDTH - padding, 98,
        COLOR_PRIMARY, (76, 56, 148)
    )

    # 留言卡片
    y = 104
    card_w = TOTAL_WIDTH - padding * 2
    for i, c in enumerate(comments):
        h = card_heights[i]
        bg = COLOR_CARD_BG_ALT if i % 2 == 1 else COLOR_CARD_BG
        draw.rounded_rectangle(
            [padding, y, padding + card_w, y + h],
            radius=12,
            fill=bg,
            outline=COLOR_CARD_BORDER,
            width=1
        )
        # 左侧渐变强调条
        for j in range(3):
            t = j / 2
            draw.rounded_rectangle(
                [padding + 4 + j, y + 10, padding + 5 + j, y + h - 10],
                radius=1,
                fill=_lerp(COLOR_PRIMARY, COLOR_ACCENT, t)
            )

        # 用户（左上）+ 时间（右上）
        user = c.get('user', '匿名')
        draw.text((padding + 20, y + 12), user, font=font_user, fill=COLOR_PRIMARY_LIGHT)
        t = c.get('time', '')
        if t:
            tw = _text_width(draw, t, font_time)
            draw.text(
                (padding + card_w - 20 - tw, y + 15),
                t,
                font=font_time,
                fill=COLOR_TEXT_DIM
            )

        # 内容（自动换行）
        content = c.get('content', '') or ''
        lines = _wrap_text(content, font_content, card_w - card_pad * 2 - 20, draw)
        cy = y + 12 + 26
        for line in lines:
            draw.text((padding + 20, cy), line, font=font_content, fill=COLOR_TEXT_PRIMARY)
            cy += 22

        y += h + gap

    footer_y = y - gap + 4
    _draw_footer(draw, "发送 /留言 歌单名 序号 内容 添加留言", footer_y)

    output = BytesIO()
    img.save(output, format='PNG')
    output.seek(0)
    return output


# ==================== 歌单绑定成员图片 ====================
def draw_binding_image(playlist_name: str, members: List[Dict]) -> BytesIO:
    """
    绘制歌单绑定成员图片。
    members: [{"name": 昵称, "role": "owner"|"member"}]，歌单主排第一
    """
    padding = PADDING
    content_w = TOTAL_WIDTH - padding * 2
    header_h = 104
    footer_h = 100
    row_h = 64
    gap = 10

    font_name = _get_font(17, bold=True)
    font_tag = _get_font(13)
    font_icon = _get_font(14, bold=True)

    n = max(1, len(members))
    total_height = header_h + footer_h + n * row_h + gap * max(0, n - 1)

    img = _make_canvas(TOTAL_WIDTH, total_height)
    draw = ImageDraw.Draw(img)

    _draw_header(draw, "歌单绑定", f"「{playlist_name}」", len(members), "人")

    y = header_h
    card_w = TOTAL_WIDTH - padding * 2
    for i, m in enumerate(members):
        is_owner = m.get('role') == 'owner'
        bg = COLOR_CARD_BG_ALT if i % 2 == 1 else COLOR_CARD_BG
        draw.rounded_rectangle(
            [padding, y, padding + card_w, y + row_h],
            radius=12, fill=bg, outline=COLOR_CARD_BORDER, width=1
        )
        # 左侧强调条：歌单主红金渐变，成员灰渐变
        for j in range(3):
            t = j / 2
            draw.rounded_rectangle(
                [padding + 4 + j, y + 10, padding + 5 + j, y + row_h - 10],
                radius=1,
                fill=(_lerp(COLOR_PRIMARY, COLOR_ACCENT, t) if is_owner
                      else _lerp((86, 92, 116), (128, 134, 158), t))
            )

        # 角色圆标
        cx = padding + 28
        cy = y + row_h // 2
        r = 14
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            fill=COLOR_PRIMARY if is_owner else COLOR_INDEX_BG
        )
        icon = "主" if is_owner else "员"
        iw = _text_width(draw, icon, font_icon)
        draw.text((cx - iw // 2, cy - 9), icon, font=font_icon, fill=(255, 255, 255))

        # 角色标签（右侧胶囊）
        tag = "歌单主" if is_owner else "（邀请用户）"
        tag_w = _text_width(draw, tag, font_tag)
        _draw_pill(
            draw,
            padding + card_w - 16 - (tag_w + 24),
            cy - 13,
            tag, font_tag,
            (74, 44, 58) if is_owner else (48, 52, 70),
            COLOR_PRIMARY_LIGHT if is_owner else COLOR_TEXT_DIM
        )

        # 昵称（剩余空间内截断）
        name_x = cx + r + 14
        name_max = padding + card_w - 16 - (tag_w + 24) - name_x - 10
        name = _truncate_text(m.get('name', '未知用户'), name_max, font_name, draw)
        draw.text((name_x, cy - 12), name, font=font_name, fill=COLOR_TEXT_PRIMARY)

        y += row_h + gap

    _draw_footer(
        draw,
        "发送 /绑定 歌单名 绑定 · /绑定查看 歌单名 查看成员",
        y - gap + 4
    )

    output = BytesIO()
    img.save(output, format='PNG')
    output.seek(0)
    return output
