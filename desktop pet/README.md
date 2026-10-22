# desktop pet —— WinMon 的桌宠目录

用户定的规矩：**桌宠相关文件全部放这里，后续新增的也一样**。

```text
desktop pet/
├─ winmon_pet.py         桌宠窗口 / 动画 / 动作 / 粒子 / 拖动 / 点击；不 import winmon.py
├─ pet_layered.py        GDI+ 逐像素 alpha 分层表面 + Tk 重绘抑制（Windows）
├─ build_assets.py       开发者工具：用 Pillow 从基础三视图生成镜像/闭眼帧
├─ assets/               187 / 238 / 306 三档精灵（正/侧/左/背 + 闭眼）+ 头像
├─ docs/                 桌宠、气泡、独立设置面板截图
├─ LICENSE               素材的 MIT 许可证（来自社区仓库）
├─ NOTICE.md             素材出处、改动与“非官方”说明
├─ AGENT.md              本地行为层 / 动作包 / Agent 的边界与设计
└─ README.md             本文件
```

主程序 `winmon.py` 把本目录加进 `sys.path` 后 `import winmon_pet`；
`winmon_pet.project_dir()` 就是本目录，所以素材路径是 `assets/`，不再散落在根目录。

## 现在的行为（2026-10-22）

- **约 60 FPS 本地动画**：呼吸、眨眼、左右看、小跳、散步；不是一张静止图。
  点击/动作在事件里立即提交一帧，不等下一个 tick。
- **二维散步**：在工作区内挑目标点，横走用侧面、向上用背面、向下用正面；
  向左/向右用真正的镜像图，不会倒着走。
- **音乐 / 视频**：主程序把媒体粗分类后告诉桌宠；音乐时正面晃、吐泡泡/音符，
  视频时正面“认真看”，偶尔闪一下。
- **没人理**：系统键鼠闲置超过 150 秒且没有媒体时，闭眼睡觉、冒 Zzz；
  动一下鼠标或点它就会醒。
- **窗口**：去掉 DWM 边框/阴影/圆角（Win11 圆角要在 `FRAMECHANGED` 之后再关，
  否则会被 DWM 重新套上）；画面用 GDI+ 32 位预乘 alpha 画进
  `UpdateLayeredWindow` 的分层表面。没有颜色键、没有 `SetWindowRgn`、
  没有 DWM 阴影；透明像素点击穿透，只有身体能点。参考 dsh-pet 的
  `transparent + frame:false + hasShadow:false` 小窗设计。
- **右键菜单**：每次右键重建（不会第二次没反应）；除打开主界面、动作抽屉、
  设置/隐藏/退出外，还有当前歌名 + 上一首 / 播放暂停 / 下一首。
- **动作抽屉**：右键或气泡「动作…」打开，收录打招呼、跳、散步、左右看、
  转圈、伸懒腰、吐泡泡、睡觉、叫醒；点一下执行并收起。
- **独立设置**：右键 →「桌宠设置…」打开不依赖主窗口的小面板；主窗口在托盘里
  也能改大小/活泼度/归位。面板里「完整设置…」会打开独立显示的完整设置窗。

## 重新生成素材

```powershell
# 需要 Pillow；只改 assets，不改运行时依赖
python "desktop pet/build_assets.py"
```

## 测试

测试仍在根目录 `tests/`（它是项目的 QA，不是桌宠运行时文件）；测试通过
`tests/_helpers.py` 的 `PET_HOME` 找到本目录，B46/B48 覆盖窗口、动作和独立设置。

**授权**：形象是社区二创，MIT；不是 DeepSeek 官方品牌。详见 `NOTICE.md`。
