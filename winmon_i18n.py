# -*- coding: utf-8 -*-
"""
WinMon 界面语言（中文 / English）。

设计取舍
--------
1. **默认中文**：现有用户、测试、文档全是中文，切到英文必须是显式选择。
2. 语言只影响"显示与提示词"，不影响窗口采集、进程操作、预算与 MCP 协议。
3. 主界面有 100 多处 `text="中文"` 的静态控件。与其在每一处都包一层
   `t(...)`（改动面大、容易漏），这里用 `translate_widgets()` 在窗口建好后
   按**中文原文**查表替换；切回中文时用反向表。动态文本（状态栏、计数、
   窗口标题）不在这张表里，它们继续按数据本身显示。
4. 新增窗口/按钮时，只要中文原文进了 `_STATIC_EN`，就会自动被覆盖；
   没进表也不会坏，只是保持中文。
5. `winmon_ai` / `winmon_mcp` 也能导入本模块（不依赖 tkinter，tkinter 只在
   真正翻译控件时延迟导入），所以无界面场景不会被拖累。

注意：不要把用户数据、窗口标题交给 `translate_text()`——它只该用于
WinMon 自己写死的界面文案。
"""
from __future__ import annotations

import json
import os

#: 语言代码 → 设置页显示名
LANGUAGES = {"zh": "中文", "en": "English"}
DEFAULT_LANGUAGE = "zh"

_current = DEFAULT_LANGUAGE

