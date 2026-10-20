# -*- coding: utf-8 -*-
"""
A7 —— 输入通路边界的回归测试（跨进程验证）。

HANDOFF 里那张"输入通路边界表"是当时最重要的环境结论之一，
但它很容易被后来的改动悄悄破坏（例如有人把 `type_message` 改坏了，
或者哪天环境放开限制，这张表就该更新）。所以这里做**真跨进程**验证：

  子进程：开一个 Tk 窗口 + Entry，把 hwnd 写进文件，然后在若干秒内
          反复回读 Entry 内容并写进结果文件（这样父进程注入的文字能被捕获）
  父进程：读到 hwnd → 用 `WindowKeySender.type_message()` 注入文字 →
          读结果文件确认"注入的文字确实出现在**另一个进程**的编辑框里"

为什么不直接在本进程里测：同进程测不出"跨进程是否可用"，
而这张表的意义正在于跨进程边界。

判定标准（为什么不是"只要有内容就算过"）：
  1. 子进程没能创建窗口 / 完全没能回读 → **skip**（环境没有图形会话）
  2. 子进程回读了、但整个过程中从没成为前台 → **skip**（WM_CHAR 本来就只有前台窗口
     才处理，这条是环境限制，不是代码坏了）
  3. 子进程回读了、也确实前台过，却始终没见到 marker → **fail**
     （这才是真正要守的东西：消息通路被改坏了）
第 3 条是这个文件存在的意义。写这条时的实测：把 `type_message` 换成空实现，
它会如实报红（见 HANDOFF「判据要能真的报红」）。

关于"重试几次"（重要，别误会成掩盖问题）：
`PostMessage` 是一条 **fire-and-forget** 的投递通路，而跨进程抢前台会让 Tk 的
**内部焦点**被打断 —— 焦点一断，之后到达的 `WM_CHAR` 会被 Tk 直接丢掉。
实测 11 个字符曾只到 8 个（`'WinMonMs'`），**尾部丢失**。这属于 HANDOFF §五
写明的环境限制（"无法可靠地把系统焦点交给另一个窗口"），不是 `type_message` 的缺陷。
所以这里放开"重试若干次、只要**有一次完整到达**就算通过"：
真正坏掉的通路（消息号写错、发错窗口、根本不投递）**每次**都到不了，仍然会报红。

为什么不干脆只测单次投递：那样它会在好代码上随机变红（本次实测 6 次里红 1 次），
假红灯同样会让"改完跑测试"这条纪律失效 —— 和 lint 那 32 处噪音是一个道理。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import _marks as marks                   # noqa: E402
import winmon_core as core               # noqa: E402

#: 子进程用的靶子脚本。刻意写成字符串常量 + 落盘执行，
#: 这样不依赖项目里的其它文件，也不会被 unittest 收集。
#:
#: 结果文件用「写临时文件 + os.replace 原子改名」落盘：
#: 早期版本是 `open(out,"w")` 直接写，父进程可能在"文件已创建但还没写完"
#: 的瞬间读到半截（实测读到过 `'WinMonMsg'`，少了最后两个字符），
#: 于是把一个好端端的通路判成了坏的。原子改名让父进程只可能读到完整内容。
TARGET_SCRIPT = r'''
# -*- coding: utf-8 -*-
"""靶子：开一个 Tk Entry 窗口，把 hwnd 报给父进程，并持续回读内容。"""
import json
import os
import sys
import time
import tkinter as tk

info_path, out_path, progress_path, project_dir, marker = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])

sys.path.insert(0, project_dir)
import winmon_core as core


def write_atomic(path, text, tries=40):
    """原子落盘：先写 .tmp 再改名，父进程不会读到写了一半的内容。

    带重试：Windows 上目标文件正好被父进程打开读取时，改名会失败。
    """
    tmp = path + ".tmp"
    last = None
    for _ in range(tries):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
            return
        except OSError as exc:                     # 目标正被读取时改名会失败
            last = exc
            time.sleep(0.02)
    raise last if last else RuntimeError("write_atomic failed")


def append_progress(path, text):
    """把"新读到的内容"追加进进度文件（追加是安全的，不需要原子改名）。

    父进程靠它尽早发现"完整 marker 已经到了"，从而提前收工。
    """
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text.replace("\n", " ") + "\n")
    except OSError:
        pass


def main():
    root = tk.Tk()
    root.title("WinMon-Input-Target-Child")
    root.geometry("420x200+120+120")
    entry = tk.Entry(root, width=28)
    entry.pack(pady=40)
    root.update()
    root.update_idletasks()
    time.sleep(0.3)
    root.update()

    hwnd = core.H(core.user32.GetAncestor(core.w.HWND(root.winfo_id()), core.GA_ROOT))

    # 自己把自己切到前台：当时里"由别的进程把窗口置前"会失败
    # （前台锁），但窗口自身调用通常可以。WM_CHAR 只在窗口是前台时才被处理。
    try:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.focus_force()
        entry.focus_set()
        core.user32.SetForegroundWindow(core.w.HWND(hwnd))
        core.user32.BringWindowToTop(core.w.HWND(hwnd))
        core.user32.SetFocus(core.w.HWND(hwnd))
    except Exception:
        pass
    root.update()

    foreground = core.H(core.user32.GetForegroundWindow()) == hwnd
    write_atomic(info_path, json.dumps({"hwnd": hwnd, "pid": os.getpid(),
                                        "foreground": foreground}))

    # 持续回读：见到完整 marker 就提前收工（正常约 1 秒），否则最多等 6 秒。
    # 记录"读到过的最长内容"而不是"最后一次读到什么" —— 最后一次读到的可能
    # 恰好是消息还没排队完毕时的前缀，那样会把好通路判坏。
    deadline = time.time() + 6
    seen = []
    tcl_errors = 0
    foreground_any = foreground
    last_reported = ""
    while time.time() < deadline:
        try:
            root.update()
            # 每轮重新确认 Tk 的**内部**焦点：跨进程抢前台会把焦点打断，
            # 之后到达的 WM_CHAR 会被 Tk 丢掉（实测 11 个字符丢过尾部 3 个）。
            # focus_set() 只动 Tk 内部焦点，不再去抢系统前台，避免互相打架。
            entry.focus_set()
        except tk.TclError:
            # 窗口被外部销毁时 update 会抛错；先重试几次，避免把偶发当致命
            tcl_errors += 1
            if tcl_errors >= 3:
                break
            time.sleep(0.1)
            continue
        if core.H(core.user32.GetForegroundWindow()) == hwnd:
            foreground_any = True
        text = entry.get()
        seen.append(text)
        if text != last_reported:
            last_reported = text
            append_progress(progress_path, text)   # 让父进程能尽早看到 marker
        if marker in text:
            break
        time.sleep(0.1)

    best = max(seen, key=len) if seen else ""
    write_atomic(out_path, json.dumps({
        "hwnd": hwnd, "foreground": foreground, "foreground_any": foreground_any,
        "marker": marker, "reads": len(seen), "best": best, "tail": seen[-3:],
        "tcl_errors": tcl_errors, "saw_marker": any(marker in t for t in seen),
    }, ensure_ascii=False))
    try:
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    main()
'''

def _progress_has(path: str, marker: str) -> bool:
    """进度文件里是否已经出现完整 marker（父进程用它尽早收工）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return marker in fh.read()
    except OSError:
        return False


