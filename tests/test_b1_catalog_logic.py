# -*- coding: utf-8 -*-
"""
B1/B3 —— 程序目录：分类、收藏、最近使用。

这几块都是纯数据逻辑，可以完全确定性地测试（不需要图形会话、不碰注册表）：
  · `classify_program` / `group_items_by_category` —— 归类
  · `CatalogPrefs` —— 收藏 / 最近使用 / 手动分类
  · `dedupe_catalog` —— 去重
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


def item(name, target="", install="", **kw):
    return core.ProgramItem(name=name, target=target, install_location=install, **kw)


# ===========================================================================
# 分类
# ===========================================================================
class TestClassifyProgram(unittest.TestCase):
    #: (软件名, 目标路径, 安装位置, 期望类别)
    CASES = [
        # 游戏：靠名字或 steam 安装路径识别
        ("Steam", r"X:\steam\steam.exe", r"X:\steam", "game"),
        ("Cities: Skylines", r"X:\steam\steamapps\common\Cities_Skylines\Cities.exe", "", "game"),
        ("MuMu模拟器", r"X:\MuMuPlayer\nx_main\MuMuNxMain.exe", r"X:\MuMuPlayer", "game"),
        ("雷神加速器", r"X:\LeiGod_Acc\leigod_launcher.exe", r"X:\LeiGod_Acc", "game"),
        ("鸣潮", r"X:\Wuthering Waves\launcher.exe", "", "game"),
        # 影音
        ("哔哩哔哩", r"X:\Program Files\bilibili\哔哩哔哩.exe", "", "media"),
        ("网易云音乐", r"X:\CloudMusic\cloudmusic.exe", "", "media"),
        ("PotPlayer 64 bit", r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe", "", "media"),
        # 浏览器
        ("Microsoft Edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "", "browser"),
        ("Google Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe", "", "browser"),
        # 办公
        ("Word", r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE", "", "office"),
        ("WPS Office", r"C:\Program Files (x86)\Kingsoft\WPS Office\ksolaunch.exe", "", "office"),
        # 开发
        ("Visual Studio Code", r"C:\Program Files (x86)\Microsoft VS Code\Code.exe", "", "dev"),
        ("Python 3.14 (64-bit)", r"X:\Python314\python.exe", "", "dev"),
        ("Node.js", r"C:\Program Files\nodejs\node.exe", "", "dev"),
        # 安全
        ("火绒安全软件", r"C:\Program Files\Huorong\Sysdiag\bin\HipsMain.exe", "", "security"),
        # 系统工具（名字是英文，只有路径能看出类别）
        ("Command Prompt", r"C:\WINDOWS\system32\cmd.exe", "", "system"),
        ("Computer Management", r"C:\WINDOWS\system32\compmgmt.msc", "", "system"),
        ("NVIDIA App", r"C:\Program Files\NVIDIA Corporation\NVIDIA App\CEA\NVIDIA App.exe", "", "system"),
        ("7-Zip File Manager", r"C:\Windows\7-Zip\7zFM.exe", "", "system"),
        ("微信", r"C:\Program Files\Tencent\WeChat\WeChat.exe", "", "system"),
        # 都不命中 → 其他
        ("e管家", r"C:\Program Files\StarSoftComm\CooCare6\CooCare.exe", "", "other"),
        ("", r"C:\random\mystery-app.exe", "", "other"),
    ]

    def test_cases(self):
        for name, target, install, want in self.CASES:
            with self.subTest(name=name):
                got = core.classify_program(name, target, install)
                self.assertEqual(got, want,
                                 f"{name} 应为 {core.CATEGORY_LABELS[want]}，"
                                 f"实际 {core.CATEGORY_LABELS.get(got, got)}")

    def test_only_known_categories_are_returned(self):
        for name, target, install, _want in self.CASES:
            with self.subTest(name=name):
                self.assertIn(core.classify_program(name, target, install),
                              core.CATEGORY_ORDER)

    def test_manual_override_wins(self):
        self.assertEqual(
            core.classify_program("Steam", r"X:\steam\steam.exe", "", override="office"),
            "office", "手动指定的类别必须覆盖自动判断")
        # 未知的 override 要被忽略，回落到自动判断
        self.assertEqual(
            core.classify_program("Steam", r"X:\steam\steam.exe", "", override="不存在的类别"),
            "game")

    def test_whitespace_and_case_insensitive(self):
        self.assertEqual(core.classify_program("VISUAL STUDIO CODE"), "dev")
        self.assertEqual(core.classify_program("Visual Studio Code"), "dev")
        self.assertEqual(core.classify_program("visualstudiocode"), "dev")
        self.assertEqual(core.classify_program("Visual-Studio-Code"), "dev")

    def test_empty_inputs_do_not_crash(self):
        self.assertEqual(core.classify_program(""), core.CATEGORY_OTHER)
        self.assertEqual(core.classify_program("", ""), core.CATEGORY_OTHER)

    def test_labels_exist_for_every_category(self):
        for key, label in core.PROGRAM_CATEGORIES:
            self.assertTrue(label)
            self.assertEqual(core.CATEGORY_LABELS[key], label)
        self.assertIn(core.CATEGORY_OTHER, core.CATEGORY_ORDER)


class TestGroupItemsByCategory(unittest.TestCase):
    def test_groups_in_stable_order(self):
        items = [
            item("Steam", r"X:\steam\steam.exe"),
            item("微信", r"C:\Program Files\Tencent\WeChat\WeChat.exe"),
            item("Google Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            item("e管家", r"X:\StarSoftComm\CooCare.exe"),
        ]
        groups = core.group_items_by_category(items)
        keys = [k for k, _ in groups]
        self.assertEqual(keys, ["game", "browser", "system", "other"])

    def test_every_item_appears_exactly_once(self):
        items = [item(f"app{i}.exe", rf"X:\x\app{i}.exe") for i in range(20)]
        items.append(item("Steam", r"X:\steam\steam.exe"))
        groups = core.group_items_by_category(items)
        total = sum(len(g) for _k, g in groups)
        self.assertEqual(total, len(items))
        seen = [id(it) for _k, g in groups for it in g]
        self.assertEqual(len(seen), len(set(seen)), "有条目被分到多个组里")

    def test_empty_categories_are_not_returned(self):
        groups = core.group_items_by_category([item("Steam", r"X:\steam\steam.exe")])
        self.assertEqual([k for k, _ in groups], ["game"])

    def test_manual_override_is_respected(self):
        it = item("Steam", r"X:\steam\steam.exe")
        groups = core.group_items_by_category([it], overrides={
            r"x:\steam\steam.exe": "office"})
        self.assertEqual([k for k, _ in groups], ["office"])

    def test_empty_input(self):
        self.assertEqual(core.group_items_by_category([]), [])


# ===========================================================================
# 收藏 / 最近使用
# ===========================================================================
class TestCatalogPrefsFavorites(unittest.TestCase):
    def setUp(self):
        self.p = core.CatalogPrefs()

    def test_toggle_adds_then_removes(self):
        t = r"X:\Program Files\bilibili\哔哩哔哩.exe"
        self.assertFalse(self.p.is_favorite(t))
        self.assertTrue(self.p.toggle_favorite(t, "哔哩哔哩"))
        self.assertTrue(self.p.is_favorite(t))
        self.assertEqual(self.p.favorite_count(), 1)
        self.assertFalse(self.p.toggle_favorite(t, "哔哩哔哩"))
        self.assertFalse(self.p.is_favorite(t))
        self.assertEqual(self.p.favorite_count(), 0)

    def test_favorite_matching_is_case_insensitive(self):
        self.p.toggle_favorite(r"X:\Steam\steam.exe")
        self.assertTrue(self.p.is_favorite(r"x:\steam\STEAM.EXE"))

    def test_add_favorite_is_idempotent(self):
        t = r"X:\steam\steam.exe"
        self.p.add_favorite(t, "Steam")
        self.p.add_favorite(t, "Steam")
        self.p.add_favorite(t.lower(), "Steam")
        self.assertEqual(self.p.favorite_count(), 1)

    def test_remove_favorite(self):
        t = r"X:\steam\steam.exe"
        self.p.add_favorite(t)
        self.p.remove_favorite(t.upper())
        self.assertEqual(self.p.favorite_count(), 0)

    def test_fallback_key_uses_name_when_no_path(self):
        """UWP 之类的条目没有 exe 路径，退回用软件名做键。"""
        self.p.toggle_favorite("", "设置")
        self.assertTrue(self.p.is_favorite("", "设置"))
        self.assertFalse(self.p.is_favorite("", "其他应用"))

    def test_empty_key_is_rejected(self):
        self.assertFalse(self.p.toggle_favorite("", ""))
        self.assertEqual(self.p.favorite_count(), 0)
        self.assertFalse(self.p.is_favorite("", ""))


class TestCatalogPrefsRecents(unittest.TestCase):
    def setUp(self):
        self.p = core.CatalogPrefs(max_recent=5)

    def test_record_and_order(self):
        now = time.time()
        for i in range(3):
            self.p.record_use(rf"X:\x\app{i}.exe", f"app{i}", ts=now + i)
        recents = self.p.recent_list()
        self.assertEqual([r["name"] for r in recents], ["app2", "app1", "app0"])

    def test_same_program_is_deduplicated_and_promoted(self):
        now = time.time()
        self.p.record_use(r"X:\x\a.exe", "a", ts=now)
        self.p.record_use(r"X:\x\b.exe", "b", ts=now + 1)
        self.p.record_use(r"X:\x\a.exe", "a", ts=now + 2)
        recents = self.p.recent_list()
        self.assertEqual([r["name"] for r in recents], ["a", "b"])
        self.assertEqual(len(recents), 2, "同一程序不应出现两条")

    def test_max_recent_is_enforced(self):
        now = time.time()
        for i in range(10):
            self.p.record_use(rf"X:\x\app{i}.exe", f"app{i}", ts=now + i)
        self.assertLessEqual(self.p.recent_count(), 5)
        self.assertEqual(self.p.recent_list()[0]["name"], "app9")

    def test_prune_recent_by_age(self):
        now = time.time()
        self.p.record_use(r"X:\x\old.exe", "old", ts=now - 90 * 86400)
        self.p.record_use(r"X:\x\new.exe", "new", ts=now)
        removed = self.p.prune_recent(max_age_days=30, now=now)
        self.assertEqual(removed, 1)
        self.assertEqual([r["name"] for r in self.p.recent_list()], ["new"])

    def test_prune_with_zero_days_is_noop(self):
        self.p.record_use(r"X:\x\a.exe", "a", ts=0)
        self.assertEqual(self.p.prune_recent(max_age_days=0), 0)
        self.assertEqual(self.p.recent_count(), 1)

    def test_is_recent(self):
        self.p.record_use(r"X:\x\a.exe", "a")
        self.assertTrue(self.p.is_recent(r"x:\X\A.EXE"))
        self.assertFalse(self.p.is_recent(r"X:\x\b.exe"))


class TestCatalogPrefsCategories(unittest.TestCase):
    def setUp(self):
        self.p = core.CatalogPrefs()

    def test_set_and_clear_override(self):
        t = r"X:\steam\steam.exe"
        self.p.set_category(t, "office")
        self.assertEqual(self.p.category_override(t), "office")
        self.p.set_category(t, "")
        self.assertEqual(self.p.category_override(t), "")
        self.assertEqual(self.p.overrides(), {})

    def test_unknown_category_clears_instead_of_storing(self):
        t = r"X:\steam\steam.exe"
        self.p.set_category(t, "office")
        self.p.set_category(t, "这个类别不存在")
        self.assertEqual(self.p.category_override(t), "")

    def test_override_is_used_by_classification(self):
        t = r"X:\steam\steam.exe"
        it = item("Steam", t)
        self.p.set_category(t, "office")
        self.assertEqual(core.category_of_item(it, self.p.category_override(t)), "office")
        self.assertEqual(core.category_of_item(it), "game")


class TestCatalogPrefsPersistence(unittest.TestCase):
    def test_round_trip(self):
        p = core.CatalogPrefs()
        t = r"X:\Program Files\bilibili\哔哩哔哩.exe"
        p.add_favorite(t, "哔哩哔哩")
        p.record_use(t, "哔哩哔哩", ts=1234567890.0)
        p.set_category(t, "media")

        q = core.CatalogPrefs.from_dict(p.to_dict())
        self.assertTrue(q.is_favorite(t))
        self.assertEqual(q.recent_count(), 1)
        self.assertEqual(q.recent_list()[0]["name"], "哔哩哔哩")
        self.assertEqual(q.category_override(t), "media")

    def test_from_dict_tolerates_garbage(self):
        for bad in (None, [], "字符串", 123, {"favorites": "不是列表"},
                    {"recents": [None, 7, {}, {"path": None}]},
                    {"categories": {"a": "不存在的类别"}}):
            with self.subTest(data=bad):
                p = core.CatalogPrefs.from_dict(bad)     # 不应抛异常
                self.assertIsInstance(p.favorites, list)
                self.assertIsInstance(p.recents, list)
                self.assertIsInstance(p.categories, dict)
        p = core.CatalogPrefs.from_dict({"categories": {"a": "不存在的类别"}})
        self.assertEqual(p.categories, {}, "未知类别必须被丢弃")

    def test_from_dict_sorts_and_trims_recents(self):
        data = {"recents": [{"path": f"X:\\x\\{i}.exe", "name": str(i), "ts": i}
                            for i in range(50)]}
        p = core.CatalogPrefs.from_dict(data, max_recent=3)
        self.assertEqual(p.recent_count(), 3)
        self.assertEqual([r["ts"] for r in p.recent_list()], [49, 48, 47])

    def test_dict_is_json_serializable(self):
        import json
        p = core.CatalogPrefs()
        p.add_favorite(r"X:\中文 路径\app.exe", "中文名")
        p.record_use(r"X:\中文 路径\app.exe", "中文名")
        p.set_category(r"X:\中文 路径\app.exe", "media")
        text = json.dumps(p.to_dict(), ensure_ascii=False)
        self.assertIn("中文名", text)
        q = core.CatalogPrefs.from_dict(json.loads(text))
        self.assertEqual(q.favorite_count(), 1)
        self.assertEqual(q.category_override(r"X:\中文 路径\app.exe"), "media")


# ===========================================================================
# 去重
# ===========================================================================
class TestDedupeCatalog(unittest.TestCase):
    def test_same_path_different_case_is_duplicate(self):
        items = [item("Steam", r"X:\steam\steam.exe"),
                 item("Steam", r"X:\steam\Steam.exe")]
        out = core.dedupe_catalog(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].target, r"X:\steam\steam.exe", "应保留先出现的那条")

    def test_same_name_and_install_location_is_duplicate(self):
        items = [item("网易云音乐", r"X:\CloudMusic\cloudmusic.exe", r"X:\CloudMusic"),
                 item("网易云音乐", r"X:\CloudMusic\cloudmusic2.exe", r"X:\CloudMusic")]
        self.assertEqual(len(core.dedupe_catalog(items)), 1)

    def test_different_programs_are_kept(self):
        items = [item("A", r"X:\a\a.exe"), item("B", r"X:\b\b.exe")]
        self.assertEqual(len(core.dedupe_catalog(items)), 2)

    def test_same_name_different_install_is_kept(self):
        """同名但装在不同目录的，是两个程序，不能合并。"""
        items = [item("Python", r"C:\Python311\python.exe", r"C:\Python311"),
                 item("Python", r"X:\Python314\python.exe", r"X:\Python314")]
        self.assertEqual(len(core.dedupe_catalog(items)), 2)

    def test_extra_fields_are_merged_into_survivor(self):
        """去重不能丢信息：注册表项里的发行商/安装位置要补进保留项。"""
        items = [item("Steam", r"X:\steam\steam.exe", source="startmenu"),
                 item("Steam", r"X:\steam\Steam.exe", install=r"X:\steam",
                      publisher="Valve", source="registry")]
        out = core.dedupe_catalog(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].publisher, "Valve")
        self.assertEqual(out[0].install_location, r"X:\steam")

    def test_empty_and_no_target_items(self):
        items = [item("无路径项1"), item("无路径项2")]
        self.assertEqual(len(core.dedupe_catalog(items)), 2)
        self.assertEqual(core.dedupe_catalog([]), [])

    def test_real_catalog_has_no_duplicate_targets(self):
        """
        真实目录（缓存或全新扫描）里不能有重复目标。

        这条会真的去构建目录：有缓存时读缓存（快），没缓存时会解析快捷方式（稍慢）。
        """
        items = core.build_program_catalog(use_cache=True)
        self.assertGreater(len(items), 10, "目录条目太少，测试前提不成立")
        targets = [(it.target or "").lower() for it in items if it.target]
        dupes = {t for t in targets if targets.count(t) > 1}
        self.assertEqual(dupes, set(), f"目录里存在重复目标：{list(dupes)[:3]}")
        self.assertEqual(len(core.dedupe_catalog(items)), len(items),
                         "对真实目录再跑一次去重，结果应当不变（幂等）")


class TestMergeRunningKeepsPidHonest(unittest.TestCase):
    """
    `_merge_running()` 写进条目的 pid 必须**只反映"这一轮真的匹配到活进程"**。

    2026-10-13 修掉的真 bug：pid 会跨会话留在 `winmon_catalog.json` 里，
    而系统会**回收**进程号。旧代码只在新匹配上时赋值、**从不归零**，于是
    "昨天在跑、今天没跑"的程序会一直挂着昨天的 pid；一旦那个号被别的程序占用，
    界面上它显示"运行中"，点「打开」被短路成"已在运行"（还会把**别的程序**的窗口
    切到前台、操作历史记一条"成功"）。
    """

    def test_stale_pid_from_the_cache_is_cleared(self):
        item = core.ProgramItem(name="绝对不存在的程序zzz",
                                target=r"C:\绝对不存在的目录\zzz.exe",
                                source="startmenu", pid=os.getpid())
        self.assertTrue(core.is_process_alive(item.pid), "前提：这个 pid 现在是活的")
        core._merge_running([item])
        self.assertEqual(item.pid, 0,
                         "没匹配到活进程时，缓存里那份 pid 必须作废（不能被回收号骗到）")

    def test_a_really_running_program_still_gets_its_pid(self):
        """
        归零不能把"真的在跑"也一起清掉：拿一个真实窗口的进程路径去匹配，必须还能配上。

        （没有任何可枚举窗口时跳过 —— 那是环境问题，不是缺陷。）
        """
        target_pid = 0
        target_path = ""
        for wi in core.enum_windows(include_cloaked=True):
            if not wi.pid or wi.is_hidden_surface:
                continue
            path, _mem = core.query_process_detail(wi.pid)
            if path and os.path.exists(path):
                target_pid, target_path = wi.pid, path
                break
        if not target_pid:
            self.skipTest("当前没有可枚举的窗口，无法验证匹配那一半")
        item = core.ProgramItem(name="随便什么名字", target=target_path, pid=0)
        core._merge_running([item])
        self.assertEqual(item.pid, target_pid,
                         f"按路径匹配活进程那一半失效了（{target_path}）")


class TestIsProgramRunningUsesFullPath(unittest.TestCase):
    """
    "这个程序在不在跑"要**按完整路径**判，不能只看 exe 文件名。

    2026-10-13 修掉的真 bug：`C:\\A\\app.exe` 在跑时，`X:\\B\\app.exe` 也被判成
    "已在运行" —— `launch_item()` 于是什么都不启动、只回一句"已在运行，已尝试切到前台"，
    还把那个程序的窗口切到前台。两个目录各装一份 / 便携版与安装版并存就会碰到。
    """

    #: 伪造的进程快照：**只有一份在跑**（就是 running_path 那一份）
    def _procs(self, running_path: str):
        return {222: {"name": "dup.exe", "ppid": 0, "path": running_path}}

    def test_same_name_but_other_directory_is_not_a_match(self):
        procs = self._procs(r"X:\B\dup.exe")
        self.assertFalse(core.is_program_running(r"C:\A\dup.exe", procs),
                         "另一个目录的同名程序在跑，不该算 'A 已经在运行'")
        self.assertTrue(core.is_program_running(r"X:\B\dup.exe", procs),
                        "真正在跑的那一份必须认得出来")

    def test_pathless_snapshot_still_matches_by_name(self):
        """拿不到路径时（Toolhelp 只有名字）退回按名字算 —— 宁可少启动，也别重复启动。"""
        procs = {333: {"name": "dup.exe", "ppid": 0, "path": ""}}
        with mock.patch.object(core, "query_process_detail",
                               side_effect=Exception("查不到")):
            self.assertTrue(core.is_program_running(r"X:\B\dup.exe", procs))

    def test_bare_name_target_matches_any_path(self):
        """目标只给了文件名（没有目录）时，按名字命中即可（无从比路径）。"""
        procs = self._procs(r"X:\B\dup.exe")
        self.assertTrue(core.is_program_running("dup.exe", procs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