#: 带 key 的整句（用于对话框标题、提示、动态拼接的段落）
_STRINGS = {
    "zh": {
        "app.title": "WinMon {ver}",
        "settings.title": "WinMon 设置",
        "settings.language": "语言 / Language",
        "settings.language_hint": "切换后主界面与 AI 窗口立即生效；详细说明在「使用说明」标签页。",
        "settings.theme": "界面颜色",
        "settings.theme_hint": "普通是白底黑字，深色是黑底白字，跟随系统读取 Windows 的"
                               "浅色/深色设置。保存后立即生效；彩色高亮卡片保留。",
        "assistant.title": "WinMon AI 助手",
        "assistant.header_reading": "正在读取当前 AI…",
        "assistant.time_reading": "正在读取时间…",
        "assistant.mode": "模式：",
        "assistant.ask_hint": "问点什么，或直接点「交给 AI」…",
        "assistant.send": "交给 AI",
        "assistant.save": "保存回复",
        "assistant.connect": "接入 AI…",
        "assistant.quip": "搭子说一句",
        "assistant.history": "记录…",
        "assistant.usage": "用量…",
        "assistant.empty": "还没有记录。点「交给 AI」会把当前窗口现场复制给你正在用的 AI。",
        "assistant.reminder_owner": "提醒由接入的 AI 在对话里说；WinMon 自己不弹窗。",
        "connect.title": "接入 AI",
        "connect.head": "连接你的 AI",
        "connect.mcp": "1）MCP 接入（推荐，只读）",
        "connect.mcp_hint": "把下面这段加到客户端的 MCP 配置里，重启客户端。"
                            "接入后主界面的 AI 卡会显示「已接入 MCP（客户端名）」。",
        "connect.mcp_short": "让支持 MCP 的桌面客户端（如 Claude Desktop）直接读取 WinMon；"
                             "只读，不改任何东西。",
        "connect.show_steps": "查看配置与步骤",
        "connect.copy_config": "复制配置",
        "connect.open_doc": "打开 MCP.md",
        "connect.key": "2）直连 API key（可选）",
        "connect.key_hint": "key 只放环境变量，不要写进仓库、文档或对话。"
                            "WinMon 不保存 key 内容，只检查它在不在。",
        "connect.key_short": "可选。先选直连对象：跟随当前 AI，或手动指定一个；"
                             "不选就不会写任何 key，也不会自己换模型。",
        "connect.key_target": "直连对象：",
        "connect.key_target_follow": "跟随当前 AI",
        "connect.key_warning": "不要写进仓库、文档或对话；WinMon 不保存 key 内容。",
        "connect.show_key_help": "为什么需要 key？",
        "connect.key_envs": "智谱 WINMON_AI_API_KEY · OpenAI WINMON_OPENAI_API_KEY · "
                            "Claude WINMON_ANTHROPIC_API_KEY · DeepSeek WINMON_DEEPSEEK_API_KEY",
        "connect.key_temp": "只用于本次运行（不写盘）：",
        "connect.key_use": "用于本次运行",
        "connect.key_saved": "已在本进程设置 {env}（不写盘；重启后需重设或改用环境变量）。",
        "connect.key_empty": "key 为空，没有改动。",
        "connect.workspace": "3）给新项目单独建一个工作区（可选）",
        "connect.show_workspace": "为什么要单独建工作区？",
        "connect.workspace_hint": "不要把新项目和旧项目的对话混在一起：AI 会拿旧上下文"
                                  "回答新问题，排查时很难分清是哪一件事。WinMon 只记录路径，"
                                  "不会自动创建或移动文件。",
        "connect.workspace_none": "（未设置）",
        "connect.workspace_pick": "选择文件夹…",
        "connect.workspace_open": "打开",
        "connect.workspace_copy": "复制建议",
        "connect.behavior": "4）AI 行为",
        "connect.persona": "人设：",
        "connect.persona_hint": "人设只写语气和边界，台词由 AI 自由发挥；"
                                "写代码/开会/深夜时它会安静。",
        "connect.persona_short": "默认关心型；台词由 AI 自由发挥，人设只定语气和边界。",
        "connect.show_persona": "人设怎么写？支持什么格式？",
        "connect.persona_copy": "复制人设",
        "connect.persona_import": "导入人设…",
        "connect.persona_more": "更多…",
        "connect.persona_more_hint": "从 GitHub 等社区导入 .md / .json 后，"
                                     "可以命名、保存、删除，并在下拉框里直接选。",
        "persona.title": "人设库",
        "persona.hint": "从 GitHub 等社区导入 .md / .txt / .json；读取后可以改名、"
                        "使用或删除。文件只在本机读取，不会联网或执行。",
        "persona.import": "导入并保存…",
        "persona.rename": "重命名…",
        "persona.delete": "删除",
        "persona.use": "使用这条",
        "persona.close": "关闭",
        "persona.empty": "还没有保存的人设。点「导入并保存…」从社区角色卡导入。",
        "persona.select": "先在列表里选一条人设。",
        "persona.imported": "已导入「{name}」；点「重命名…」可以改名字。",
        "persona.renamed": "已改名为「{name}」。",
        "persona.deleted": "已删除「{name}」。",
        "persona.confirm_delete": "删除人设「{name}」？\n\n文字会从本机设置里移除，"
                                  "原始文件不受影响。",
        "persona.name_prompt": "给人设起一个名字（最多 40 字）：",
        "persona.name_empty": "名称不能为空。",
        "persona.char_count": "{n} 字",
        "persona.current": "当前使用：{name}",
        "persona.preview": "内容预览",
        "persona.source": "来源：{path}",
        "connect.persona_support": "支持 .txt / .md / .json（UTF-8，≤4000 字）；"
                                   "GitHub 角色卡会自动读名称和正文。",
        "connect.persona_none": "自定义：未导入（会按关心型兜底）",
        "connect.persona_empty": "文件里没有可用文字。",
        "connect.persona_too_long": "人设太长（上限 4000 字）。",
        "connect.persona_bad": "读不了这个人设文件：{err}",
        "connect.read_media": "读取媒体应用名",
        "connect.read_game": "读取游戏应用名",
        "connect.read_web": "读取浏览器网页标题",
        "connect.show_facts": "AI 会看到哪些数据？",
        "connect.facts_hint": "只给应用名；浏览器才给网页标题。不截屏、不读聊天/文件；"
                              "歌曲名和歌手不会交给 AI。",
        "connect.show_advanced": "主动 AI 的额外动作（会花 token）",
        "connect.proactive": "5）主动搭子（C，默认关）",
        "connect.proactive_enable": "允许 WinMon 主动说一句（会花 token）",
        "connect.proactive_hint": "默认关；只在「主动 AI」且当前 AI 有 key 时生效。"
                                  "专注/会议/夜深默认不允许，由你逐项打开。",
        "connect.proactive_short": "默认关。只有你打开、且当前 AI 有 key 时，才可能主动说一句。",
        "connect.show_proactive": "调整场景、频率与上限",
        "connect.proactive_focus": "专注时也允许",
        "connect.proactive_meeting": "会议中也允许",
        "connect.proactive_night": "深夜也允许",
        "connect.proactive_max": "每天最多",
        "connect.proactive_interval": "最短间隔",
        "connect.unit_per_day": "句",
        "connect.unit_min": "分钟",
        "connect.break": "把休息时段提供给 AI（由 AI 决定说不说）",
        "connect.watch": "值守（仅主动 AI：前台变化时检查，会花 token）",
        "connect.explain": "出问题时自动解释（仅主动 AI）",
        "connect.close": "关闭",
        "connect.copied": "已复制",
        "connect.status": "接入状态",
        "chat.title": "AI 对话记录（本机留痕）",
        "chat.empty": "还没有记录。",
        "chat.copy": "复制全部",
        "chat.close": "关闭",
        "common.ok": "确定",
        "common.cancel": "取消",
        "common.save": "保存",
    },
    "en": {
        "app.title": "WinMon {ver}",
        "settings.title": "WinMon Settings",
        "settings.language": "Language / 语言",
        "settings.language_hint": "Applies to the main window and AI windows immediately; "
                                  "details are in the Guide tab.",
        "settings.theme": "Color",
        "settings.theme_hint": "Light is white with dark text; Dark is black with white text; "
                               "Follow system reads the Windows light/dark setting. Applies "
                               "immediately; colored highlight cards stay visible.",
        "assistant.title": "WinMon AI Assistant",
        "assistant.header_reading": "Detecting the AI you are using…",
        "assistant.time_reading": "Reading local time…",
        "assistant.mode": "Mode:",
        "assistant.ask_hint": "Ask something, or click “Send to AI”…",
        "assistant.send": "Send to AI",
        "assistant.save": "Save reply",
        "assistant.connect": "Connect AI…",
        "assistant.quip": "Say something",
        "assistant.history": "History…",
        "assistant.usage": "Usage…",
        "assistant.empty": "No records yet. “Send to AI” copies the current desktop "
                            "context to the AI you are already using.",
        "assistant.reminder_owner": "Reminders come from the connected AI in its own chat; "
                                     "WinMon does not pop them up itself.",
        "connect.title": "Connect an AI",
        "connect.head": "Connect your AI",
        "connect.mcp": "1) MCP connection (recommended, read-only)",
        "connect.mcp_hint": "Add this to the client's MCP config and restart it. "
                            "WinMon's AI card will then show “MCP connected (client)”.",
        "connect.mcp_short": "Let an MCP-capable desktop client (e.g. Claude Desktop) read "
                             "WinMon directly; read-only, nothing is changed.",
        "connect.show_steps": "Show config and steps",
        "connect.copy_config": "Copy config",
        "connect.open_doc": "Open MCP.md",
        "connect.key": "2) Direct API key (optional)",
        "connect.key_hint": "Keep the key in an environment variable only. Never put it in "
                            "a repo, document, or chat. WinMon checks presence, not content.",
        "connect.key_short": "Optional. Pick a direct target first: follow the current "
                             "AI, or choose one manually. Nothing is written until you do.",
        "connect.key_target": "Direct target:",
        "connect.key_target_follow": "Follow current AI",
        "connect.key_warning": "Never put it in a repo, document, or chat. WinMon does not "
                               "save the key contents.",
        "connect.show_key_help": "Why do I need a key?",
        "connect.key_envs": "Zhipu WINMON_AI_API_KEY · OpenAI WINMON_OPENAI_API_KEY · "
                            "Claude WINMON_ANTHROPIC_API_KEY · DeepSeek WINMON_DEEPSEEK_API_KEY",
        "connect.key_temp": "This run only (not saved):",
        "connect.key_use": "Use this run",
        "connect.key_saved": "{env} is set for this process (not saved to disk).",
        "connect.key_empty": "The key is empty; nothing changed.",
        "connect.workspace": "3) Give each new project its own workspace (optional)",
        "connect.show_workspace": "Why a separate workspace?",
        "connect.workspace_hint": "Do not mix a new project with old chats: the AI may answer "
                                  "with stale context. WinMon only records the path and never "
                                  "creates or moves files.",
        "connect.workspace_none": "(not set)",
        "connect.workspace_pick": "Choose folder…",
        "connect.workspace_open": "Open",
        "connect.workspace_copy": "Copy advice",
        "connect.behavior": "4) AI behaviour",
        "connect.persona": "Persona:",
        "connect.persona_hint": "The persona sets tone and boundaries; the AI chooses the "
                                "words. It stays quiet while you code, meet, or work late.",
        "connect.persona_short": "Caring is the default; the AI chooses the words, the "
                                 "persona only sets tone and boundaries.",
        "connect.show_persona": "How do I write a persona? Which formats work?",
        "connect.persona_copy": "Copy persona",
        "connect.persona_import": "Import persona…",
        "connect.persona_more": "More…",
        "connect.persona_more_hint": "Import .md / .json character cards from GitHub; "
                                     "name, save, delete, and pick them in the dropdown.",
        "persona.title": "Persona library",
        "persona.hint": "Import .md / .txt / .json character cards from GitHub. After "
                        "reading one, you can rename, use, or delete it. Files are read "
                        "locally; nothing is uploaded or executed.",
        "persona.import": "Import & save…",
        "persona.rename": "Rename…",
        "persona.delete": "Delete",
        "persona.use": "Use this one",
        "persona.close": "Close",
        "persona.empty": "No saved personas yet. Click “Import & save…” to add a community "
                         "character card.",
        "persona.select": "Select a persona first.",
        "persona.imported": "Imported “{name}”; click “Rename…” to change the name.",
        "persona.renamed": "Renamed to “{name}”.",
        "persona.deleted": "Deleted “{name}”.",
        "persona.confirm_delete": "Delete persona “{name}”?\n\nThe text is removed from "
                                  "local settings; the original file is untouched.",
        "persona.name_prompt": "Name this persona (max 40 characters):",
        "persona.name_empty": "The name cannot be empty.",
        "persona.char_count": "{n} chars",
        "persona.current": "In use: {name}",
        "persona.preview": "Preview",
        "persona.source": "Source: {path}",
        "connect.persona_support": "Supports .txt / .md / .json (UTF-8, max 4000 chars); "
                                   "GitHub character cards keep their name and body.",
        "connect.persona_none": "Custom: not imported (caring fallback)",
        "connect.persona_empty": "The file has no usable text.",
        "connect.persona_too_long": "Persona is too long (max 4000 chars).",
        "connect.persona_bad": "Cannot read this persona file: {err}",
        "connect.read_media": "Read media app name",
        "connect.read_game": "Read game app name",
        "connect.read_web": "Read browser page title",
        "connect.show_facts": "What data will the AI see?",
        "connect.facts_hint": "App names only; only browsers share a page title. No "
                              "screenshots, chats, or files; track and artist names are "
                              "never given to the AI.",
        "connect.show_advanced": "Extra Active AI actions (cost tokens)",
        "connect.proactive": "5) Proactive companion (C, off by default)",
        "connect.proactive_enable": "Allow WinMon to say one line on its own (costs tokens)",
        "connect.proactive_hint": "Off by default; only when Active AI is selected and the "
                                  "current AI has a key. Focus/meeting/late-night are denied "
                                  "by default; enable each explicitly.",
        "connect.proactive_short": "Off by default. It can speak only after you enable it "
                                    "and the current AI has a key.",
        "connect.show_proactive": "Adjust contexts, frequency, and limits",
        "connect.proactive_focus": "Allow while focusing",
        "connect.proactive_meeting": "Allow in meetings",
        "connect.proactive_night": "Allow late at night",
        "connect.proactive_max": "Max per day",
        "connect.proactive_interval": "Min interval",
        "connect.unit_per_day": "lines",
        "connect.unit_min": "min",
        "connect.break": "Give the AI your break schedule (it decides whether to mention it)",
        "connect.watch": "Watch (Active AI only: check on foreground change; costs tokens)",
        "connect.explain": "Auto-explain problems (Active AI only)",
        "connect.close": "Close",
        "connect.copied": "Copied",
        "connect.status": "Connection status",
        "chat.title": "AI chat log (local only)",
        "chat.empty": "No records yet.",
        "chat.copy": "Copy all",
        "chat.close": "Close",
        "common.ok": "OK",
        "common.cancel": "Cancel",
        "common.save": "Save",
    },
}

