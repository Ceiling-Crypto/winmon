# -*- coding: utf-8 -*-
"""
B12 —— 媒体识别「运行时发现优先」的回归钉（`test_b12`）。

这一批解决什么
--------------
用户的原话：「脱离个人环境，我们只对目前已经安装的媒体测试，范围不够大，
应该是基于所有可识别的媒体。」

改之前是**双名单制**：下拉框里只列 `MEDIA_APP_CANDIDATES` 里那 12 个进程名
＋ 开始菜单里被关键词分到「影音播放 / 浏览器」两类的条目。后果：

  · 用户装了个表里没有的播放器 → 选不到、也控不了；
  · 换一台机器 → 整张清单跟着变（**范围被个人环境框死**）；
  · UWP 应用在会话里报的是 `包名_哈希!App`，跟进程名对不上
    → 连"它到底在不在跑"都会判错。

改之后：**系统媒体会话（GSMTC）是第一真源**。谁能发布会话，谁就自动出现在清单里
并可被精确控制，与候选表、与"本机装了什么"都无关。候选表降级为
**显示名映射 + 唤醒补充**。

这个文件钉住的就是这批新判据
----------------------------
  · `session_app_ids()`：会话行 → 去重后的 app id（正在播放的排前面、坏行不炸）；
  · `media_app_from_session()`：**任意** app id → 一条可选媒体应用
    （认得出候选表的换显示名；认不出的**不假装**成播放器，标成「媒体会话」）；
  · `media_app_running()`：家族名 / UWP 的 PFN 也要能判出"它在跑"；
  · `build_media_app_entries(sessions=…)`：会话条目排在前面、与候选表/开始菜单
    **不重复**、已知应用仍用候选表的显示名、exe 能被 `_app_key_for_media()` 用上；
  · `_sync_media_session_apps()`：会话集合**变了才**重建清单（不反复折腾）；
  · `_media_app_is_running()`：进程名解析不出来时，"它此刻有会话"也算在跑。

为什么这么测（本项目的取舍）
----------------------------
1. **一个"本机装没装"的应用都不依赖**：全部用**替身会话行**驱动，任意 app id
   （含 UWP 的 PFN、中文名、陌生播放器）都能测 —— 这就是"脱离个人环境"。
2. 真实 GSMTC 通路的用例不在这里：`test_b4` 里那条已经改成"**只观察、不造环境**"，
   "自己拉一个浏览器放音频"那种造环境的做法移到了手动探针
   `tests/media_session_probe.py --play-audio`。
3. 每条断言都指向一个能复现的失效模式（漏掉陌生播放器 / 标签重复 /
   把陌生应用假称成播放器 / 家族名被判成"没在跑"），不做填充式断言。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winmon_core as core                                    # noqa: E402
import winmon                                                 # noqa: E402


# ---------------------------------------------------------------------------
# 一、会话行 → app id（纯逻辑）
# ---------------------------------------------------------------------------

class TestSessionAppIds(unittest.TestCase):
    """`session_app_ids()` —— 吃会话行，不碰系统（不为了算清单再起一次 PowerShell）。"""

    def test_playing_comes_first_and_duplicates_collapse(self):
        """
        「正在播放」必须排在「已暂停」前面。

        这条抓的是一个**老 bug**：原来排序键是 `-status`，而状态是
        `4 = 正在播放 / 5 = 已暂停` —— 倒序把已暂停排到了正在播放前面，
        与注释里那句"正在播放的排前面"正好相反。改完这条顺序会真的被用户看到
        （下拉框里会话条目就按它排），所以钉住。
        """
        rows = [{"app": "A", "status": 5}, {"app": "B", "status": 4},
                {"app": "A", "status": 4}, {"app": "C", "status": 0}]
        self.assertEqual(core.session_app_ids(rows), ["B", "A", "C"])

    def test_paused_never_jumps_ahead_of_playing(self):
        rows = [{"app": "paused", "status": 5}, {"app": "playing", "status": 4}]
        self.assertEqual(core.session_app_ids(rows), ["playing", "paused"])

    def test_bad_rows_are_skipped_not_fatal(self):
        """
        坏行一律跳过：清单是"增强"，绝不能因为一行数据不干净就把整个界面刷新打断。

        `status` 不是数字也要认（只当 0），因为那是**外部数据**
        （PowerShell 的输出），不是本程序保证的。
        """
        rows = [None, 42, "x", {}, {"app": ""}, {"app": "   "},
                {"app": "ok"}, {"app": "ok", "status": "playing"}]
        self.assertEqual(core.session_app_ids(rows), ["ok"])

    def test_no_sessions_is_an_empty_list(self):
        self.assertEqual(core.session_app_ids([]), [])
        self.assertEqual(core.session_app_ids(None), [])

    def test_media_session_app_ids_uses_the_same_rule(self):
        """`media_session_app_ids()` 只是"自己读一次"的薄壳，规则必须是同一套。"""
        got = {"ok": True, "sessions": [{"app": "B", "status": 4},
                                       {"app": "A", "status": 0}]}
        with mock.patch.object(core, "collect_media_sessions", return_value=got):
            self.assertEqual(core.media_session_app_ids(), ["B", "A"])


# ---------------------------------------------------------------------------
# 二、任意 app id → 一条可选媒体应用
# ---------------------------------------------------------------------------

class TestMediaAppFromSession(unittest.TestCase):
    """`media_app_from_session()` —— 这是"基于所有可识别的媒体"的落地。"""

    def test_known_family_name_becomes_the_display_name(self):
        app = core.media_app_from_session("MSEdge", {"msedge.exe"})
        self.assertIsNotNone(app)
        self.assertEqual(app.label, "Microsoft Edge")
        self.assertEqual(app.exe, "msedge.exe")
        self.assertTrue(app.browser)
        self.assertEqual(app.kind, "", "认得出候选表的，不该标成「媒体会话」")
        self.assertEqual(app.title, "Microsoft Edge（浏览器）")

    def test_uwp_package_id_is_kept_as_is_and_not_called_a_player(self):
        """UWP 报的是 `包名_哈希!App` —— 认不出来也**不许编造**名字和类别。"""
        app = core.media_app_from_session("SomePlayer_1a2b3c!App", set())
        self.assertIsNotNone(app)
        self.assertEqual(app.label, "SomePlayer_1a2b3c!App")
        self.assertEqual(app.kind, core.MEDIA_KIND_SESSION)
        self.assertIn("媒体会话", app.title)
        self.assertNotIn("播放器", app.title, "认不出类别就别假称成播放器")
        self.assertFalse(app.browser)

    def test_unknown_app_gets_its_real_process_name(self):
        """能对上正在跑的进程就换成真进程名 —— 这样 `media_app_path()` 才唤得醒它。"""
        app = core.media_app_from_session("SomeCoolPlayer_1a2b3c!App",
                                          {"somecoolplayer.exe", "explorer.exe"})
        self.assertEqual(app.exe, "somecoolplayer.exe")

    def test_unknown_app_without_a_matching_process_keeps_the_session_id(self):
        """解析不出进程名就原样用会话 id —— 命令照样能按它精确下发（松匹配）。"""
        app = core.media_app_from_session("Weird_Player_9z!App", {"explorer.exe"})
        self.assertEqual(app.exe, "Weird_Player_9z!App")

    def test_a_known_browser_reported_by_family_name_is_still_a_browser(self):
        app = core.media_app_from_session("Chrome", {"chrome.exe"})
        self.assertEqual(app.label, "Google Chrome")
        self.assertTrue(app.browser)

    def test_chinese_id_is_not_mangled(self):
        app = core.media_app_from_session("酷我音乐", set())
        self.assertEqual(app.label, "酷我音乐")
        self.assertNotIn("?", app.label)

    def test_short_family_name_does_not_match_a_longer_player(self):
        """
        `QQ` 不许被 `QQMusic` 包含命中 —— 那是"选了 A、控的却是 B"的老坑
        （`media_app_stems_match` 的 ≥5 字符规则就是为它加的）。
        """
        app = core.media_app_from_session("QQ", {"qqmusic.exe"})
        self.assertEqual(app.exe, "QQ")

    def test_empty_id_is_not_an_app(self):
        for bad in ("", "   ", None):
            with self.subTest(app_id=bad):
                self.assertIsNone(core.media_app_from_session(bad))


# ---------------------------------------------------------------------------
# 三、"它在不在跑"要认家族名与 UWP
# ---------------------------------------------------------------------------

class TestMediaAppRunning(unittest.TestCase):
    """`media_app_running()` —— 直接比字符串会把正在放歌的应用判成"没在跑"。"""

    def test_family_name_matches_the_process(self):
        self.assertTrue(core.media_app_running("MSEdge", {"msedge.exe"}))

    def test_uwp_family_id_matches_its_process(self):
        self.assertTrue(core.media_app_running("SomePlayer_1a2b3c!App",
                                              {"someplayer.exe"}))

    def test_exact_name_still_works(self):
        self.assertTrue(core.media_app_running("cloudmusic.exe", {"cloudmusic.exe"}))

    def test_short_names_do_not_cross_match(self):
        self.assertFalse(core.media_app_running("QQ", {"qqmusic.exe"}))

    def test_unrelated_process_is_not_a_match(self):
        self.assertFalse(core.media_app_running("vlc.exe", {"explorer.exe"}))

    def test_empty_inputs_are_false_not_an_error(self):
        self.assertFalse(core.media_app_running("", {"explorer.exe"}))
        self.assertFalse(core.media_app_running("vlc.exe", None))


# ---------------------------------------------------------------------------
# 四、三个来源合流：会话 > 候选表 > 开始菜单
# ---------------------------------------------------------------------------

class TestBuildMediaAppEntriesWithSessions(unittest.TestCase):
    """`build_media_app_entries(sessions=…)` —— 会话条目怎么和另外两个来源合流。"""

    def _entries(self, sessions=(), running=(), available=None, found=()):
        return winmon.build_media_app_entries(
            running=set(running), available=available or (lambda exe: False),
            found=list(found), sessions=list(sessions))

    def test_a_session_app_that_is_not_installed_is_still_listed(self):
        """用户装了个候选表里没有的播放器 —— 这条就是"范围不够大"的解药。"""
        labels, mapping = self._entries(sessions=["Weird_Player_9z!App"])
        hit = [l for l in labels if "Weird_Player_9z!App" in l]
        self.assertEqual(len(hit), 1, f"会话里的应用没进清单：{labels}")
        self.assertIsNotNone(mapping[hit[0]])
        self.assertEqual(mapping[hit[0]].exe, "Weird_Player_9z!App")

    def test_session_entries_come_right_after_the_auto_item(self):
        labels, _m = self._entries(
            sessions=["Weird_Player_9z!App"],
            running={"cloudmusic.exe"},
            available=lambda exe: exe == "cloudmusic.exe")
        self.assertEqual(labels[0], winmon.MEDIA_APP_ANY)
        self.assertTrue(labels[1].startswith("Weird_Player_9z!App"),
                        f"此刻真在放东西的最该被看到：{labels}")

    def test_a_known_session_app_keeps_the_candidate_label_and_exe(self):
        """认得出候选表的：标签与以前**完全一致**，只是来路变成了会话。"""
        labels, mapping = self._entries(sessions=["MSEdge"], running={"msedge.exe"})
        edge = [l for l in labels if "Edge" in l]
        self.assertEqual(len(edge), 1, f"会话与候选表撞车了：{labels}")
        self.assertEqual(edge[0], "Microsoft Edge（浏览器）（运行中）")
        self.assertEqual(mapping[edge[0]].exe, "msedge.exe")

    def test_a_session_app_is_not_repeated_as_a_menu_entry(self):
        labels, _m = self._entries(
            sessions=["MSEdge"], running={"msedge.exe"},
            found=[core.MediaApp("Microsoft Edge", "", True, r"C:\菜单\Edge.lnk")])
        self.assertEqual(len([l for l in labels if "Edge" in l]), 1, labels)

    def test_no_two_entries_share_a_stem_including_sessions(self):
        """通用判据：**任何**两条的主干都不能相同（以后再加来源也不会冒重复项）。"""
        labels, _m = self._entries(
            sessions=["MSEdge", "Weird_Player_9z!App", "MSEdge"],
            running={"msedge.exe", "cloudmusic.exe"},
            available=lambda exe: exe in ("msedge.exe", "cloudmusic.exe"),
            found=[core.MediaApp("Microsoft Edge", "", True, "x.lnk"),
                   core.MediaApp("网易云音乐", "", False, "y.lnk")])
        stems = [core.media_app_stem(l) for l in labels
                 if l != winmon.MEDIA_APP_ANY]
        self.assertEqual(len(stems), len(set(stems)), f"有重复条目：{labels}")

    def test_default_sessions_keeps_the_old_behaviour(self):
        """不给 `sessions` 时行为与以前一模一样（老用例与老调用点都不受影响）。"""
        labels, _m = winmon.build_media_app_entries(
            running=set(), available=lambda exe: False, found=[])
        self.assertEqual(labels, [winmon.MEDIA_APP_ANY])

    def test_discovered_entry_is_usable_as_the_command_key(self):
        """
        会话发现的那条必须带得上 exe：`exe` 是空串时命令会发给"第一个会话"，
        用户看到的是"我选了 A、控的却是 B"（项目里最忌讳的假成功）。
        """
        labels, mapping = self._entries(sessions=["Weird_Player_9z!App"])
        hit = [l for l in labels if "Weird_Player_9z!App" in l][0]
        self.assertTrue(mapping[hit].exe)

    def test_the_session_word_is_a_strippable_suffix(self):
        """「（媒体会话）」必须能被 `media_app_stem` 剥掉，否则去重与选择都会失灵。"""
        self.assertIn("（媒体会话）", core.MEDIA_NAME_SUFFIXES)
        self.assertEqual(core.media_app_stem("某播放器（媒体会话）（运行中）"),
                         "某播放器")


# ---------------------------------------------------------------------------
# 五、选择要活过"这条是从哪来的"
# ---------------------------------------------------------------------------

class TestPinSurvivesTheSourceChanging(unittest.TestCase):
    """同一个程序这次从会话发现、下次从开始菜单发现 —— 用户的选择不许被丢掉。"""

    def test_pin_resolves_across_sources(self):
        labels = [winmon.MEDIA_APP_ANY, "某播放器（播放器）（菜单）"]
        resolved, dropped = core.resolve_media_pin("某播放器（媒体会话）", labels,
                                                   winmon.MEDIA_APP_ANY)
        self.assertEqual(resolved, "某播放器（播放器）（菜单）")
        self.assertEqual(dropped, [])

    def test_pin_survives_the_other_direction(self):
        labels = [winmon.MEDIA_APP_ANY, "某播放器（媒体会话）"]
        resolved, dropped = core.resolve_media_pin("某播放器（播放器）（菜单）",
                                                   labels, winmon.MEDIA_APP_ANY)
        self.assertEqual(resolved, "某播放器（媒体会话）")
        self.assertEqual(dropped, [])


# ---------------------------------------------------------------------------
# 六、清单什么时候重建（会话集合变了才重建）
# ---------------------------------------------------------------------------

class TestSessionSyncRebuildsTheList(unittest.TestCase):
    """`_sync_media_session_apps()` —— 不建窗口、不起进程，纯接线检查。"""

    def _fake_app(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._media_session_apps = []
        app.calls = []

        def reload_list():
            app.calls.append(("reload", list(app._media_session_apps)))

        app._reload_media_app_list = reload_list
        app._set_status = lambda text: app.calls.append(("status", text))
        return app

    def test_first_sync_builds_the_list(self):
        app = self._fake_app()
        app._sync_media_session_apps([{"app": "MSEdge", "status": 4}])
        self.assertEqual(app._media_session_apps, ["MSEdge"])
        self.assertEqual(app.calls, [("reload", ["MSEdge"])])

    def test_same_sessions_do_not_rebuild(self):
        """每 4 秒都会拿到一份同样的会话 —— 不许每次都重建清单（那是白折腾）。"""
        app = self._fake_app()
        rows = [{"app": "MSEdge", "status": 4}]
        app._sync_media_session_apps(rows)
        app._sync_media_session_apps(rows)
        self.assertEqual(len(app.calls), 1, app.calls)

    def test_a_new_session_rebuilds_the_list(self):
        """新打开一个播放器 → 它自动进清单（这就是"所有可识别的媒体"）。"""
        app = self._fake_app()
        app._sync_media_session_apps([{"app": "MSEdge", "status": 4}])
        app._sync_media_session_apps([{"app": "MSEdge", "status": 4},
                                     {"app": "Weird_9z!App", "status": 4}])
        self.assertEqual(len(app.calls), 2, app.calls)
        self.assertEqual(app._media_session_apps, ["MSEdge", "Weird_9z!App"])

    def test_a_reload_failure_is_reported_not_swallowed(self):
        """清单重建失败要说出来 —— 静默失败会让用户以为"就是没有播放器"。"""
        app = self._fake_app()

        def boom():
            raise RuntimeError("清单炸了")

        app._reload_media_app_list = boom
        app._sync_media_session_apps([{"app": "MSEdge", "status": 4}])
        self.assertTrue(any(c[0] == "status" and "清单炸了" in c[1]
                            for c in app.calls), app.calls)


# ---------------------------------------------------------------------------
# 七、端到端一小步：钉住的那个应用，真的被挑中 / 真的算"在跑"
# ---------------------------------------------------------------------------

class TestTheDiscoveredAppIsTheOneThatGetsPicked(unittest.TestCase):
    def test_summary_picks_the_discovered_app_by_its_session_id(self):
        rows = [{"app": "MSEdge", "title": "网页歌", "artist": "", "status": 4},
                {"app": "Weird_Player_9z!App", "title": "陌生播放器的歌",
                 "artist": "", "status": 4}]
        info = core.media_session_summary(rows, "Weird_Player_9z!App")
        self.assertTrue(info["found"])
        self.assertEqual(info["picked"]["app"], "Weird_Player_9z!App")
        self.assertIn("陌生播放器的歌", info["text"])

    def test_a_live_session_counts_as_running(self):
        """
        进程名解析不出来（UWP 的 PFN）时，"它此刻有会话"本身就说明它在跑。
        没有这条，`media_command()` 会以为要先唤醒它，或者干脆说"它没在运行"。
        """
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._media_session_apps = ["Weird_Player_9z!App"]
        media = core.MediaApp("Weird_Player_9z!App", "Weird_Player_9z!App",
                              False, "", core.MEDIA_KIND_SESSION)
        with mock.patch.object(core, "snapshot_processes",
                               return_value={1: {"name": "explorer.exe"}}):
            self.assertTrue(app._media_app_is_running(media))

    def test_no_session_and_no_process_means_not_running(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._media_session_apps = []
        media = core.MediaApp("VLC", "vlc.exe")
        with mock.patch.object(core, "snapshot_processes",
                               return_value={1: {"name": "explorer.exe"}}):
            self.assertFalse(app._media_app_is_running(media))

    def test_a_running_process_is_enough_without_any_session(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._media_session_apps = []
        media = core.MediaApp("VLC", "vlc.exe")
        with mock.patch.object(core, "snapshot_processes",
                               return_value={1: {"name": "vlc.exe"}}):
            self.assertTrue(app._media_app_is_running(media))


if __name__ == "__main__":
    unittest.main(verbosity=2)
