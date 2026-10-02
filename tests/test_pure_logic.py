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
    from astrbot_plugin_netease_music.main import NeteaseMusicPlugin
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
