# AI 音轨分离

本地运行的 macOS 鼓轨、人声分离与鼓点 MIDI 桌面工具。界面可选择两个独立模块：

- 鼓轨分离：`HTDemucs FT` 生成 `drums.mp3` 和 `no_drums.mp3`，完成后可用 ADTOF 或 STRUM 生成 `drums.mid`。
- 人声分离：`MelBand RoFormer Kim` 生成 `vocals.mp3` 和 `instrumental.mp3`。

音频只保存在本机。安装版会在应用内启动一个随机端口的 `127.0.0.1` 服务，无需先手动打开网页或终端。

## 系统要求

- macOS 13 或更高版本
- Apple Silicon 发布版支持 M1/M2/M3/M4 系列
- 至少 16GB 内存
- 首次安装和首次下载模型时需要网络
- 建议预留 5GB 以上磁盘空间

源码运行模式会在项目的 `.runtime` 和 `.venv` 目录中安装 Python 3.11 与依赖，不修改系统 Python，也不需要 Homebrew、Docker 或 Redis。安装版已携带 Python、FFmpeg 和运行依赖。

## 安装版

1. 打开 `AI-Audio-Separator-macOS-arm64.dmg`。
2. 将“AI 音轨分离”拖入“应用程序”。
3. 在 Launchpad 或“应用程序”中直接打开。

桌面窗口内的下载按钮会打开 macOS 原生保存面板，可选择任意本地目录。任务数据和模型保存在：

```text
~/Library/Application Support/AI Audio Separator/
```

应用退出后，内置服务会一并关闭。已完成的任务、音频与模型缓存会保留，下次打开可继续使用。

## 启动

在 Finder 中双击 `run.command`，或在终端运行：

```bash
./run.command
```

启动完成后会打开独立桌面窗口。macOS 原生标题栏提供拖动、关闭、缩小和全屏按钮，窗口边缘可以调整尺寸。桌面窗口关闭后，本地服务和正在处理的任务会继续运行；再次双击 `run.command` 即可恢复界面。

需要使用普通浏览器时，也可以访问：<http://127.0.0.1:8765>

页面操作分为四步：

1. 点击“选择音频”。
2. 选择“分离鼓轨”或“分离人声”。
3. 点击上传按钮，等待分轨完成后试听或下载。
4. 鼓轨任务可继续选择“快速 · ADTOF”或“高精度 · STRUM”生成 MIDI。拍号默认自动推断，也可手动指定 2/4、3/4、4/4、6/8、9/8 或 12/8。页面会预览开头四个有鼓点的小节；小节线相位有偏差时，可前移或后移 1–2 拍并重新生成。

第一次启动会安装运行环境。首个鼓轨任务会下载 `htdemucs_ft`，首个人声任务会下载约 913MB 的 `MelBand RoFormer Kim` 权重并校验 SHA-256。第一次使用 ADTOF 会下载约 78MB 的 Beat This! 节拍模型；第一次使用 STRUM 会下载固定版本源码和约 1GB 的鼓专用权重。缓存完成后，后续任务可以离线运行。

## 使用限制

- 支持 WAV、MP3、FLAC、M4A、OGG。
- 单文件最大 500MB。
- 音频最长 15 分钟。
- 当前 Intel Mac 固定使用 CPU：鼓轨用 `htdemucs_ft`，人声用 `melband-roformer-kim-vocals`。
- 同一时间处理一个任务，其余任务进入队列。
- 输出为 44.1kHz、双声道、256 kbps MP3。5 分钟音频的每条结果通常约 10MB。
- ADTOF 快速模式识别底鼓 36、军鼓 38、闭镲 42、Low-Mid Tom 47、Crash 49，并用 Beat This! 检测拍点与重拍。
- STRUM 高精度模式复用已分离鼓轨，检测并区分底鼓 36、军鼓 38、闭镲 42、Floor Tom 43、Low-Mid Tom 47、高 Tom 50、Crash 49、Ride 51。STRUM 内部的 Clone Hero 结果会自动转换成标准 General MIDI 鼓组。
- STRUM 固定关闭音频首拍裁切，鼓点和原始音频保持同一绝对时间轴；小节偏移仍由本工具统一处理。
- 自动拍号会结合 Beat This! 输出的重拍间隔与鼓点三分律动，识别 3/4 和 6/8 等常用拍号。6/8、9/8、12/8 会使用附点四分音符 BPM，预览用每三个八分拍的辅助线显示复拍子分组。
- 手动切换拍号时，MIDI 会同步换算事件 tick 与 tempo，音符的实际播放时间保持不变。
- “小节偏移”通过 MIDI 前置不完整小节调整小节线，鼓点 tick、力度和速度事件保持原位；当前提供自动、前移 1–2 拍和后移 1–2 拍。
- 模型加载失败时会使用本地频谱算法与固定节拍网格回退，网页会明确显示“使用回退”。

