# 鼓点拆解室

本地运行的鼓轨分离网页工具。上传一首歌后，使用 `HTDemucs FT` 生成：

- `drums.mp3`：鼓轨
- `no_drums.mp3`：去鼓伴奏

音频只保存在本机，服务仅监听 `127.0.0.1`。

## 系统要求

- macOS 15
- 至少 16GB 内存
- 首次安装和首次下载模型时需要网络
- 建议预留 5GB 以上磁盘空间

项目会在自己的 `.runtime` 和 `.venv` 目录中安装 Python 3.11 与依赖，不修改系统 Python，也不需要 Homebrew、Docker 或 Redis。

## 启动

在 Finder 中双击 `run.command`，或在终端运行：

```bash
./run.command
```

启动完成后会自动打开浏览器。如果浏览器没有自动打开，请访问：<http://127.0.0.1:8765>

页面操作只有三步：

1. 点击“选择音频”。
2. 点击“开始分离鼓轨”。
3. 等待处理完成，试听或下载鼓轨和去鼓伴奏。

第一次启动会安装运行环境。第一次提交任务会下载 `htdemucs_ft` 模型，等待时间会比后续任务长。

## 使用限制

- 支持 WAV、MP3、FLAC、M4A、OGG。
- 单文件最大 500MB。
- 音频最长 15 分钟。
- 固定使用 CPU 和 `htdemucs_ft` 高质量模型。
- 同一时间处理一个任务，其余任务进入队列。
- 输出为 44.1kHz、双声道、256 kbps MP3。5 分钟音频的每条结果通常约 10MB。

当前 Intel i5 iMac 的预计处理速度约为音频时长的 6–9 倍。实际速度会受到编曲复杂度、系统负载和首次模型加载影响。

## 命令行验证

网页之外，也可以直接运行分离内核：

```bash
.venv/bin/python scripts/separate.py "/path/to/song.mp3" --output output
```

## 数据位置

```text
data/
├── jobs.sqlite3
├── jobs/       上传文件、任务输出
└── models/     Demucs 模型缓存
```

任务文件会保留到网页中手动删除。处理中间文件会在成功、失败或取消后清理。
旧版本生成的 WAV 会继续保留，可在任务卡片点击“压缩旧文件”直接转换，无需重新运行分离模型。

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
DELETE /api/jobs/{id}
GET    /api/jobs/{id}/files/{original|drums|no_drums}
```

## 测试

```bash
.venv/bin/pytest
```

自动化测试使用轻量假模型，不会下载 Demucs 权重。真实模型冒烟测试需要提供一段音频：

```bash
.venv/bin/python scripts/separate.py test.wav --output output
```
