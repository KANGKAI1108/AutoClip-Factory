# AutoClipFactory - Mac 本地离线 AI 视频高光剪辑工具

适配 **8GB 统一内存 MacBook Air**（Apple Silicon M1/M2/M3/M4），纯本地离线运行，优先稳定、牺牲速度。

将「视频下载/上传 → 语音转录 → 镜头切片 → AI 文案推理 → 素材打包」全链路打通为**双击即用**的一键流程，全程不依赖任何云端服务。

---

## 目录

- [项目简介](#项目简介)
- [硬件与系统适配要求](#硬件与系统适配要求)
- [核心功能](#核心功能)
- [快速开始（一键启动）](#快速开始一键启动)
- [手动部署教程](#手动部署教程)
- [项目目录结构](#项目目录结构)
- [完整操作流程](#完整操作流程)
- [API 接口文档](#api-接口文档)
- [8GB 内存优化说明](#8gb-内存优化说明)
- [离线运行说明](#离线运行说明)
- [常见报错修复方案](#常见报错修复方案)

---

## 项目简介

AutoClipFactory 是一款运行在 macOS Apple Silicon 上的**纯本地**短视频高光剪辑工具。专为 **8GB 统一内存的 MacBook Air** 设计，通过严格的内存分区管控、进程互斥锁、分层冷却机制，在有限内存下稳定完成从原始视频到带 AI 文案的成片素材的端到端处理。

**核心原则**：优先稳定，牺牲速度。所有大内存负载（Whisper 转录 / FFmpeg 切片 / Ollama 推理）分时串行运行，绝不并发抢占内存。

### 全链路流程

```
视频来源（URL下载 / 本地上传）
        │
        ▼
  yt-dlp 下载（8MB流式写入 + 2GB硬拦截）
        │
        ▼
  faster-whisper 转录（CPU int8 + 30s分块）
        │
        ▼
  FFmpeg 切片（scene滤镜镜头打分 + auto/manual双模式）
        │
        ▼
  Ollama llama3:8b 推理（生成title/hook/SEO标签）
        │
        ▼
  素材打包输出（mp4 + details.json + details.txt + score.txt）
```

---

## 硬件与系统适配要求

### 硬件

| 项目 | 要求 |
|------|------|
| 芯片 | Apple Silicon（M1 / M2 / M3 / M4） |
| 内存 | 8GB 统一内存（已针对此规格做全部内存约束优化；16GB+ 可正常运行） |
| 磁盘 | 至少 10GB 可用空间（含 Ollama 模型约 4.7GB + faster-whisper 模型 140MB + 临时文件） |

### 系统

| 项目 | 要求 |
|------|------|
| 操作系统 | macOS 13.0+（Ventura / Sonoma / Sequoia） |
| 终端 | zsh（macOS 默认终端，无需额外配置） |
| Python | 3.10 ~ 3.13（一键脚本会自动安装） |
| Homebrew | 用于安装 ffmpeg / ollama（一键脚本会自动安装） |
| Deno | YouTube链接解析依赖，本机已安装完成（`brew install deno`），无需再次执行 |
| 代理端口 | 暴加速默认端口7892，代码内 `--proxy` 已统一修改为7892 |

### macOS TCC 磁盘权限

首次运行时，应用会在 `~/AutoClip_Factory/` 写入 `test_write.tmp` 校验磁盘读写权限。若 macOS 弹出 TCC 权限请求，请允许终端 / Python 对该目录的访问。校验失败时，前端页面会以红字提示权限路径，并同步写入 `error.log`。

---

## 核心功能

### 双模式视频输入

- **链接下载模式**：粘贴 YouTube 等平台视频 URL，通过 yt-dlp 自动下载（8MB 流式写入 + 2GB 硬拦截 + 900 秒超时保护）
- **本地上传模式**：直接上传本地 MP4/MOV/MKV/WEBM/AVI 视频文件（支持 2GB 以内）

### 自动 AI 高光切片

- FFmpeg scene 滤镜自动检测镜头切换，按场景变化打分筛选高光片段
- 双模式切换：`auto`（全自动 AI 切片）或 `manual`（手动指定切片参数）
- 切片时长约束：最短 15 秒、最长 2 分钟

### Ollama AI 文案推理

- 本地 llama3:8b（Q4_K_M 量化）模型为每个切片生成：
  - `title`：不超过 50 字符的标题
  - `hook`：20 个英文词的吸引点描述
  - `tags`：3 个英文 SEO 标签
- Token 粗算（`len(text)//4`，零依赖），超过 700 token 自动分 2 组推理

### 素材打包输出

- 每个切片在输出目录下生成 4 个文件：
  - `clip_XXX.mp4` — 切片视频
  - `clip_XXX_details.json` — 结构化详情（id/title/hook/tags/时间轴/score）
  - `clip_XXX_details.txt` — 人类可读文本
  - `clip_XXX_score.txt` — 评分

### 断点续存缓存

- 缓存 key：`{文件名}_{字节大小}_{修改时间戳}`（不使用哈希）
- 源视频特征变更自动清空对应缓存，源文件删除自动清理废弃缓存

### 批量容错

- 单条切片链路失败写入 `error.log`，跳过当前任务不中断整批队列

---

## 核心使用流程（优先级说明）

> **推荐工作流：上传本地 MP4 视频**（点击【选择文件】），无代理、网络依赖，零报错。
>
> YouTube 海外链接仅作为备用方案，必须后台运行暴加速代理才可使用。

## 快速开始（一键启动）

> 最简方式：双击 `start.command` 即可，脚本自动完成全部环境检测、依赖安装、模型拉取、服务启动。

### 步骤 1：克隆仓库

```bash
git clone https://github.com/KANGKAI1108/AutoClipFactory.git
cd AutoClipFactory
```

### 步骤 2：授权脚本执行权限（仅首次）

```bash
chmod +x start.command
```

### 步骤 3：双击运行

在 Finder 中**双击 `start.command`** 文件即可。

或者在终端中执行：

```bash
./start.command
```

脚本会自动完成以下全部步骤（已安装则跳过）：

1. 检测并安装 Homebrew（Apple Silicon 路径自动配置）
2. 检测并安装 Python3
3. 检测并安装 FFmpeg
4. 检测并安装 Ollama，后台静默启动服务，自动拉取 llama3:8b 模型（约 4.7GB）
5. 创建项目工作文件夹（Source_Videos / Temp_Clips / Output / static）
6. 创建 Python 虚拟环境，使用清华 pip 镜像源安装全部依赖
7. 自动打开浏览器访问 http://127.0.0.1:5000
8. 启动 Flask 主程序

> **首次运行提示**：faster-whisper 的 `base.en` 模型（约 140MB）会在首次转录时自动联网下载，仅此一次。

---

## 手动部署教程

如需手动部署（不使用一键脚本），按以下步骤操作：

### 1. 克隆仓库

```bash
git clone https://github.com/KANGKAI1108/AutoClipFactory.git
cd AutoClipFactory
```

### 2. 安装 Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Apple Silicon 需配置环境变量
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zshrc
source ~/.zshrc
```

### 3. 安装系统依赖

```bash
brew install ffmpeg
brew install python3
brew install ollama
```

### 4. 启动 Ollama 服务并拉取模型

```bash
# 后台启动 Ollama 服务（脚本绝不通过 pkill/kill 干预它）
nohup ollama serve >/dev/null 2>&1 &

# 拉取 llama3:8b 模型（Q4_K_M 量化，约 4.7GB）
ollama pull llama3:8b
```

### 5. 安装 Python 依赖

```bash
python3 -m venv .venv
source .venv/bin/activate

# 使用清华镜像源加速（可选）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 6. 启动程序

```bash
source .venv/bin/activate
python3 app.py
```

浏览器打开 **http://127.0.0.1:5000** 即可使用。

---

## 项目目录结构

```
AutoClipFactory/
├── start.command           # Mac 一键启动脚本（双击运行，zsh）
├── app.py                  # Flask 后端主文件（单文件，含全部路由与全流程编排）
├── index.html              # 前端单页面（启动检测 / 任务提交 / 日志轮询）
├── requirements.txt        # Python 依赖清单
├── downloader.py           # yt-dlp 下载器模块（8MB流式写 + 2GB拦截）
├── transcriber.py          # faster-whisper 转录器模块（CPU int8 + 30s分块）
├── ffmpeg_processor.py     # FFmpeg 切片 + scene 镜头打分模块（auto/manual双模式）
├── memory_watcher.py       # 系统内存/CPU 监控 + 进程互斥锁 + 分层冷却
├── ollama_agent.py         # Ollama llama3:8b 推理代理（API keep_alive:0 释放内存）
├── .gitignore              # Git 忽略规则
├── README.md               # 项目文档
├── static/                 # 静态资源目录（首次运行自动创建）
├── Source_Videos/          # 原始视频存放（本地上传，首次运行自动创建）
├── Temp_Clips/             # 临时切片 + 字幕 + 断点缓存（首次运行自动创建）
├── Output/                 # 项目根输出目录（首次运行自动创建）
└── ~/AutoClip_Factory/     # TCC 权限校验 + 最终打包输出根目录（用户主目录下）
    ├── test_write.tmp      #   TCC 磁盘权限校验文件
    ├── Source_Videos/      #   yt-dlp 下载文件存放处
    └── Output/             #   最终打包输出（日期_任务名/子目录）
```

> 四个工作目录（`Source_Videos/`、`Temp_Clips/`、`Output/`、`static/`）在首次启动时自动创建，无需手动建。

---

## 完整操作流程

### 1. 启动服务

双击 `start.command` 或执行 `python3 app.py`，等待浏览器自动打开。

### 2. 查看启动检测

页面首页显示系统检测卡片（TCC 权限 / 内存 / 系统信息），确认全部通过。

### 3. 选择切片模式

- `auto`：自动 AI 切片（推荐）
- `manual`：手动切片

### 4. 提交视频

- **链接下载**：粘贴视频 URL（支持 YouTube 等平台），点击提交
- **本地上传**：选择本地视频文件上传（< 2GB）

### 5. 一键全流程

点击「全流程处理」按钮，或使用 `/api/run_full_pipeline` 接口，自动执行：

```
下载/上传 → 转录 → 切片 → AI推理 → 打包输出
```

### 6. 查看进度

页面实时显示当前阶段与进度百分比，日志区显示详细处理日志。

### 7. 获取结果

处理完成后，在 `~/AutoClip_Factory/Output/日期_任务名/` 目录下查看打包结果：

```
~/AutoClip_Factory/Output/20250731_my_video/
├── clip_001.mp4
├── clip_001_details.json
├── clip_001_details.txt
├── clip_001_score.txt
├── clip_002.mp4
├── clip_002_details.json
├── clip_002_details.txt
└── clip_002_score.txt
```

---

## API 接口文档

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 返回前端页面 |
| `/api/startup_check` | GET | 获取启动检测结果 |
| `/api/task_state` | GET | 获取当前任务状态 |
| `/api/logs` | GET | 获取最新 N 行日志（参数 `n`） |
| `/api/set_clip_mode` | POST | 设置切片模式（`{"mode":"auto"}`） |
| `/api/submit_link` | POST | 提交 URL 下载视频 |
| `/api/upload_video` | POST | 上传视频文件（< 2GB） |
| `/api/run_transcribe` | POST | 启动语音转录 |
| `/api/run_slicing` | POST | 启动 FFmpeg 切片 |
| `/api/run_full_pipeline` | POST | **端到端全流程** |
| `/api/cancel_task` | POST | 取消当前任务 |

### 全流程调用示例

```bash
# URL 下载模式
curl -s -X POST http://127.0.0.1:5000/api/run_full_pipeline \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=xxxxx"}'

# 本地文件模式
curl -s -X POST http://127.0.0.1:5000/api/run_full_pipeline \
  -H 'Content-Type: application/json' \
  -d '{"source_video":"/path/to/video.mp4"}'

# 上传文件模式
curl -s -X POST http://127.0.0.1:5000/api/run_full_pipeline \
  -F 'file=@/path/to/video.mp4'
```

---

## 8GB 内存优化说明

本项目针对 8GB 统一内存 MacBook Air 做了以下专项优化：

### 进程互斥锁

Whisper / FFmpeg / Ollama 三类大内存进程分时串行运行，任意时刻仅一种负载占用内存，绝不并发。

### 分层冷却机制

| 场景 | 冷却时间 |
|------|----------|
| 单次转录完成 | 30 秒 |
| 单次切片完成 | 30 秒 |
| 单组 AI 推理完成 | 30 秒 |
| 完整处理一条视频 | 180 秒 |

### 内存轮询保护

- 推理期间每 2 秒检测可用内存
- 可用内存 < 1GB → 休眠 45 秒
- 可用内存持续 < 0.5GB → 中断推理，保存已生成缓存
- 模型卸载后等待 15 秒校验，不足 3.2GB → 全局休眠 120 秒

### CPU 过载保护

连续 3 秒 CPU > 95% → 强制休眠 60 秒。

### Ollama 低内存模式

- 启动时自动设置 `OLLAMA_LOW_VRAM=1` 环境变量
- API 请求体固定 `keep_alive:"0"`，每次推理后通过 API 释放模型内存（绝不 pkill/kill Ollama 服务进程）
- `num_ctx:1024` 嵌套在 `options` 对象内，限制上下文窗口

### Token 零内存粗算

使用 `len(text)//4` 估算 token 数（不引入 HuggingFace Tokenizer），超过 700 token 自动最多分为 2 组推理。

---

## 离线运行说明

本项目为**纯本地离线运行**，无任何云端接口、无 Vercel/公网部署逻辑。

### 需要联网的场景（仅首次）

| 场景 | 大小 | 说明 |
|------|------|------|
| Homebrew 安装 | ~50MB | 首次安装 Homebrew 包管理器 |
| FFmpeg 安装 | ~100MB | 通过 Homebrew 安装 |
| Ollama 安装 | ~200MB | 通过 Homebrew 安装 |
| llama3:8b 模型拉取 | ~4.7GB | Q4_K_M 量化模型，仅拉取一次 |
| faster-whisper 模型 | ~140MB | base.en 模型，首次转录时自动下载 |
| Python 依赖安装 | ~500MB | Flask / faster-whisper / yt-dlp 等 |

### 之后完全离线

以上全部安装完成后，后续运行**完全离线**，无需任何网络连接。所有视频处理、语音转录、AI 推理均在本地完成。

---

## 启动前检查清单

- [ ] Python 虚拟环境已创建且依赖已安装（`start.command` 自动完成）
- [ ] FFmpeg 已安装（`ffmpeg -version` 可正常输出）
- [ ] Ollama 服务已启动且 llama3:8b 模型已拉取（`ollama list` 可见 llama3:8b）
- [ ] Deno 已安装（`deno --version` 可正常输出，YouTube 链接解析依赖）
- [ ] macOS TCC 磁盘权限已授权（终端/Python 可访问 `~/AutoClip_Factory/`）
- [ ] **使用 YouTube 链接下载前**：确认暴加速代理软件已启动（端口7892）

---

## 常见报错修复方案

### 1. TCC 磁盘权限失败（页面红字提示）

**现象**：启动后页面 TCC 权限卡片显示红字，`error.log` 中有权限错误。

**修复**：
1. 打开「系统设置 → 隐私与安全性 → 完全磁盘访问权限」
2. 点击「+」号，添加终端（Terminal）或 iTerm
3. 如使用虚拟环境，同时添加 `.venv/bin/python3`
4. 重启终端后重新运行 `start.command`

### 2. Ollama 服务无法启动

**现象**：`ollama serve` 启动失败或 `curl http://127.0.0.1:11434` 无响应。

**修复**：
```bash
# 检查端口是否被占用
lsof -i :11434

# 手动启动
ollama serve

# 如端口冲突，指定其他端口
OLLAMA_HOST=127.0.0.1:11435 ollama serve
```

### 3. llama3:8b 模型拉取失败

**现象**：`ollama pull llama3:8b` 超时或中断。

**修复**：
```bash
# 重新拉取（支持断点续传）
ollama pull llama3:8b

# 如网络不稳定，配置代理后重试
```

### 4. Python 依赖安装失败

**现象**：`pip install -r requirements.txt` 报错。

**修复**：
```bash
# 更新 pip
pip install --upgrade pip

# 使用清华镜像源
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 或使用阿里云镜像源
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

### 5. FFmpeg 命令找不到

**现象**：切片时报 `ffmpeg: command not found`。

**修复**：
```bash
brew install ffmpeg

# 验证安装
ffmpeg -version
```

### 6. start.command 无法双击运行

**现象**：双击 `start.command` 提示「无法执行」或「权限不足」。

**修复**：
```bash
chmod +x start.command

# 如仍无法运行，在终端中直接执行
./start.command
```

### 7. macOS 提示「无法验证开发者」

**现象**：双击 `start.command` 时 macOS 拦截，提示安全风险。

**修复**：
1. 打开「系统设置 → 隐私与安全性」
2. 下滑找到「允许下次启动时打开」提示，点击「仍要打开」
3. 或在终端执行：`xattr -d com.apple.quarantine start.command`

### 8. 内存不足导致系统卡顿

**现象**：处理视频时 macOS 出现严重卡顿或 swap 占用过高。

**修复**：
- 确保处理视频时关闭其他大型应用（如 Chrome 多标签页、Docker 等）
- 使用 3~5 分钟的短素材进行调试（v4.5 规范要求）
- 确认 Ollama 服务已启动且模型已拉取（避免重复加载）
- 检查 `app.log` 中内存监控日志，确认分层冷却正常工作

### 9. 端口 5000 被占用

**现象**：Flask 启动报 `Address already in use`。

**修复**：
```bash
# 查看占用 5000 端口的进程
lsof -i :5000

# 终止占用进程（仅限非系统进程）
kill -9 <PID>

# 重新启动
python3 app.py
```

### 10. faster-whisper 模型下载失败

**现象**：首次转录时卡在模型下载步骤。

**修复**：
```bash
# 手动预下载模型（在虚拟环境中执行）
source .venv/bin/activate
python3 -c "from faster_whisper import WhisperModel; WhisperModel('base.en', compute_type='int8')"
```

### 11. YouTube 下载报错 Errno 61 Connection refused / SSL连接中断

**现象**：通过链接下载 YouTube 视频时报 `Errno 61 Connection refused`，或重试后仍下载失败。

**故障根源**：暴加速软件未打开，或代码代理端口与软件端口不一致（本机固定7892）。

**解决步骤**：

```bash
# ① 打开暴加速，保持软件后台常驻

# ② 核对 downloader.py 内 proxy 端口为7892
#    "--proxy", "http://127.0.0.1:7892"

# ③ 更新 yt-dlp 到最新版（使用清华镜像源加速）
pip3 install -U yt-dlp -i https://pypi.tuna.tsinghua.edu.cn/simple

# ④ 重启项目
./start.command
# 或手动重启
source .venv/bin/activate && python3 app.py

# ⑤ 重新提交 YouTube 链接测试
```

> **推荐**：如代理环境不稳定，建议直接使用【选择文件】上传本地 MP4 视频，完全规避网络问题。

### 12. 模型加载失败 float16 compute type 不支持

**现象**：进入音频转录阶段，Whisper 模型加载时报 `float16 compute type is not supported` 或硬件不兼容错误。

**故障原因**：本机 Mac 硬件无 float16 半精度运算加速（部分 Mac 机型 / CTranslate2 CPU 后端不支持），导致 Whisper 默认 float16 无法加载。

**修复逻辑**：代码已自动切换为 **int8 整型运算精度**，全机型兼容，内存消耗减半。

**手动备选优化（低配 8GB 设备可进一步降低负载）**：

```bash
# 方案1：把模型名称 base.en 替换为 tiny.en，大幅降低 CPU/内存占用
# 修改 transcriber.py：
# MODEL_SIZE = "base.en"  →  MODEL_SIZE = "tiny.en"

# 方案2：关闭其他后台软件释放内存后重启项目
./start.command
```

### 13. 任务完成但文件夹只有字幕 json，无剪辑视频

**现象**：任务显示全部完成（进度 100%），`Temp_Clips` / `Output` 目录只有 srt 字幕和 slice_candidates.json，没有拼接好的 MP4 成片。

**故障根因**：缺少 FFmpeg 视频裁剪、拼接导出代码，仅完成文本分析未合成视频（本次迭代已补充完整视频合成逻辑）。

**修复操作**：本次迭代已新增 `clip_assembler.py` 模块，自动读取高光时间段 → FFmpeg 裁剪 → concat 拼接 → 输出至 `Finished_Clips`。

**手动排查步骤**：

```bash
# ① 确认 FFmpeg 已安装（未安装执行 brew install ffmpeg）
ffmpeg -version

# ② 修复项目文件夹读写权限
chmod -R 777 ./AutoClipFactory

# ③ 重启项目重新提交剪辑任务
./start.command
```

**成片存放路径**：项目内 `Finished_Clips/` 文件夹，命名规则 `原视频名称_高光成片_时间戳.mp4`（统一带时间戳，避免重名覆盖）。

### 界面功能说明（高光成片导出）

剪辑完成后页面自动展示白色结果卡片，包含以下内容：

1. **完成标识**：✅ 高光剪辑&编码全部完成
2. **三行文件信息**：
   - 📁 成品统一保存目录：`Finished_Clips`
   - 🎬 成片文件名：`{视频名}_高光成片_{时间戳}.mp4`
   - 📍 文件完整路径：本地绝对路径（可直接复制定位）
3. **双操作按钮**（并排摆放）：
   - **⬇ 下载成片MP4**：浏览器直接下载成品视频文件
   - **📂 打开文件所在文件夹**：一键调用 macOS 访达打开 `Finished_Clips` 目录
4. **无成片时的状态**：按钮置灰，提示"未生成剪辑视频"
5. **临时分片自动清理**：仅保留成片、字幕、切片清单文件，节省磁盘空间

---

## 许可证

本项目仅供学习和个人使用。
