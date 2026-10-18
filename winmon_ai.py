# -*- coding: utf-8 -*-
"""
WinMon AI 工作伴侣 —— AI 会话识别 / 本地事件（Incident）/ 剧本库。

设计边界（对应 MCP.md §10）：

1. **不主动调 AI**：这一层只做本地判断，不联网、不需要 API key。
   AI 通过 MCP 按需来读；"稳定提醒"由本地事件中心 + 托盘气泡负责。
2. **不依赖 tkinter**：可以单独 import，也方便命令行 / MCP 子进程使用。
3. **可测**：识别、打分、事件检测、去重都是纯函数或纯状态机，
   喂固定快照就能断言，不需要真的开着 AI 客户端。

主要对象：
    AiSession       一个"正在使用的 AI"（桌面客户端或网页标签）
    AiTracker       前台时长累计器（GUI 每 700ms 喂一次）
    Incident        一条本地事件（症状 + 证据 + 剧本）
    IncidentStore   事件状态机 + 去重 + 落盘（%LOCALAPPDATA%\\WinMon）
    PLAYBOOKS       剧本库（症状 → 检查 → 处理步骤）

数据只写到 `core.data_dir()`（运行时数据目录），不写程序目录。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field

import winmon_core as core

# ============================================================================
# 一、常量：预算默认值与节流
# ============================================================================

#: 每日 token 预算默认值。用户 2026-10-18 明确指定 **1000 万**。
#: 注意：这是"WinMon 交出去的上下文"的估算上限，不是客户端账单（见 MCP.md §10.7）。
DEFAULT_DAILY_TOKEN_BUDGET = 10_000_000
#: 单条事件的证据包上限（估算 token）
DEFAULT_INCIDENT_TOKEN_BUDGET = 2_000
#: 每小时工具调用上限
DEFAULT_HOURLY_CALLS = 60
#: 同一个事件两次提醒之间至少隔多久（秒）
INCIDENT_DEDUP_SECONDS = 600
#: 已解决的事件保留多久（秒）
INCIDENT_RETENTION_SECONDS = 24 * 3600
#: 事件文件里最多保留多少条
INCIDENT_MAX_ITEMS = 100

#: 进程名 → (显示名, 厂商)。只放"确定是 AI 客户端"的进程，避免误判。
DESKTOP_AI_APPS = {
    "claude.exe": ("Claude Desktop", "Anthropic"),
    "chatgpt.exe": ("ChatGPT", "OpenAI"),
    "copilot.exe": ("Microsoft Copilot", "Microsoft"),
    "cursor.exe": ("Cursor", "Anysphere"),
    "windsurf.exe": ("Windsurf", "Codeium"),
    "zed.exe": ("Zed", "Zed Industries"),
    "raycast.exe": ("Raycast", "Raycast"),
    "doubao.exe": ("豆包", "ByteDance"),
    "kimi.exe": ("Kimi", "Moonshot"),
    "tongyi.exe": ("通义千问", "Alibaba"),
    "yiyan.exe": ("文心一言", "Baidu"),
    "deepseek.exe": ("DeepSeek", "DeepSeek"),
}

#: 窗口标题关键词 → 显示名。用于网页标签，也用于"进程不认识但标题像 AI"的兜底。
TITLE_HINTS = (
    ("chatgpt", "ChatGPT"),
    ("claude", "Claude"),
    ("gemini", "Gemini"),
    ("copilot", "Copilot"),
    ("perplexity", "Perplexity"),
    ("cursor", "Cursor"),
    ("windsurf", "Windsurf"),
    ("kimi", "Kimi"),
    ("豆包", "豆包"),
    ("通义", "通义千问"),
    ("文心", "文心一言"),
    ("deepseek", "DeepSeek"),
    ("元宝", "腾讯元宝"),
    ("智谱", "智谱清言"),
    ("mistral", "Mistral"),
    ("grok", "Grok"),
)

#: 网页端 AI 的域名 → 显示名（UIA 读到地址栏时使用；读不到就靠标题）
WEB_HOSTS = {
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "claude.ai": "Claude",
    "gemini.google.com": "Gemini",
    "copilot.microsoft.com": "Copilot",
    "www.perplexity.ai": "Perplexity",
    "perplexity.ai": "Perplexity",
    "kimi.moonshot.cn": "Kimi",
    "www.doubao.com": "豆包",
    "doubao.com": "豆包",
    "tongyi.aliyun.com": "通义千问",
    "yiyan.baidu.com": "文心一言",
    "chat.deepseek.com": "DeepSeek",
    "yuanbao.tencent.com": "腾讯元宝",
    "chatglm.cn": "智谱清言",
    "grok.com": "Grok",
}

#: 这些"父进程"只是壳，识别 MCP 客户端时要继续往上找
SHELL_PROCS = {
    "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe", "explorer.exe",
    "py.exe", "python.exe", "pythonw.exe", "wscript.exe", "cscript.exe",
}

#: 可疑的"像指令"的片段（只用来打标记，不做任何执行）
_INJECTION_HINTS = ("ignore previous", "ignore all previous", "忽略之前", "忽略以上",
                    "忽略前面", "system:", "assistant:", "human:", "<system")


def sanitize_untrusted(text: str, limit: int = 200) -> tuple:
    """
    清洗来自其它程序的文本（窗口标题 / 进程名 / 路径）。

    返回 `(清洗后的文本, 是否可疑)`。只做三件事：去不可见字符、折叠换行/控制符、
    截断；**不做**任何"执行/解释"动作。可疑标记只是给 AI 与用户看的提示。
    """
    raw = text or ""
    try:
        raw = core._strip_invisible(raw)
    except Exception:
        pass
    cleaned = "".join(ch if (ch >= " " and ch != "\x7f") else " " for ch in raw)
    cleaned = " ".join(cleaned.split())
    low = cleaned.lower()
    suspicious = any(hint in low for hint in _INJECTION_HINTS)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned, suspicious


def _matches_ai(needle: str, session: "AiSession") -> bool:
    """pin / 连接名与某个会话是否指同一个 AI（宽一点，但仍是明确名字）。"""
    n = (needle or "").strip().lower()
    if not n:
        return False
    name = (session.name or "").lower()
    exe = os.path.basename((session.proc or "").lower())
    return n == name or n in name or (bool(exe) and n == exe)


# ============================================================================
# 二、AI 会话识别
# ============================================================================

@dataclass
class AiSession:
    """一个"用户可能正在使用"的 AI。"""

    kind: str                    # "desktop" / "web"
    name: str
    vendor: str = ""
    proc: str = ""
    pid: int = 0
    hwnd: int = 0
    title: str = ""
    url: str = ""
    source: str = ""             # mcp_parent / process_table / window_title / user_pin
    confidence: float = 0.0
    foreground_seconds: float = 0.0
    activations: int = 0
    cpu_share: float = 0.0
    mem_share: float = 0.0
    score: float = 0.0
    pinned: bool = False

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"

    def to_dict(self) -> dict:
        title, suspicious = sanitize_untrusted(self.title, 120)
        return {
            "kind": self.kind, "name": self.name, "vendor": self.vendor,
            "proc": self.proc, "pid": self.pid, "hwnd": self.hwnd,
            "title": title, "title_suspicious": suspicious, "url": self.url,
            "source": self.source,
            "confidence": round(self.confidence, 3),
            "foreground_seconds_30m": round(self.foreground_seconds, 1),
            "activations_30m": self.activations,
            "cpu_share": round(self.cpu_share, 4),
            "mem_share": round(self.mem_share, 4),
            "score": round(self.score, 1), "pinned": self.pinned,
        }


def _match_title(title: str) -> str:
    """窗口标题里是否出现某个 AI 的名字？返回显示名或空串。"""
    low = (title or "").lower()
    for hint, name in TITLE_HINTS:
        if hint and hint in low:
            return name
    return ""


def host_to_name(url: str) -> str:
    """从 URL 里取主机名并映射成 AI 名；不是已知 AI 就返回空串。"""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    if host in WEB_HOSTS:
        return WEB_HOSTS[host]
    # 允许子域（例如 chat.claude.ai）
    for known, name in WEB_HOSTS.items():
        if host.endswith("." + known):
            return name
    return ""


def classify_window(proc: str, title: str, url: str = "") -> tuple:
    """
    判断一个窗口属于哪个 AI。

    返回 `(kind, name, vendor, confidence, source)`；不是 AI 返回 `("", "", "", 0, "")`。
    `kind` 只会是 `desktop` / `web`（空表示不认识）。
    """
    p = (proc or "").lower().strip()
    if p and not p.endswith(".exe"):
        p += ".exe"

    # 1) 有地址栏（UIA 读到）时最可靠：先按域名判网页端
    if url:
        name = host_to_name(url)
        if name:
            return ("web", name, "", 0.9, "uia_url")
        # 已知浏览器但地址不是 AI 站点：直接判"不是 AI"，别再用标题去猜
        if p in core.BROWSER_PROCESSES:
            return ("", "", "", 0.0, "")

    # 2) 浏览器：只能靠标题（页面标题通常带 AI 名字）
    if p in core.BROWSER_PROCESSES:
        name = _match_title(title)
        if name:
            return ("web", name, "", 0.65, "window_title")
        return ("", "", "", 0.0, "")

    # 3) 已知桌面客户端：进程名就是实锤
    if p in DESKTOP_AI_APPS:
        name, vendor = DESKTOP_AI_APPS[p]
        return ("desktop", name, vendor, 0.95, "process_table")

    # 4) 兜底：进程不认识，但标题像 AI（例如 Electron 客户端改名了）
    name = _match_title(title)
    if name:
        return ("desktop", name, "", 0.55, "window_title")
    return ("", "", "", 0.0, "")


def session_key_for_window(wi, procs: dict | None = None) -> str:
    """
    给 `AiTracker` 用的轻量 key（`kind:name`）。

    只做分类，不做任何枚举/查询；窗口对象为 None 时返回空串。
    """
    if wi is None:
        return ""
    proc = getattr(wi, "proc", "") or ""
    if not proc and procs:
        proc = (procs.get(getattr(wi, "pid", 0), {}) or {}).get("name", "")
    kind, name, _vendor, _conf, _src = classify_window(proc, getattr(wi, "title", ""))
    return f"{kind}:{name}" if name else ""


class AiTracker:
    """
    前台时长累计器（只记内存，不落盘）。

    调用方（GUI）每 700ms 喂一次"当前前台窗口属于哪个 AI"即可。
    命令行 / MCP 是**一次性**查询，没有历史，所以 `foreground_seconds` 为 0 ——
    这不是错误，工具描述里会写清楚"累计时长需要 WinMon 界面在跑"。
    """

    def __init__(self, window_seconds: float = 1800.0) -> None:
        self.window_seconds = float(window_seconds)
        self._key = ""
        self._since = 0.0
        self._seconds: dict[str, float] = {}
        self._activations: dict[str, int] = {}
        self._last_seen: dict[str, float] = {}

    def observe(self, now: float, key: str) -> None:
        """喂一拍：`key` 为空表示前台不是 AI。"""
        if self._key:
            self._seconds[self._key] = self._seconds.get(self._key, 0.0) + max(
                0.0, now - self._since)
        if key:
            if key != self._key:
                self._activations[key] = self._activations.get(key, 0) + 1
            self._last_seen[key] = now
        self._key = key or ""
        self._since = now

    def stats(self, key: str, now: float) -> tuple:
        """返回 `(前台秒数, 激活次数, 新鲜度 0~1)`；秒数按 30 分钟窗口截断。"""
        seconds = self._seconds.get(key, 0.0)
        if self._key == key and now > self._since:
            seconds += now - self._since
        seconds = min(seconds, self.window_seconds)
        acts = self._activations.get(key, 0)
        last = self._last_seen.get(key, 0.0)
        if not last:
            fresh = 0.0
        else:
            fresh = max(0.0, 1.0 - (now - last) / max(60.0, self.window_seconds / 6.0))
        return (seconds, acts, min(1.0, fresh))


def _resource_shares(sessions: list, windows: list) -> None:
    """
    给每个会话算"资源占比"（只用于并列时打破平局）。

    做法：该会话所有窗口的私有提交 / 所有可见窗口的私有提交。
    网页端（浏览器）的占比**不可归因到具体站点**，所以恒为 0（见 MCP.md §10.1）。
    """
    total_mem = sum(max(0, getattr(w, "mem_private_kb", 0)) for w in windows) or 0
    by_pid: dict[int, int] = {}
    for w in windows:
        by_pid[w.pid] = by_pid.get(w.pid, 0) + max(0, getattr(w, "mem_private_kb", 0))
    for s in sessions:
        if s.kind == "web" or not total_mem:
            s.mem_share = 0.0
            s.cpu_share = 0.0
            continue
        s.mem_share = min(1.0, by_pid.get(s.pid, 0) / total_mem)
        s.cpu_share = 0.0


def identify_ai_sessions(windows: list, procs: dict | None = None,
                         tracker: AiTracker | None = None,
                         foreground_hwnd: int = 0, pin: str = "",
                         connected: dict | None = None,
                         now: float | None = None) -> dict:
    """
    识别"现在有哪些 AI、主要在用的是哪个"。

    - `windows`：`WinInfo` 列表（`core.enum_windows` / `collect_desktop` 的结果）
    - `tracker`：前台累计器；为 None 时前台时长按 0 算
    - `connected`：MCP 连接级识别结果（`detect_connected_client()`），最可靠
    - `pin`：用户指定的主 AI（显示名或进程名，大小写不敏感）

    返回 `{"primary": dict|None, "others": [...], "ambiguous": bool, "pinned": bool,
    "connected": dict|None, "note": str}`。
    """
    now = time.monotonic() if now is None else now
    procs = procs or {}
    grouped: dict[str, dict] = {}

    for wi in windows:
        if getattr(wi, "is_hidden_surface", False):
            continue
        proc = getattr(wi, "proc", "") or (procs.get(wi.pid, {}) or {}).get("name", "")
        kind, name, vendor, conf, source = classify_window(proc, getattr(wi, "title", ""))
        if not name:
            continue
        key = f"{kind}:{name}"
        cur = grouped.get(key)
        if cur is None:
            cur = {"kind": kind, "name": name, "vendor": vendor, "proc": proc,
                   "pid": wi.pid, "hwnd": wi.hwnd, "title": wi.title,
                   "confidence": conf, "source": source, "windows": 0}
            grouped[key] = cur
        cur["windows"] += 1
        # 前台窗口优先当代表；否则保留第一个
        if wi.hwnd == foreground_hwnd:
            cur.update(proc=proc, pid=wi.pid, hwnd=wi.hwnd, title=wi.title,
                       confidence=max(conf, cur["confidence"]), source=source)
        elif cur["hwnd"] != foreground_hwnd and conf > cur["confidence"]:
            cur.update(proc=proc, pid=wi.pid, hwnd=wi.hwnd, title=wi.title,
                       confidence=conf, source=source)

    sessions: list[AiSession] = []
    fresh_by_key: dict[str, float] = {}
    for key, cur in grouped.items():
        s = AiSession(kind=cur["kind"], name=cur["name"], vendor=cur["vendor"],
                      proc=cur["proc"], pid=cur["pid"], hwnd=cur["hwnd"],
                      title=cur["title"], source=cur["source"],
                      confidence=cur["confidence"])
        if tracker is not None:
            s.foreground_seconds, s.activations, fresh = tracker.stats(key, now)
        else:
            fresh = 1.0 if s.hwnd == foreground_hwnd else 0.0
        fresh_by_key[key] = fresh
        sessions.append(s)

    # 资源占比要先算出来再打分（否则 0.2 那一项永远是 0）
    _resource_shares(sessions, windows)
    for s in sessions:
        s.score = (3.0 * s.foreground_seconds / 60.0
                   + 1.0 * s.activations
                   + 1.5 * fresh_by_key.get(s.key, 0.0)
                   + 0.2 * (s.cpu_share + s.mem_share))

    pin_low = (pin or "").strip().lower()
    pinned = None
    for s in sessions:
        if pin_low and _matches_ai(pin_low, s):
            s.pinned = True
            s.score = 1000.0
            pinned = s
    if pinned is None and pin_low:
        # pin 指向的 AI 现在没开：仍然如实返回，避免"选了却不生效"的困惑
        pinned = AiSession(kind="desktop", name=pin.strip(), source="user_pin",
                           confidence=1.0, score=1000.0, pinned=True)
        sessions.append(pinned)

    conn_name = ""
    if connected:
        conn_name = (connected.get("name") or "").lower()
    if conn_name:
        matched = False
        for s in sessions:
            if _matches_ai(conn_name, s):
                s.score += 100.0
                s.source = "mcp_parent"
                s.confidence = max(s.confidence, float(connected.get("confidence", 0.95)))
                matched = True
        if not matched:
            # 客户端可能最小化 / 藏在托盘：连接本身就是实锤，补一条会话
            name = connected.get("name") or connected.get("exe") or "MCP 客户端"
            sessions.append(AiSession(
                kind="desktop", name=str(name), vendor=connected.get("vendor", ""),
                proc=connected.get("exe", ""), pid=int(connected.get("pid", 0) or 0),
                source="mcp_parent", confidence=float(connected.get("confidence", 0.95)),
                score=100.0 + 1.0))

    sessions.sort(key=lambda s: (-s.score, s.name.lower()))
    primary = sessions[0] if sessions and sessions[0].score > 0 else None
    others = [s for s in sessions if s is not primary]
    ambiguous = bool(primary and others and primary.score > 0
                     and (primary.score - others[0].score) / max(primary.score, 1e-6) < 0.15
                     and not primary.pinned)
    if not sessions:
        note = "当前没有识别到 AI 客户端或网页端 AI"
    elif primary is not None and primary.pinned:
        note = "按用户指定的主 AI"
    elif ambiguous:
        note = "前两名分差很小，按前台时长暂时判给第一名；建议手动指定主 AI"
    else:
        note = "按「连接状态 > 前台时长 > 交互新鲜度 > 资源占比」打分"
    return {
        "primary": primary.to_dict() if primary else None,
        "others": [s.to_dict() for s in others[:5]],
        "ambiguous": ambiguous,
        "pinned": bool(primary and primary.pinned),
        "connected": connected or None,
        "note": note,
    }


def collect_ai_sessions(pin: str = "", connected: dict | None = None) -> dict:
    """现场采集一次（命令行 / MCP 用）。会真的枚举窗口与进程。"""
    wins = core.collect_desktop(include_cloaked=True, with_detail=True)
    procs = core.snapshot_processes()
    fg = core.get_foreground_hwnd()
    return identify_ai_sessions(wins, procs, None, fg, pin=pin, connected=connected)


def detect_connected_client(max_depth: int = 4) -> dict | None:
    """
    识别"是谁拉起了这个 MCP 服务器"。

    这是最可靠的身份来源：stdio 模式下，本进程的祖先里一定有一个是 AI 客户端。
    会跳过 cmd / powershell / py / python 这些壳，往上找最多 `max_depth` 层。

    返回 `{"pid","name","path","exe","confidence","chain"}`；认不出返回 None。
    """
    try:
        procs = core.snapshot_processes()
        pid = os.getppid()
        chain = []
        for _ in range(max(1, max_depth)):
            info = procs.get(pid) or {}
            name = (info.get("name") or "").lower()
            if not name:
                break
            path, _mem = core.query_process_detail(pid, with_private=False)
            chain.append({"pid": pid, "name": name, "path": path})
            if name in DESKTOP_AI_APPS:
                display, vendor = DESKTOP_AI_APPS[name]
                return {"pid": pid, "name": display, "vendor": vendor,
                        "exe": name, "path": path, "confidence": 0.95,
                        "source": "mcp_parent", "chain": chain}
            if name not in SHELL_PROCS:
                # 不是壳、也不是已知 AI：可能是改名后的客户端，按标题/名字猜一次
                display = _match_title(name)
                if display:
                    return {"pid": pid, "name": display, "vendor": "",
                            "exe": name, "path": path, "confidence": 0.6,
                            "source": "mcp_parent", "chain": chain}
                return {"pid": pid, "name": info.get("name") or name, "vendor": "",
                        "exe": name, "path": path, "confidence": 0.3,
                        "source": "mcp_parent", "chain": chain}
            pid = int(info.get("ppid") or 0)
            if pid <= 0:
                break
    except Exception:
        return None
    return None


# ============================================================================
# 三、Incident（本地事件）
# ============================================================================

@dataclass
class Incident:
    """一条"出事了"的记录：症状 + 证据 + 剧本。"""

    kind: str
    severity: str
    subject: dict
    playbook_id: str
    confidence: float = 0.8
    signals: list = field(default_factory=list)
    evidence_refs: list = field(default_factory=list)
    state: str = "open"
    opened_at: str = ""
    updated_at: str = ""
    resolved_at: str = ""
    id: str = ""

    @property
    def key(self) -> str:
        return "|".join(str(x) for x in (
            self.kind, self.subject.get("pid", 0), self.subject.get("hwnd", 0),
            self.subject.get("name", "")))

    def to_dict(self) -> dict:
        subject = dict(self.subject)
        if "title" in subject:
            subject["title"], subject["title_suspicious"] = sanitize_untrusted(
                str(subject.get("title") or ""), 120)
        return {"id": self.id, "kind": self.kind, "severity": self.severity,
                "subject": subject, "playbook_id": self.playbook_id,
                "confidence": round(self.confidence, 2), "signals": self.signals,
                "evidence_refs": self.evidence_refs, "state": self.state,
                "opened_at": self.opened_at, "updated_at": self.updated_at,
                "resolved_at": self.resolved_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Incident":
        return cls(kind=d.get("kind", ""), severity=d.get("severity", "warn"),
                   subject=d.get("subject") or {},
                   playbook_id=d.get("playbook_id", ""),
                   confidence=float(d.get("confidence", 0.8)),
                   signals=list(d.get("signals") or []),
                   evidence_refs=list(d.get("evidence_refs") or []),
                   state=d.get("state", "open"),
                   opened_at=d.get("opened_at", ""),
                   updated_at=d.get("updated_at", ""),
                   resolved_at=d.get("resolved_at", ""),
                   id=d.get("id", ""))


def _now_text(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(
        time.time() if now is None else now))


def _incident_id(kind: str, subject: dict, now: float | None = None) -> str:
    return f"inc-{time.strftime('%Y%m%d', time.localtime(time.time() if now is None else now))}-{kind}-{subject.get('pid', 0)}-{subject.get('hwnd', 0)}"


def detect_incidents(windows: list, procs: dict | None = None,
                     disk_usage: tuple | None = None,
                     mcp_errors: list | None = None,
                     now: float | None = None) -> list:
    """
    从"当前这一拍"的快照里产生候选事件（纯函数）。

    只做**本地能确定**的判断；CPU 持续占用、内存增长这类需要历史的检测器
    留给后续阶段（它们必须有跨拍累计器才准，一次性查询会误报）。
    """
    out: list = []
    now = time.time() if now is None else now
    stamp = _now_text(now)

    for wi in windows:
        if getattr(wi, "is_hidden_surface", False) or getattr(wi, "iconic", False):
            continue
        if getattr(wi, "hung", False):
            subject = {"pid": wi.pid, "hwnd": wi.hwnd,
                       "name": getattr(wi, "proc", "") or "",
                       "title": (getattr(wi, "title", "") or "")[:80]}
            out.append(Incident(
                kind="app_hung", severity="warn", subject=subject,
                playbook_id="app_hung", confidence=0.9,
                signals=[{"name": "not_responding", "value": True}],
                evidence_refs=[f"proc:{wi.pid}", f"win:{wi.hwnd}"],
                opened_at=stamp, updated_at=stamp,
                id=_incident_id("app_hung", subject, now)))

    if disk_usage:
        total, free = 0, 0
        try:
            total, _used, free = disk_usage
            ratio = (free / total) if total else 1.0
        except Exception:
            ratio = 1.0
        if total and (ratio < 0.05 or free < 2 * 1024 * 1024 * 1024):
            subject = {"name": "系统盘", "free_bytes": int(free),
                       "total_bytes": int(total)}
            out.append(Incident(
                kind="disk_low", severity="error", subject=subject,
                playbook_id="disk_low", confidence=1.0,
                signals=[{"name": "free_ratio", "value": round(ratio, 4)}],
                evidence_refs=["disk:system"],
                opened_at=stamp, updated_at=stamp,
                id=_incident_id("disk_low", subject, now)))

    for err in (mcp_errors or [])[:5]:
        if not isinstance(err, dict):
            continue
        subject = {"name": "MCP 客户端", "tool": err.get("tool", ""),
                   "pid": err.get("pid", 0), "hwnd": 0}
        out.append(Incident(
            kind="mcp_tool_failed", severity="warn", subject=subject,
            playbook_id="mcp_tool_failed", confidence=0.8,
            signals=[{"name": "error", "value": str(err.get("error", ""))[:200]}],
            evidence_refs=[f"mcp:{err.get('method', '')}"],
            opened_at=stamp, updated_at=stamp,
            id=_incident_id("mcp_tool_failed", subject, now)))
    return out


class IncidentStore:
    """
    事件状态机：去重（同一事件 10 分钟只提醒一次）、状态流转、落盘。

    状态：`open`（新）/ `acked`（用户看过）/ `snoozed`（稍后）/ `resolved`（已恢复）。
    落盘位置由调用方传入（GUI 用 `incidents_path()`）；命令行 / MCP 只读或合并使用。
    """

    def __init__(self, path: str = "", limit: int = INCIDENT_MAX_ITEMS) -> None:
        self.path = path
        self.limit = int(limit)
        self._items: dict[str, Incident] = {}
        self._last_notify: dict[str, float] = {}
        self._dirty = False
        self.load()

    @staticmethod
    def _key_of(d: dict) -> str:
        return Incident.from_dict(d).key

    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for d in (data.get("incidents") or [])[:self.limit * 2]:
            if not isinstance(d, dict):
                continue
            inc = Incident.from_dict(d)
            if inc.kind and inc.key:
                self._items[inc.key] = inc
        self._last_notify = {k: float(v) for k, v in (data.get("last_notify") or {}).items()
                             if isinstance(v, (int, float))}

    def save(self) -> None:
        if not self.path:
            return
        try:
            core.ensure_data_dir()
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"incidents": [i.to_dict() for i in self._items.values()],
                           "last_notify": self._last_notify},
                          fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception:
            pass

    def observe(self, candidates: list, now: float | None = None) -> list:
        """
        喂一拍候选事件。返回**需要提醒**的（新建或距上次提醒超过 10 分钟）列表。

        没再出现的 `open` 事件会被标成 `resolved`；`acked` / `snoozed` 保留。
        """
        now = time.time() if now is None else now
        seen = set()
        fresh: list = []
        changed = False
        for inc in candidates:
            seen.add(inc.key)
            cur = self._items.get(inc.key)
            if cur is None:
                inc.id = inc.id or _incident_id(inc.kind, inc.subject, now)
                self._items[inc.key] = inc
                self._last_notify[inc.key] = now
                fresh.append(inc)
                changed = True
                continue
            cur.updated_at = inc.updated_at or cur.updated_at
            cur.signals = inc.signals
            cur.evidence_refs = inc.evidence_refs
            cur.confidence = inc.confidence
            if cur.state == "resolved":
                cur.state = "open"
                cur.resolved_at = ""
                changed = True
            if now - self._last_notify.get(inc.key, 0.0) >= INCIDENT_DEDUP_SECONDS:
                self._last_notify[inc.key] = now
                fresh.append(cur)
        for key, inc in list(self._items.items()):
            if key not in seen and inc.state in ("open", "acked"):
                inc.state = "resolved"
                inc.resolved_at = _now_text(now)
                inc.updated_at = inc.resolved_at
                changed = True
        changed = self._prune(now) or changed
        # 只有状态真的变了才落盘：GUI 每 2 秒喂一拍，不能每拍都写文件
        if changed:
            self.save()
        return fresh

    def _prune(self, now: float) -> bool:
        changed = False
        drop = []
        for key, inc in self._items.items():
            if inc.state == "resolved":
                try:
                    t = time.mktime(time.strptime(inc.resolved_at, "%Y-%m-%dT%H:%M:%S"))
                except Exception:
                    t = now
                if now - t > INCIDENT_RETENTION_SECONDS:
                    drop.append(key)
        for key in drop:
            self._items.pop(key, None)
            self._last_notify.pop(key, None)
            changed = True
        if len(self._items) > self.limit:
            order = sorted(self._items.values(), key=lambda i: i.updated_at)
            for inc in order[:len(self._items) - self.limit]:
                self._items.pop(inc.key, None)
                changed = True
        return changed

    def set_state(self, incident_id: str, state: str) -> bool:
        for inc in self._items.values():
            if inc.id == incident_id:
                inc.state = state
                inc.updated_at = _now_text()
                if state == "resolved":
                    inc.resolved_at = inc.updated_at
                self.save()
                return True
        return False

    def open_incidents(self, include_resolved: bool = False) -> list:
        items = list(self._items.values())
        if not include_resolved:
            items = [i for i in items if i.state != "resolved"]
        items.sort(key=lambda i: (0 if i.severity == "error" else 1, i.updated_at),
                   reverse=False)
        return items


def incidents_path() -> str:
    """
    事件文件在运行时数据目录里（不在程序目录里）。

    `WINMON_AI_INCIDENTS` 可覆盖（测试用：避免写用户真实数据）。
    """
    override = os.environ.get("WINMON_AI_INCIDENTS", "").strip()
    if override:
        return override
    return core.data_file("winmon_incidents.json")


def disk_usage_for_data_dir() -> tuple | None:
    """取运行时数据目录所在盘的 `(total, used, free)`；失败返回 None。"""
    try:
        path = core.data_dir() or os.environ.get("SystemDrive", "C:") + "\\"
        drive = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        return shutil.disk_usage(drive)
    except Exception:
        return None


def collect_incidents(limit: int = 20, include_resolved: bool = False) -> dict:
    """
    合并"已落盘的事件（GUI 写的）"与"这一拍能静态判定的事件"。

    **只读**：不会改写事件文件（查询不该产生副作用）。
    MCP / 命令行都走这里；没有 WinMon 界面在跑时，至少还能发现无响应窗口与磁盘告急。
    """
    store = IncidentStore(path=incidents_path())
    merged: dict[str, Incident] = {}
    for inc in store.open_incidents(include_resolved=include_resolved):
        merged[inc.key] = inc
    live = detect_incidents(core.enum_windows(include_cloaked=True),
                            disk_usage=disk_usage_for_data_dir())
    for inc in live:
        merged.setdefault(inc.key, inc)
    items = list(merged.values())
    items.sort(key=lambda i: (0 if i.severity == "error" else 1, i.updated_at))
    items = items[:max(1, min(100, int(limit or 20)))]
    return {"incidents": [i.to_dict() for i in items], "count": len(items),
            "source": "winmon_incidents.json + live"}


# ============================================================================
# 四、剧本库
# ============================================================================

PLAYBOOKS = {
    "app_hung": {
        "id": "app_hung", "title": "程序无响应",
        "checks": ["窗口是否在前台", "是否有模态对话框被遮住", "CPU 是否接近 0"],
        "steps": [
            {"do": "等待 5 秒并刷新一次", "verify": "窗口恢复响应", "risk": "无"},
            {"do": "尝试正常关闭（发送 WM_CLOSE）",
             "verify": "进程退出", "risk": "中：可能丢未保存内容"},
            {"do": "确认无用后再结束进程",
             "verify": "进程与窗口都消失", "risk": "高：未保存数据会丢"},
        ],
        "auto_safe": False,
    },
    "disk_low": {
        "id": "disk_low", "title": "系统盘空间不足",
        "checks": ["系统盘剩余空间", "WinMon 可清理项", "回收站大小"],
        "steps": [
            {"do": "先清理 WinMon 自己的缓存与日志（--clean --yes）",
             "verify": "可清理项归零且程序仍能启动", "risk": "低"},
            {"do": "检查用户临时目录与下载目录的大文件",
             "verify": "剩余空间回到 10% 以上", "risk": "中：删前先确认"},
        ],
        "auto_safe": False,
    },
    "mcp_tool_failed": {
        "id": "mcp_tool_failed", "title": "MCP 工具调用失败",
        "checks": ["客户端名称与版本", "失败工具名与错误码", "是否参数不合法"],
        "steps": [
            {"do": "先让 AI 重试一次同名工具", "verify": "返回 isError=false", "risk": "无"},
            {"do": "检查客户端是否支持当前协议版本",
             "verify": "server/discover 的 supportedVersions 含客户端版本", "risk": "低"},
        ],
        "auto_safe": False,
    },
    "ai_budget_exhausted": {
        "id": "ai_budget_exhausted", "title": "AI 上下文预算已用尽",
        "checks": ["今日估算用量", "预算上限", "是否有一次性大请求"],
        "steps": [
            {"do": "在 WinMon 界面里查看「AI 用量」并决定是否提高上限",
             "verify": "预算状态回到 OK", "risk": "低"},
        ],
        "auto_safe": False,
    },
}


def get_playbooks(playbook_id: str = "") -> list:
    """返回剧本列表（可按 id 过滤）。"""
    if playbook_id:
        item = PLAYBOOKS.get(playbook_id)
        return [item] if item else []
    return [PLAYBOOKS[k] for k in sorted(PLAYBOOKS)]


# ============================================================================
# 五、文本输出与 token 估算
# ============================================================================

def estimate_tokens(text: str) -> int:
    """
    粗估 token：英文约 4 字符/token，中文约 1.6 字符/token。

    只用于"我们交出去多少上下文"的预算，**不是账单**（MCP.md §10.7）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_chars = max(0, len(text) - cjk)
    return int(ascii_chars / 4 + cjk / 1.6 + 24)