#: 静态控件中文原文 → English。只放 WinMon 自己写死的界面文案。
_STATIC_EN = {
    "　（本机托盘不可用，选「隐藏到托盘」会自动退回结束进程）":
        "  (Tray unavailable; “Hide to tray” falls back to exiting.)",
    "　（托盘不可用时这一项不生效，界面照常显示）":
        "  (Ignored when the tray is unavailable; the window still shows.)",
    "　把当前窗口的位置与最小化状态存下来，之后一键切回去":
        "  Save window positions and minimized state, then restore in one click.",
    "（勾了才替你启动；没有媒体会话时改用媒体键）":
        "(Starts it if checked; media keys as fallback)",
    "（未选择程序）": "(No program selected)",
    "（长期用同名环境变量）": "(Use the same environment variable long-term)",
    "☆ 收藏": "☆ Favorite",
    "★ 已收藏": "★ Favorited",
    "AI 识别与用量（真实 / 精确 / 估算分栏）":
        "AI identity and usage (provider / tokenizer / estimated)",
    "AI 助手…": "AI assist",
    "接入…": "Connect…",
    "WinMon 设置": "WinMon Settings",
    "常规": "General",
    "AI 用量": "AI usage",
    "使用说明": "Guide",
    "按时间提醒（24 小时制）": "Time-based reminders (24h)",
    "保存 (Enter)": "Save (Enter)",
    "保存 AI 回复": "Save AI reply",
    "保存当前布局…": "Save layout…",
    "保存布局…": "Save layout…",
    "存一套窗口布局，之后一键切回":
        "Save a window layout and restore it in one click",
    "应用时最小化其它窗口（不碰受保护进程）":
        "Minimize other windows on apply (protected processes stay)",
    "更新": "Update",
    "保存时间": "Saved at",
    "播放/暂停": "Play/Pause",
    "操作": "Actions",
    "总览": "Overview",
    "工具": "Tools",
    "监控": "Monitor",
    "工作台": "Workbench",
    "AI 工作区": "AI workspace",
    "AI 工作区…": "AI workspace…",
    "清理程序文件…": "Clean files…",
    "操作历史（留痕：谁在什么时候被关闭 / 结束了）":
        "Action history (who was closed or terminated, and when)",
    "操作历史…": "History…",
    "操作历史日志保留：": "Keep action history for:",
    "程序": "Program",
    "程序名称": "Program",
    "程序目录": "Program catalog",
    "出问题时自动解释": "Auto-explain problems",
    "窗口": "Window",
    "窗口标题": "Window title",
    "窗口数": "Windows",
    "打开": "Open",
    "打开日志文件夹": "Open log folder",
    "打开数据目录": "Open data folder",
    "单击：选中并预览　双击：最小化/还原　":
        "Click: select and preview   Double-click: minimize/restore   ",
    "单击：选中并预览　双击：最小化/还原　Enter/中键：切到最前　悬停看详情　右键：更多":
        "Click: select · Dbl: min/restore · Enter: front · Right-click: menu",
    "导出 CSV…": "Export CSV…",
    "导出 txt…": "Export txt…",
    "等待画面…": "Waiting for frames…",
    "点右上角的 × 时：": "When clicking ×:",
    "定位": "Locate",
    "发送": "Send",
    "分析当前窗口": "Analyze window",
    "复制路径": "Copy path",
    "复制全部": "Copy all",
    "复制现场给 AI": "Copy context to AI",
    "跟随选择": "Follow selection",
    "工作区": "Workspace",
    "工作区快照": "Workspace snapshots",
    "关闭": "Close",
    "关闭窗口": "Close window",
    "关闭选中的 (Enter)": "Close selected (Enter)",
    "请求关闭": "Request close",
    "请求关闭（先保存再退出）": "Request close (save first)",
    "强制结束": "Force end",
    "强制结束（未保存会丢）": "Force end (unsaved work is lost)",
    "强制结束整树": "Force-end tree",
    "刷新速度：": "Refresh:",
    "媒体": "Media",
    "恢复 AI 接入": "Resume AI access",
    "恢复预算只能在这里操作；MCP 没有改预算的工具。":
        "Budget can only be resumed here; MCP cannot change it.",
    "活动": "Act",
    "继续刷新": "Resume",
    "加入总览": "Add view",
    "监视当前窗口": "Watch win",
    "接入说明": "Connection help",
    "结束进程": "End process",
    "结束进程（真的退出，托盘图标一起清掉）":
        "Exit (really quit and remove the tray icon)",
    "结束进程树": "End process tree",
    "结束整树": "End tree",
    "结束选中进程及子树": "End subtree",
    "仅可启动": "Launchable only",
    "仅显示有窗口": "Windowed only",
    "进程（父 → 子）": "Process (parent → child)",
    "进程树": "Process tree",
    "开机自启动（登录后自动运行 WinMon）":
        "Start with Windows (run WinMon at sign-in)",
    "可多选（不勾 = 自动跟随正在放的那个）":
        "Multi-select (none = follow the active player)",
    "快捷键：Space / Enter = 关闭视频　　Esc = 继续播放　（鼠标点按钮同样有效）":
        "Space/Enter = close video, Esc = keep playing (buttons work too)",
    "来源": "Source",
    "类别": "Category",
    "类别：全部": "Category: All",
    "路径 / 目标": "Path / target",
    "媒体：": "Media:",
    "播放器：": "Player:",
    "模式：": "Mode:",
    "默认只勾选「删了会自动重建 / 本来就没用」的项；":
        "Only safe-to-delete items are checked by default;",
    "内存 工作/私有": "Memory",
    "内存(工作集)": "Memory",
    "判定依据：窗口不在前台、CPU 几乎为 0。\n":
        "Reason: not foreground and near-zero CPU.\n",
    "启动时直接收进托盘（界面不弹出来，双击托盘图标再看）":
        "Start hidden in tray (double-click the tray icon to show)",
    "切到前台": "Bring to front",
    "清空列表": "Clear list",
    "清空日志文件…": "Clear log file…",
    "清理选中 (Enter)": "Clean selected (Enter)",
    "取消": "Cancel",
    "取消 (Esc)": "Cancel (Esc)",
    "全不选": "Select none",
    "全选": "Select all",
    "确定": "OK",
    "确定 (Enter)": "OK (Enter)",
    "删除": "Delete",
    "上限 ×2": "Limit ×2",
    "稍后再说 (Esc)": "Later (Esc)",
    "设为类别…": "Set category…",
    "设置…": "Settings…",
    "实时预览": "Live preview",
    "实时总览": "Live overview",
    "视频联动：": "Video link:",
    "刷新": "Refresh",
    "刷新列表": "Refresh",
    "私有(任务管理器)": "Private",
    "搜索：": "Search:",
    "搜索程序：": "Find programs:",
    "提醒间隔": "Reminder interval",
    "停止监视": "Stop",
    "图标": "Img",
    "闲置超时提醒": "Idle alert",
    "闲置清单": "Idle list",
    "显示无窗口进程": "Show windowless processes",
    "显示隐藏窗口": "Show hidden windows",
    "移除选中总览": "Remove",
    "移除总览": "Remove",
    "以管理员打开": "Open as administrator",
    "以后到点自动关闭（不再询问）——只优雅关闭，不强杀":
        "Auto-close when due (graceful only, never force-kill)",
    "以下都是 WinMon 自己产生的文件，勾选后点「清理选中」":
        "These files are created by WinMon; check items and click Clean selected",
    "隐藏到托盘（推荐：音乐继续放、监控继续跑）":
        "Hide to tray (music and monitoring keep running)",
    "应用": "Apply",
    "应用 (Enter)": "Apply (Enter)",
    "应用时顺带最小化其它窗口（保护进程与 WinMon 自己不动）":
        "Also minimize other windows (protected processes and WinMon stay)",
    "用当前布局更新": "Update with current layout",
    "用于本次运行": "Use run",
    "暂停 AI 接入": "Pause AI access",
    "暂停刷新": "Pause",
    "正在重新扫描…": "Rescanning…",
    "直接关闭视频": "Close video",
    "直连分析": "Direct analysis",
    "值守（前台变化时检查）": "Watch (check on foreground change)",
    "只看程序：": "Program:",
    "只看动作：": "Action:",
    "只看收藏": "Favorites only",
    "只看最近使用": "Recent only",
    "重新扫描": "Rescan",
    "重置今日用量": "Reset today's usage",
    "状态": "State",
    "自动分类": "Auto-categorize",
    "自动启动": "Auto-start",
    "总览实时刷新": "Live overview refresh",
    "最近使用…": "Recent…",
    "最小化选中的": "Minimize selected",
    # 新增窗口里用到的短句
    "接入与建议": "Setup & advice",
    "AI 对话记录": "AI chat log",
    "复制配置": "Copy config",
    "打开 MCP.md": "Open MCP.md",
    "选择文件夹…": "Choose folder…",
    "复制建议": "Copy advice",
    "允许接入的 AI 提醒休息（按本地 24 小时时段）":
        "Allow the connected AI to suggest breaks (local 24h slots)",
    "值守（仅主动 AI：前台变化时检查，会花 token）":
        "Watch (Active AI only: check on foreground change; costs tokens)",
    "出问题时自动解释（仅主动 AI）": "Auto-explain problems (Active AI only)",
    "只用于本次运行（不写盘）：": "This run only (not saved):",
}

