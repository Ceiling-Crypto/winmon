# 桌宠形象来源与授权

- **形象**：DeepSeek 社区二创「鲸鱼娘 · 大肥鱼」（蓝发、女仆装、鲸鱼尾）。
- **来源仓库**：[RYANFFY/deepseek-dafeiyu-pet](https://github.com/RYANFFY/deepseek-dafeiyu-pet)
- **上游来源**：该仓库在 [1190fasheqi/dafeiyu-pet](https://github.com/1190fasheqi/dafeiyu-pet)
  的桌宠基础上整理；本目录只取 `sprites/正面*、侧面*、背面*` 与
  `assets/small-whale.png`（头像）。
- **授权**：仓库以 **MIT License** 发布（全文见同目录 `LICENSE`）。

## 本仓库做了什么改动

1. 只保留 187 / 238 / 306 三个高度的正面、侧面、背面；
2. 保留**二值 alpha**（>=128 不透明，其余透明）；这套图是硬边像素画，
   二值化让闭眼帧/镜像帧与基础帧的轮廓完全一致，GDI+ 分层窗口直接按
   alpha 合成，不会出现底色描边；
3. 额外生成 `side_left.png`（侧面水平镜像），用于向右走；
   `build_assets.py` 会从 `side.png` 重新生成它，避免两张图被误改成同一张；
4. 2026-10-22 用 `build_assets.py` 从同一套 MIT 原图生成闭眼帧
   `front_blink.png` / `side_blink.png` / `side_left_blink.png`：在眼睛位置
   覆一层与眼周肤色一致的椭圆，再画下垂睫毛线；没有重新绘制角色。
   `build_assets.py` 只是开发工具（需要 Pillow），**运行时不依赖 Pillow**；
5. `avatar.png` 由 `small-whale.png` 裁切/缩放到 160px 并做同样的 alpha 二值化。

原始 PNG 没有提交，避免仓库里同时放两套图；需要原图或其它尺寸请回来源仓库。
闭眼帧/镜像帧都可以用 `python "desktop pet/build_assets.py"` 重新生成。
运行时的透明与轮廓由 `pet_layered.py` 的 GDI+ 分层表面直接处理，不生成/读取
额外的轮廓文件。

## 动作片段（2026-10-23 起）

- **来源**：[PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)，
  作者 PC2005-cloud。
- **条款**：其 README 写明「素材（动画/提示词/源视频）允许开源使用，禁止商用」；
  二创作品须附原作者 GitHub 地址。
- **本仓库的用法**：已选定 21 段常用动作，接入后裁切角色、降采样、透明化并打包；
  **仅用于免费、非商业的 WinMon**，不单独售卖，也不放进付费产品。
  截至阶段 8.37，动作播放器尚未接入，这些片段还没有随包分发。
- **署名**：介绍、展示、分发含这些动作的版本时，请保留
  <https://github.com/PC2005-cloud/dsh-pet>。
- **完整说明**：见仓库根目录 [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。

## 重要说明

「大肥鱼 / 鲸鱼娘」是 **DeepSeek 社区的二创形象**，不是 DeepSeek 官方品牌形象。
WinMon 与 DeepSeek 官方没有隶属或背书关系；这里按 MIT 协议使用社区素材，
并在 `README.md` / `HANDOFF.md` 中保留出处。