def format_sessions_text(res: dict) -> str:
    """给人/模型看的一句话摘要。"""
    p = res.get("primary")
    if not p:
        return res.get("note") or "当前没有识别到 AI"
    line = (f"主 AI：{p.get('name')}（{p.get('kind')}，置信度 {p.get('confidence')}，"
            f"前台 {p.get('foreground_seconds_30m')} 秒 / 激活 {p.get('activations_30m')} 次）")
    if res.get("ambiguous"):
        line += "；注意：前两名分差很小"
    if res.get("pinned"):
        line += "；用户已指定"
    return line


def format_incidents_text(items: list) -> str:
    """把事件列表压成几行（给 CLI / MCP text 块用）。"""
    if not items:
        return "当前没有打开的本地事件"
    lines = [f"打开的事件 {len(items)} 条："]
    for inc in items[:10]:
        subj = inc.get("subject") or {}
        name = subj.get("name") or subj.get("title") or "?"
        lines.append(f"  · [{inc.get('severity')}] {inc.get('kind')}｜{name}"
                     f"｜{inc.get('state')}｜{inc.get('updated_at')}")
    return "\n".join(lines)


def incident_notification_text(inc: Incident) -> str:
    """托盘气泡的一句话（**不含不可信标题的原文**，避免把窗口标题当指令展示）。"""
    label = {
        "app_hung": "有程序无响应",
        "disk_low": "系统盘空间不足",
        "mcp_tool_failed": "MCP 工具调用失败",
        "ai_budget_exhausted": "AI 上下文预算已用尽",
    }.get(inc.kind, inc.kind)
    name = (inc.subject.get("name") or "").strip()
    return f"{label}：{name}" if name else label
