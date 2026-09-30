"""
图片绘制工具模块
负责生成搜索结果列表图片 / 本地歌单图片 / 帮助图片 / 留言图片
v2 视觉升级：渐变背景 + 光晕 + 卡片深度 + 徽章序号
"""

import os
from io import BytesIO
from typing import List, Dict, Optional

from PIL import Image, ImageDraw, ImageFont


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


# ==================== 字体 ====================
def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """加载字体，bold=True 时优先使用粗体字库"""
    if bold:
        font_paths = [
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simsun.ttc",
        ]
    else:
        font_paths = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
        ]

    for path in font_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

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
PLUGIN_VERSION = "1.2.3"  # 插件版本号（每次更新/修改递增）


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


# ==================== 通用组件 ====================
def _draw_top_accent(draw: ImageDraw.ImageDraw, width: int):
    """顶部渐变红条"""
    strip_h = 6
    for x in range(width):
        t = x / width
        draw.line([(x, 0), (x, strip_h)], fill=_lerp(COLOR_PRIMARY, COLOR_PRIMARY_LIGHT, t))


def _draw_gradient_line(draw, x0: int, x1: int, y: int, c1: tuple, c2: tuple, width: int = 1):
    """水平渐变线"""
    for x in range(x0, x1):
        t = (x - x0) / max(1, (x1 - x0))
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


