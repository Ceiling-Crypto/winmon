# desktop pet —— WinMon 的桌宠目录

用户定的规矩：**桌宠相关文件全部放这里，后续新增的也一样**。

```text
desktop pet/
├─ winmon_pet.py         桌宠窗口 / 动画 / 拖动 / 点击；不 import winmon.py
├─ assets/               187 / 238 / 306 三档精灵 + 头像
├─ docs/                 桌宠与设置页截图
├─ LICENSE               素材的 MIT 许可证（来自社区仓库）
├─ NOTICE.md             素材出处、改动与“非官方”说明
├─ AGENT.md              桌宠 Agent / 动作包 / 全屏漫游的设计稿（尚未实现）
└─ README.md             本文件
```

主程序 `winmon.py` 把本目录加进 `sys.path` 后 `import winmon_pet`；
`winmon_pet.project_dir()` 就是本目录，所以素材路径是 `assets/`，不再散落在根目录。

测试仍在根目录 `tests/`（它是项目的 QA，不是桌宠运行时文件）；测试通过
`tests/_helpers.py` 的 `PET_HOME` 找到本目录。

**授权**：形象是社区二创，MIT；不是 DeepSeek 官方品牌。详见 `NOTICE.md`。
