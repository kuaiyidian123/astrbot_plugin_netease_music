"""纯逻辑函数最小单测（不依赖 AstrBot 运行时）

覆盖 _validate_playlist_name / _sanitize_filename / _is_admin /
_match_playlist_and_song / _strip_command 等纯逻辑，其中 _strip_command 与
_match_playlist_and_song 的用例用于回归「歌单名含『歌单』二字时 /歌单 失效」问题。

运行方式（需要 AstrBot 框架在 import 路径上，用于 import main 模块）：
    set ASTRBOT_CORE=D:\\123\\instances\\<实例>\\core
    python -m unittest discover -s astrbot_plugin_netease_music/tests -v
"""

import os
import sys
import tempfile
import unittest

# 让 `astrbot_plugin_netease_music.main` 可被导入（需要插件目录的父目录在 sys.path 上）
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_PLUGIN_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# AstrBot 框架源码目录（main.py 依赖 astrbot.api），通过环境变量指定
_CORE = os.environ.get("ASTRBOT_CORE")
if _CORE and _CORE not in sys.path:
    sys.path.insert(0, _CORE)

try:
    from astrbot_plugin_netease_music.main import (
        NeteaseMusicPlugin,
        _BILI_MIXIN_KEY_ENC_TAB,
        _bili_mixin_key,
        _bili_wbi_sign,
        _bili_clean_title,
        _format_duration,
        _format_play_count,
    )
    _IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - 环境缺失时跳过
    NeteaseMusicPlugin = None
    _IMPORT_ERROR = e


@unittest.skipIf(NeteaseMusicPlugin is None, f"无法导入插件模块: {_IMPORT_ERROR}")
class PureLogicTest(unittest.TestCase):

    # ---------- _strip_command：问题 1 的核心修复 ----------

    def test_strip_command_keeps_playlist_word(self):
        """歌单名里的「歌单」二字不应被吃掉"""
        strip = NeteaseMusicPlugin._strip_command
        self.assertEqual(strip("/歌单 我的歌单", "歌单"), "我的歌单")
        self.assertEqual(strip("歌单 我的歌单", "歌单"), "我的歌单")
        self.assertEqual(strip("/歌单/ 我的歌单", "歌单"), "我的歌单")
        self.assertEqual(strip("/歌单 歌单", "歌单"), "歌单")

    def test_strip_command_empty(self):
        strip = NeteaseMusicPlugin._strip_command
        self.assertEqual(strip("/歌单", "歌单"), "")
        self.assertEqual(strip("歌单", "歌单"), "")
        self.assertEqual(strip("", "歌单"), "")

    # ---------- _validate_playlist_name ----------

    def test_validate_playlist_name_ok(self):
        validate = NeteaseMusicPlugin._validate_playlist_name
        self.assertIsNone(validate("我的歌单"))
        self.assertIsNone(validate("abc_123"))

    def test_validate_playlist_name_bad(self):
        validate = NeteaseMusicPlugin._validate_playlist_name
        self.assertIsNotNone(validate(""))
        self.assertIsNotNone(validate("   "))
        self.assertIsNotNone(validate("a" * 16))     # 超长
        self.assertIsNotNone(validate("a/b"))        # 非法字符
        self.assertIsNotNone(validate("a..b"))       # 路径穿越
        self.assertIsNotNone(validate("name."))      # 点号结尾
        self.assertIsNotNone(validate("CON"))        # Windows 保留名

    # ---------- _sanitize_filename ----------

    def test_sanitize_filename(self):
        san = NeteaseMusicPlugin._sanitize_filename
        self.assertEqual(san('a/b:c*d?"e<f>g|h'), "a_b_c_d__e_f_g_h")  # ?" 相邻各替换一个下划线
        self.assertEqual(san("abc\x00\x1f"), "abc")   # 控制字符被剔除
        self.assertEqual(san("name. "), "name")       # 结尾点/空格
        self.assertEqual(san("."), "_")
        self.assertEqual(san(".."), "_")
        self.assertEqual(san(""), "_")
        self.assertEqual(san("CON.txt"), "_CON.txt")  # 保留名加前缀

    # ---------- _is_admin：未绑定收紧后不再默认放开 ----------

    def test_is_admin_unbound_denied(self):
        self.assertFalse(NeteaseMusicPlugin._is_admin({}, "10001"))
        self.assertFalse(NeteaseMusicPlugin._is_admin({"owner": {}}, "10001"))

    def test_is_admin_owner_and_members(self):
        binding = {
            "owner": {"user_id": "10001", "name": "群主"},
            "members": [{"user_id": "10002"}],
        }
        self.assertTrue(NeteaseMusicPlugin._is_admin(binding, "10001"))
        self.assertTrue(NeteaseMusicPlugin._is_admin(binding, "10002"))
        self.assertFalse(NeteaseMusicPlugin._is_admin(binding, "10003"))

    # ---------- _match_playlist_and_song ----------

    def _make_plugin(self, music_root):
        plugin = object.__new__(NeteaseMusicPlugin)
        plugin.config = {"music_root": music_root}
        return plugin

    def test_match_playlist_with_gedan_in_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            for folder in ("我的歌单", "歌单", "测试"):
                os.makedirs(os.path.join(tmp, folder))
            plugin = self._make_plugin(tmp)

            self.assertEqual(plugin._match_playlist_and_song("我的歌单"), ("我的歌单", None))
            self.assertEqual(plugin._match_playlist_and_song("歌单"), ("歌单", None))
            self.assertEqual(
                plugin._match_playlist_and_song("我的歌单 晴天"), ("我的歌单", "晴天")
            )
            # 更长歌单名优先匹配
            self.assertEqual(
                plugin._match_playlist_and_song("我的歌单 歌单"), ("我的歌单", "歌单")
            )
            self.assertEqual(plugin._match_playlist_and_song("不存在"), (None, None))


