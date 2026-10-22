# 桌宠动作素材（21 段）

本目录是 **构建产物**，运行时由 `desktop pet/winmon_pet.py` 只读加载；
运行时不依赖 ffmpeg 或 Pillow。

## 来源与授权

- 源素材：[PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)
- 作者：PC2005-cloud
- 原作者 README 的素材条款：**允许开源使用，禁止商用**；二创作品须附作者
  GitHub 地址。
- WinMon 只选取 21 段常用动作，裁切角色、降采样到 10 FPS / 300px 高、
  96 色调色板透明 PNG；**仅用于免费、非商业的 WinMon**。
- 完整条款见仓库根目录 [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md)。

## 目录

```
actions/
├─ manifest.json          # 帧数 / 条带 / fps / 分组
├─ idle/strip_00.png ...  # 每 10 帧横向拼成一条
├─ wave/...
└─ ...
```

## 重新构建

```powershell
py -3.12 "desktop pet/build_actions.py" `
  --source "C:\path\to\big-blue-fish\assets\fish" `
  --ffmpeg "C:\path\to\ffmpeg.exe"
```

`--only idle,wave` 可只重建几段。构建脚本需要 Pillow 和带
`libvpx-vp9` 的 ffmpeg；这两者都**不进运行时**。

## 运行时约定

- `manifest.json` 里的 `strips` 路径使用 `/`；Windows 上由运行时转换。
- 每段动作的 `loop` 决定循环还是一次性；`idle` / `sleep` / `think` /
  `busy` / `tidy` / `talk` / 两段舞蹈为循环。
- 动作帧按桌宠当前高度缩放（小 187 / 中 238 / 大 306），底边对齐。