#: English → 中文（切回中文时用；重复的英文取第一条）
_STATIC_ZH = {}
for _zh, _en in _STATIC_EN.items():
    _STATIC_ZH.setdefault(_en, _zh)


def normalize_language(code) -> str:
    """只认 zh / en；其它值一律回默认中文。"""
    value = str(code or "").strip().lower()
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def get_language() -> str:
    return _current


def set_language(code) -> str:
    """设置当前语言并返回规范化后的代码（不写文件）。"""
    global _current
    _current = normalize_language(code)
    return _current


def language_label(code) -> str:
    return LANGUAGES.get(normalize_language(code), LANGUAGES[DEFAULT_LANGUAGE])


def load_from_settings(path: str) -> str:
    """从 winmon_settings.json 读 `language`；文件坏了就保持默认，不抛异常。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return set_language(DEFAULT_LANGUAGE)
    if not isinstance(data, dict):
        return set_language(DEFAULT_LANGUAGE)
    return set_language(data.get("language"))


def t(key: str, **kw) -> str:
    """取一句带 key 的文案；英文缺失时回中文，格式化失败就返回原文。"""
    table = _STRINGS.get(_current) or _STRINGS["zh"]
    text = table.get(key)
    if text is None:
        text = _STRINGS["zh"].get(key, key)
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        return text


def pick(zh: str, en: str = "") -> str:
    """就地二选一：英文为空时永远回中文。"""
    return en if (_current == "en" and en) else zh


def translate_text(text: str) -> str:
    """把 WinMon 写死的中文界面文案换成当前语言；不在表里就原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    if _current == "en":
        return _STATIC_EN.get(text, text)
    return _STATIC_ZH.get(text, text)