@unittest.skipIf(NeteaseMusicPlugin is None, f"无法导入插件模块: {_IMPORT_ERROR}")
class BilibiliPureLogicTest(unittest.TestCase):
    """B 站相关纯逻辑（不联网）"""

    def test_mixin_key_table_is_valid(self):
        """重排表必须是 0..63 的一个完整排列"""
        self.assertEqual(len(_BILI_MIXIN_KEY_ENC_TAB), 64)
        self.assertEqual(sorted(_BILI_MIXIN_KEY_ENC_TAB), list(range(64)))

    def test_mixin_key_length(self):
        key = _bili_mixin_key("a" * 32, "b" * 32)
        self.assertEqual(len(key), 32)

    def test_wbi_sign_adds_fields(self):
        signed = _bili_wbi_sign({"keyword": "abc", "page": 1}, "x" * 32)
        self.assertIn("wts", signed)
        self.assertIn("w_rid", signed)
        self.assertEqual(len(signed["w_rid"]), 32)
        # 不应改动入参
        params = {"keyword": "abc"}
        _bili_wbi_sign(params, "x" * 32)
        self.assertNotIn("w_rid", params)

    def test_clean_title(self):
        self.assertEqual(_bili_clean_title('<em class="keyword">That</em> Girl'), "That Girl")
        self.assertEqual(_bili_clean_title("A &amp; B"), "A & B")

    def test_parse_duration(self):
        parse = NeteaseMusicPlugin._bili_parse_duration
        self.assertEqual(parse(93), 93)
        self.assertEqual(parse("93"), 93)
        self.assertEqual(parse("01:33"), 93)
        self.assertEqual(parse("1:01:33"), 3693)
        self.assertEqual(parse(""), 0)
        self.assertEqual(parse("bad"), 0)

    def test_format_duration_and_play(self):
        self.assertEqual(_format_duration(93), "01:33")
        self.assertEqual(_format_duration(3693), "1:01:33")
        self.assertEqual(_format_play_count(1234), "1234")
        self.assertEqual(_format_play_count(23456), "2.3万")
        self.assertEqual(_format_play_count(234567890), "2.3亿")

    def test_pick_streams_respects_size_limit(self):
        """体积超限时自动降清晰度，且音轨避开杜比/Hi-Res"""
        pick = NeteaseMusicPlugin._bili_pick_streams
        vids = [
            {"id": 16, "baseUrl": "v16", "bandwidth": 400 * 1024},
            {"id": 32, "baseUrl": "v32", "bandwidth": 900 * 1024},
            {"id": 80, "baseUrl": "v80", "bandwidth": 3000 * 1024},
        ]
        auds = [{"id": 30280, "baseUrl": "a192", "bandwidth": 200 * 1024}]
        # 上限 5MB：只有最低画质(约 4.6MB)能放下
        self.assertEqual(pick(vids, auds, 60, 5 * 1024 * 1024)[0], "v16")
        # 上限 30MB：可选最高画质
        self.assertEqual(pick(vids, auds, 60, 30 * 1024 * 1024)[0], "v80")
        # 杜比/Hi-Res 音轨被排除，只选常规音轨
        auds2 = [
            {"id": 30250, "baseUrl": "dolby", "bandwidth": 500 * 1024},
            {"id": 30216, "baseUrl": "a64", "bandwidth": 64 * 1024},
        ]
        self.assertEqual(pick(vids, auds2, 60, 100 * 1024 * 1024)[1], "a64")
        # 没有可用视频流时返回 None
        self.assertIsNone(pick([], auds, 60, 1024))

    def test_pick_streams_prefers_h264_codec(self):
        """同一清晰度有 H.264 / H.265 时优先 H.264（QQ 播放兼容性更好）"""
        pick = NeteaseMusicPlugin._bili_pick_streams
        vids = [
            {"id": 32, "baseUrl": "v32-hev", "bandwidth": 300 * 1024,
             "codecs": "hev1.1.6.L120.90"},
            {"id": 32, "baseUrl": "v32-avc", "bandwidth": 320 * 1024,
             "codecs": "avc1.64001F"},
        ]
        self.assertEqual(pick(vids, [], 10, 1024 * 1024)[0], "v32-avc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
