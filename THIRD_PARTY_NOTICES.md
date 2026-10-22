# 第三方声明

winmon1.0 的**代码**以 MIT 发布，完整条款见 [`LICENSE`](LICENSE)。
下面这些素材/项目**不适用** MIT，或需要单独署名；分发、改版、商用前请先看这一页。

## 1. 桌宠形象（精灵图）

- **形象**：DeepSeek 社区二创「鲸鱼娘 · 大肥鱼」（蓝发、女仆装、鲸鱼尾）。
- **来源**：[RYANFFY/deepseek-dafeiyu-pet](https://github.com/RYANFFY/deepseek-dafeiyu-pet)
- **上游**：该仓库在 [1190fasheqi/dafeiyu-pet](https://github.com/1190fasheqi/dafeiyu-pet)
  的桌宠基础上整理。
- **许可**：**MIT License**，全文见 [`desktop pet/LICENSE`](<desktop pet/LICENSE>)。
- **本仓库的改动**：只保留 187 / 238 / 306 三个高度；二值 alpha；生成左向镜像；
  用 `build_assets.py` 从同一套 MIT 原图生成闭眼帧；头像裁切到 160px。
  具体见 [`desktop pet/NOTICE.md`](<desktop pet/NOTICE.md>)。

## 2. 桌宠动作片段（`desktop pet/actions/`）

- **来源**：[PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)
- **作者**：PC2005-cloud
- **素材条款**（摘自其 README）：
  - 代码：MIT；
  - 素材（动画 / 提示词 / 源视频）：**允许开源使用，禁止商用**；
  - 二创约定：基于本项目的衍生 / 改版 / 换皮作品，在任何介绍、展示、分发该作品的
    地方，须附上原作者 GitHub 地址。
- **WinMon 的用法**：已选定 21 段常用动作，接入后会裁切到角色、降采样到
  10–12 fps、透明化并打包成雪碧图；**仅用于免费、非商业的 WinMon**，
  不对这些动作单独收费，也不把它们打包进付费产品。截至阶段 8.37，
  动作播放器尚未接入，这些片段**还没有随包分发**。
- **署名**：任何介绍、展示、分发含这些动作的版本，请保留
  <https://github.com/PC2005-cloud/dsh-pet> 这个链接。

### 关于「Bai-Cute/big-blue-fish-desktop-pet 标了 MIT」

[Bai-Cute/big-blue-fish-desktop-pet](https://github.com/Bai-Cute/big-blue-fish-desktop-pet)
的 README 把 106 段动画描述为「来自 PC2005-cloud/dsh-pet，MIT 许可」。
那是**下游转述**；上游作者在 dsh-pet README 里对素材另有「禁止商用」条款。
两份说法冲突时，本项目按**上游作者**的条款执行，不把动画当 MIT 使用。

## 3. 非官方声明

「蓝色大肥鱼 / 鲸鱼娘」是 **DeepSeek 社区二创形象**，不是 DeepSeek 官方品牌形象。
WinMon 与 DeepSeek 官方、dsh-pet 作者均**没有隶属或背书关系**。
WinMon 的桌宠是一个轻量入口；完整的一百余段动作、换装和更多互动请使用 dsh-pet。

## 4. 侵权处理

如果你认为本仓库的某段素材侵犯了你的权利，请通过
[GitHub Issues](https://github.com/Ceiling-Crypto/winmon/issues)
（标题注明「版权」）或邮箱 **3688068026@qq.com** 联系。
我们会先下架相关素材，再核对授权。