def translate_menu(menu) -> None:
    """翻译一个 tk.Menu 的所有命令项（含级联子菜单）。"""
    try:
        end = menu.index("end")
    except Exception:
        return
    if end is None:
        return
    for i in range(int(end) + 1):
        try:
            kind = menu.type(i)
        except Exception:
            continue
        if kind == "separator":
            continue
        try:
            label = menu.entrycget(i, "label")
        except Exception:
            label = ""
        if label:
            new = translate_text(label)
            if new != label:
                try:
                    menu.entryconfigure(i, label=new)
                except Exception:
                    pass
        if kind == "cascade":
            try:
                sub = menu.nametowidget(menu.entrycget(i, "menu"))
            except Exception:
                continue
            translate_menu(sub)


def translate_widgets(root) -> int:
    """
    递归翻译一棵控件树里的静态 `text`（以及 Notebook 标签、Treeview 表头）。

    返回改动的控件数，方便测试断言"确实翻到了东西"。只碰 `text` 选项，
    不碰 `textvariable`、输入框内容、列表数据与用户文本。
    """
    import tkinter as tk
    from tkinter import ttk

    changed = 0
    stack = [root]
    while stack:
        widget = stack.pop()
        try:
            stack.extend(widget.winfo_children())
        except Exception:
            pass
        if isinstance(widget, tk.Menu):
            translate_menu(widget)
            continue
        try:
            text = widget.cget("text")
        except Exception:
            text = ""
        if isinstance(text, str) and text:
            new = translate_text(text)
            if new != text:
                try:
                    widget.configure(text=new)
                    changed += 1
                except Exception:
                    pass
                # 英文通常比中文长；固定 width 的按钮不改宽就会被裁成
                # "Add to over" / "Remove from ov"（实测截图里真出现过）。
                if isinstance(widget, (tk.Button, ttk.Button)):
                    try:
                        old_w = int(widget.cget("width") or 0)
                    except Exception:
                        old_w = 0
                    if old_w and len(new) > old_w:
                        try:
                            widget.configure(width=min(22, len(new) + 1))
                        except Exception:
                            pass
        if isinstance(widget, ttk.Notebook):
            try:
                for i in range(widget.index("end")):
                    old = widget.tab(i, "text")
                    new = translate_text(old)
                    if new != old:
                        widget.tab(i, text=new)
                        changed += 1
            except Exception:
                pass
        if isinstance(widget, ttk.Treeview):
            try:
                # `#0`（图标列）也有表头，之前漏了，英文界面里会留下“图标”
                try:
                    old0 = str(widget.heading("#0").get("text") or "")
                except Exception:
                    old0 = ""
                new0 = translate_text(old0)
                if old0 and new0 != old0:
                    widget.heading("#0", text=new0)
                    changed += 1
                for col in widget["columns"]:
                    heading = widget.heading(col)
                    old = str(heading.get("text") or "")
                    new = translate_text(old)
                    if old and new != old:
                        widget.heading(col, text=new)
                        changed += 1
            except Exception:
                pass
    return changed