@marks.slow("真起子进程 + 跨进程前台输入")
class TestMessageInputAcrossProcesses(unittest.TestCase):
    """`WindowKeySender.type_message()` 必须能到达**另一个进程**的编辑框。"""

    def test_wm_char_reaches_another_process_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "target.py")
            info = os.path.join(tmp, "info.json")
            out = os.path.join(tmp, "out.json")
            progress = os.path.join(tmp, "progress.log")
            marker = "WinMonMsgOK"
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(TARGET_SCRIPT)

            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen([sys.executable, script, info, out, progress,
                                     h.PROJECT_DIR, marker],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    cwd=tmp, creationflags=flags)
            self.addCleanup(lambda: proc.poll() is None and proc.kill())

            # 等子进程把窗口开起来
            hwnd = 0
            reported = {}
            for _ in range(60):
                if proc.poll() is not None:
                    err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                    self.skipTest(f"子进程提前退出：{err[-400:]}")
                if os.path.exists(info):
                    try:
                        with open(info, encoding="utf-8") as fh:
                            reported = json.load(fh)
                        hwnd = int(reported.get("hwnd") or 0)
                    except Exception:
                        hwnd = 0
                    if hwnd:
                        break
                time.sleep(0.2)
            if not hwnd:
                self.skipTest("子进程没能创建窗口（环境没有图形会话）")

            sender = core.WindowKeySender(hwnd)
            if not reported.get("foreground"):
                # 靶子自己没抢到前台时才由父进程出手（AttachThreadInput 组合拳）。
                # 不无谓地抢：抢前台的过程会打断 Tk 的内部焦点，反而更容易丢尾部字符。
                try:
                    sender.focus()
                except Exception:
                    pass

            # 注入文字：这条通路在本环境里是可用的（见 HANDOFF 的边界表）。
            # 允许多试几次 —— 理由见文件头 docstring（fire-and-forget + 前台抖动）。
            # 每次尝试之间看进度文件，一旦完整 marker 出现就收工。
            attempts = 0
            delivered = False
            for attempts in range(1, 7):
                sender.type_message(marker)
                for _ in range(6):
                    if _progress_has(progress, marker):
                        delivered = True
                        break
                    time.sleep(0.1)
                if delivered:
                    break

            # 等子进程回读并原子落盘（它见到完整 marker 会立刻收工）
            result = None
            for _ in range(60):
                if os.path.exists(out):
                    try:
                        with open(out, encoding="utf-8") as fh:
                            result = json.load(fh)
                        break
                    except Exception:
                        result = None
                time.sleep(0.2)

            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

            if delivered and result and result.get("saw_marker"):
                return                                   # 通路可用，收工
            if not result:
                self.skipTest("子进程没有落盘回读结果（窗口可能开不起来）")
            if not result.get("reads"):
                self.skipTest("子进程一次都没能回读编辑框内容（环境没有图形会话）")
            if not result.get("foreground_any"):
                # WM_CHAR 只被前台窗口处理：整个过程都没抢到前台就测不成，属环境限制
                self.skipTest("靶子窗口全程没能成为前台窗口，WM_CHAR 本就不会被处理")
            self.assertTrue(result.get("saw_marker"),
                            "WM_CHAR 没有到达另一个进程的编辑框"
                            f"（注入 {attempts} 次、回读 {result.get('reads')} 次，"
                            f"最长读到 {result.get('best')!r}，"
                            f"最近几次 {result.get('tail')!r}，"
                            f"target hwnd={result.get('hwnd')}）")

    def test_key_message_is_safe_on_bogus_hwnd(self):
        """消息通路对无效窗口只能安静失败，不能抛异常。"""
        try:
            core.WindowKeySender(0xFFFF0000).key_message(0x0D)
            core.WindowKeySender(0xFFFF0000).type_message("abc")
        except Exception as exc:                             # pragma: no cover
            self.fail(f"对无效窗口发消息抛异常了：{type(exc).__name__}: {exc}")

    def test_render_widget_prefers_chromium_child(self):
        """有 Chromium 子窗口时要选中它（按键消息发给它才有效）。"""
        wi = core.find_video_window("bilibili")
        if wi is None:
            self.skipTest("哔哩哔哩没有运行")
        sender = core.WindowKeySender(wi.hwnd)
        rw = sender.render_widget()
        self.assertTrue(rw)
        # 要么是渲染子窗口（不等于主窗口），要么退回主窗口
        self.assertTrue(core.user32.IsWindow(core.w.HWND(rw)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
