# 桌宠形象来源与授权

- **形象**：DeepSeek 社区二创「鲸鱼娘 · 大肥鱼」（蓝发、女仆装、鲸鱼尾）。
- **来源仓库**：[RYANFFY/deepseek-dafeiyu-pet](https://github.com/RYANFFY/deepseek-dafeiyu-pet)
- **上游来源**：该仓库在 [1190fasheqi/dafeiyu-pet](https://github.com/1190fasheqi/dafeiyu-pet)
  的桌宠基础上整理；本目录只取 `sprites/正面*、侧面*、背面*` 与
  `assets/small-whale.png`（头像）。
- **授权**：仓库以 **MIT License** 发布（全文见同目录 `LICENSE`）。

## 本仓库做了什么改动

1. 只保留 187 / 238 / 306 三个高度的正面、侧面、背面；
2. 把半透明 alpha **二值化**（>=128 不透明，其余透明），因为 Tk 的
   `-transparentcolor` 是颜色键而不是逐像素 alpha；不做这一步会在边缘出现
   底色描边；
3. 额外生成 `side_left.png`（侧面水平镜像），用于向左走；
4. `avatar.png` 由 `small-whale.png` 裁切/缩放到 160px 并做同样的 alpha 二值化。

原始 PNG 没有提交，避免仓库里同时放两套图；需要原图或其它尺寸请回来源仓库。

## 重要说明

「大肥鱼 / 鲸鱼娘」是 **DeepSeek 社区的二创形象**，不是 DeepSeek 官方品牌形象。
WinMon 与 DeepSeek 官方没有隶属或背书关系；这里按 MIT 协议使用社区素材，
并在 `README.md` / `HANDOFF.md` 中保留出处。