# ==================== 搜索图片 ====================
def draw_search_result_image(
    keyword: str,
    songs: List[Dict],
    max_display: int = 20,
    covers: Optional[Dict] = None
) -> BytesIO:
    """绘制搜索结果列表图片；covers: {song_id: PIL.Image} 封面映射"""
    display_count = min(len(songs), max_display)

    total_height = (CARD_START_Y
                    + display_count * (CARD_HEIGHT + CARD_GAP)
                    + FOOTER_HEIGHT)

    img = _make_canvas(TOTAL_WIDTH, total_height)
    draw = ImageDraw.Draw(img)

    _draw_header(draw, "搜索结果", f"「{keyword}」", display_count)

    card_x = PADDING
    card_w = TOTAL_WIDTH - PADDING * 2
    for idx in range(display_count):
        song = songs[idx]
        card_y = CARD_START_Y + idx * (CARD_HEIGHT + CARD_GAP)
        song_name, detail = _song_detail_from_dict(song)
        cover = None
        if covers:
            cover = covers.get(song.get('id'))
        _draw_card(
            draw, img, card_x, card_y, card_w, CARD_HEIGHT,
            idx, song_name, detail,
            highlight=(idx < 3),
            cover=cover
        )

    footer_y = CARD_START_Y + display_count * (CARD_HEIGHT + CARD_GAP) + 4
    _draw_footer(draw, "发送 /选歌 <序号> 点播歌曲", footer_y)

    output = BytesIO()
    img.save(output, format='PNG')
    output.seek(0)
    return output


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
def draw_help_image(music_root: str = MUSIC_ROOT) -> BytesIO:
    """绘制使用帮助图片"""
    sections = [
        ("点歌播放", [
            ("/点歌 歌名", "搜索歌曲，返回歌曲图片列表"),
            ("/选歌 序号", "播放搜索结果中的第 N 首"),
            ("/选歌 序号 添加歌单 歌单名", "把搜索到的歌曲下载到本地歌单"),
        ]),
        ("本地歌单", [
            ("/创造歌单 歌单名", f"在 {music_root} 下新建歌单文件夹"),
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

    padding = PADDING
    header_h = 104
    footer_h = 98
    col_gap = 16
    section_gap = 12

    # 三栏布局，整体比例锁定 4:3
    col_count = 3

    font_title = _get_font(26, bold=True)
    font_sub = _get_font(15)
    font_section = _get_font(16, bold=True)
    font_cmd = _get_font(14, bold=True)
    font_desc = _get_font(12)

    section_h = 30
    item_h = 54
    min_item_h = 46

    # 计算每个分区的高度
    sec_blocks = []
    for sec_title, items in sections:
        h = section_h + len(items) * item_h + section_gap
        sec_blocks.append((sec_title, items, h))

    # 顺序填充到各栏，超过目标高度一定比例才换栏，尽量均衡
    total_h = sum(b[2] for b in sec_blocks)
    target = total_h / col_count
    columns = [[] for _ in range(col_count)]
    col_heights = [0] * col_count
    ci = 0
    for block in sec_blocks:
        if (ci < col_count - 1 and col_heights[ci] > 0
                and col_heights[ci] + block[2] > target * 1.15):
            ci += 1
        columns[ci].append(block)
        col_heights[ci] += block[2]

    content_h = max(col_heights)
    total_height = header_h + content_h + footer_h

    # 由高度反推宽度，使整体恰为 4:3
    HELP_WIDTH = int(round(total_height * 4 / 3))
    col_w = (HELP_WIDTH - padding * 2 - col_gap * (col_count - 1)) // col_count
    cmd_max = col_w - 32
    desc_max = col_w - 32

    img = _make_canvas(HELP_WIDTH, total_height)
    draw = ImageDraw.Draw(img)

    # 头部
    _draw_top_accent(draw, HELP_WIDTH)
    _draw_vinyl(draw, padding + 14, 36, 12)
    draw.text((padding + 34, 20), "网易云点歌 · 使用帮助", font=font_title, fill=COLOR_TEXT_PRIMARY)
    draw.text((padding + 34, 58), "所有功能与用法一览", font=font_sub, fill=COLOR_PRIMARY_LIGHT)
    _draw_gradient_line(
        draw, padding, HELP_WIDTH - padding, 92,
        COLOR_PRIMARY, (76, 56, 148)
    )

    item_counter = 0
    for col_idx, col_sections in enumerate(columns):
        x = padding + col_idx * (col_w + col_gap)

        # 自适应：该栏卡片高度按可用空间拉伸，填满整栏，底部与其它栏对齐
        sec_num = len(col_sections)
        item_num = sum(len(items) for _, items, _h in col_sections)
        avail = content_h - sec_num * section_h - sec_num * section_gap
        card_h = max(min_item_h, avail / item_num) if item_num else item_h

        y = float(header_h)
        for sec_title, items, _h in col_sections:
            # 小节标题 + 左侧渐变短横线
            _draw_gradient_line(
                draw, x + 2, x + 12, int(y) + 12,
                COLOR_PRIMARY, COLOR_ACCENT
            )
            draw.text((x + 20, int(y) + 2), sec_title, font=font_section, fill=COLOR_PRIMARY_LIGHT)
            y += section_h

            for cmd, desc in items:
                y0 = int(y)
                h_i = int(y + card_h) - y0
                bg = COLOR_CARD_BG_ALT if item_counter % 2 == 1 else COLOR_CARD_BG
                draw.rounded_rectangle(
                    [x, y0, x + col_w, y0 + h_i],
                    radius=10,
                    fill=bg,
                    outline=COLOR_CARD_BORDER,
                    width=1
                )
                # 左侧渐变强调条
                for i in range(3):
                    t = i / 2
                    draw.rounded_rectangle(
                        [x + 4 + i, y0 + 8, x + 5 + i, y0 + h_i - 8],
                        radius=1,
                        fill=_lerp(COLOR_PRIMARY, COLOR_ACCENT, t)
                    )
                # 文本垂直居中，不随卡片拉伸而错位
                text_total = 20 + 5 + 16
                ty = y0 + (h_i - text_total) // 2
                cmd_text = _truncate_text(cmd, cmd_max, font_cmd, draw)
                draw.text((x + 16, ty), cmd_text, font=font_cmd, fill=COLOR_TEXT_PRIMARY)
                desc_text = _truncate_text(desc, desc_max, font_desc, draw)
                draw.text((x + 16, ty + 25), desc_text, font=font_desc, fill=COLOR_TEXT_SECONDARY)

                y += card_h
                item_counter += 1
            y += section_gap

    footer_y = total_height - footer_h + 8
    _draw_footer(draw, "发送 /点歌帮助 随时查看本帮助", footer_y, width=HELP_WIDTH)

    output = BytesIO()
    img.save(output, format='PNG')
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