当前 Intel i5 iMac 的 HTDemucs 分轨预计处理速度约为音频时长的 6–9 倍。MelBand RoFormer 的 CPU 耗时会更长，首次还要完成 913MB 模型下载，页面会持续显示当前阶段与运行时间。STRUM 在实测 10 秒《深渊》鼓轨片段上，包含首次模型加载共约 35 秒。

## 命令行验证

网页之外，也可以直接运行分离内核：

```bash
.venv/bin/python scripts/separate.py "/path/to/song.mp3" --output output
```

## 数据位置

源码运行模式使用下列项目目录：

```text
data/
├── jobs.sqlite3
├── jobs/       上传文件、任务输出
└── models/     Demucs、MelBand RoFormer、ADTOF/Beat This! 与 STRUM 模型缓存
```

任务文件会保留到界面中手动删除。处理中间文件会在成功、失败或取消后清理。
旧版本生成的 WAV 会继续保留，可在任务卡片点击“压缩旧文件”直接转换，无需重新运行分离模型。
MIDI 可以在网页中切换模型、拍号和小节偏移并重新生成。ADTOF 适合快速草稿，STRUM 增加 Ride、三类 Tom 等细分并加强复杂鼓型识别。弱音、开闭镲变化和强混响仍可能需要在 DAW 中人工修正。

## MIDI 模型与许可

- [Beat This!](https://github.com/CPJKU/beat_this) 的代码与公开权重采用 MIT 许可证，模型缓存位于 `data/models/torch/hub/checkpoints/`。
- [ADTOF-PyTorch](https://github.com/xavriley/ADTOF-pytorch) 固定到提交 `85c192e`，五分类权重随 Python 包安装。
- ADTOF-PyTorch 上游仓库当前没有单独列出许可证；权重来源项目 [ADTOF](https://github.com/mzehren/adtof) 标注 CC BY-NC-SA 4.0。制作公开或商业安装包前需要再次确认其分发授权。
- [STRUM](https://github.com/opria123/strum) 固定到提交 `9f420cb6`，鼓权重固定到 Hugging Face 版本 `5b9ab23b`。上游项目采用 MIT 许可，更多固定版本与用途见 [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md)。

## 人声模型与许可

- 推理层使用 [openmirlab/melband-roformer-infer](https://github.com/openmirlab/melband-roformer-infer) `0.1.5`，代码采用 MIT 许可。
- 默认权重使用 Kimberley Jensen 训练的 `MelBand RoFormer Kim`，首次使用会下载到 `data/models/melband-roformer/`。权重由第三方单独发布，打包或商业分发前需要再次确认权重授权。

## API

启动后访问 <http://127.0.0.1:8765/api/docs> 查看接口文档。

主要接口：

```text
POST   /api/jobs
GET    /api/jobs
GET    /api/jobs/{id}
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry
POST   /api/jobs/{id}/compress
POST   /api/jobs/{id}/midi?force=true&bar_offset_beats=1&midi_model=strum&meter=6%2F8
GET    /api/jobs/{id}/midi/preview?bars=4
DELETE /api/jobs/{id}
GET    /api/jobs/{id}/files/{original|drums|no_drums|vocals|instrumental|midi}
```

## 测试

```bash
.venv/bin/pytest
```

自动化测试使用轻量假模型，不会下载 Demucs 或 STRUM 权重。真实 Demucs 模型冒烟测试需要提供一段音频：

```bash
.venv/bin/python scripts/separate.py test.wav --output output
```

## macOS 打包

打包脚本会生成 `.app`、DMG 和 SHA-256 校验文件。PyInstaller 会针对当前构建机器生成单一架构产物：

```bash
MACOS_TARGET_ARCH=arm64 ./scripts/build_macos.sh
```

Intel Mac 可以生成 `x86_64` 验证包。仓库的 `Build macOS Apple Silicon` GitHub Actions 工作流使用 `macos-15` M1 运行器生成 ARM64 DMG。也可以在 Apple Silicon Mac 上直接执行上述命令。

未配置 Apple Developer ID 时，脚本会生成 ad-hoc 签名包，适合内部测试。工作流支持通过 GitHub Secrets 导入 Developer ID 证书，并在配置 Apple ID、Team ID 和 app-specific password 后自动公证 DMG。
