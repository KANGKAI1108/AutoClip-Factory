# AutoClip Factory

本地 AI 高光剪辑工具 —— 适配 8GB 统一内存 MacBook Air，优先稳定、牺牲速度、纯本地离线运行。

基于 v4.5 工程规范开发，将「视频下载 → 语音转录 → 镜头切片 → AI 文案推理 → 素材打包」全链路打通为单机一键流程，全程不依赖任何云端服务。

---

## 目录

- [项目简介](#项目简介)
- [核心特性](#核心特性)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [环境要求](#环境要求)
- [安装步骤](#安装步骤)
- [启动运行](#启动运行)
- [使用方式](#使用方式)
- [API 接口文档](#api-接口文档)
- [架构设计](#架构设计)
- [内存防护体系](#内存防护体系)
- [约束规范](#约束规范)
- [自测验收](#自测验收)
- [常见问题](#常见问题)

---

## 项目简介

AutoClip Factory 是一款运行在 macOS Apple Silicon（M1/M2/M3）上的**纯本地**短视频高光剪辑工具。它专为 **8GB 统一内存的 MacBook Air** 设计，通过严格的内存分区管控、进程互斥锁、分层冷却机制，在有限内存下稳定完成从原始视频到带 AI 文案的成片素材的端到端处理。

**核心目标**：优先稳定，牺牲速度。所有大内存负载（Whisper 转录 / FFmpeg 切片 / Ollama 推理）分时串行运行，绝不并发抢占内存。

### 全链路流程

```
视频来源（URL下载 / 本地文件 / 网页上传）
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

## 核心特性

### 全链路端到端

- 一键启动：`/api/run_full_pipeline` 接口支持 URL 下载、本地视频路径、网页上传三种触发方式
- 自动串联：下载 → 转录 → 切片 → AI 推理 → 打包输出，全程后台线程执行
- 实时进度：前端轮询 `/api/task_state` 获取当前阶段与进度百分比

### 8GB 内存专项优化

- **进程互斥锁**：Whisper / FFmpeg / Ollama 三类大内存进程分时串行，任意时刻仅一种负载运行
- **分层冷却**：单次转录/切片/AI 推理完成后休眠 30 秒；完整处理一条视频后全局休眠 180 秒
- **内存轮询**：推理期间每 2 秒检测可用内存，低于 1GB 休眠 45 秒，持续低于 0.5GB 中断并保存已生成缓存
- **CPU 过载保护**：连续 3 秒 CPU > 95% 强制休眠 60 秒
- **Ollama 低内存模式**：启动时自动设置 `OLLAMA_LOW_VRAM=1` 环境变量

### 断点续存缓存

- 缓存 key 拼接规则：`{文件名}_{文件字节大小}_{文件修改时间戳}`（不使用哈希）
- 源视频特征变更（大小/时间戳变化）自动清空对应缓存
- 源视频文件删除自动清理废弃缓存目录，防止磁盘堆积
- 已切片/已推理的中间结果可复用，重跑时自动跳过

### 批量容错

- 单条切片链路失败写入 `error.log`，跳过当前任务不中断整批队列
- 全流程异常自动释放任务锁，前端可获取错误信息

---

## 技术栈

| 组件 | 说明 |
|------|------|
| **Python 3** | 后端语言 |
| **Flask** | 单文件后端 Web 框架（`app.py`），localhost:5000 |
| **faster-whisper** | 本地语音转录（CTranslate2 CPU int8，base.en 模型） |
| **FFmpeg** | 视频切片 + scene 滤镜镜头打分（高光筛选） |
| **yt-dlp** | 视频下载（支持 YouTube 等多平台） |
| **Ollama llama3:8b (Q4_K_M)** | 本地大语言模型，生成切片文案（title/hook/SEO标签） |

**纯本地离线**：无云端接口、无公网部署逻辑。faster-whisper 模型仅首次联网下载约 140MB。

---

## 目录结构

```
AutoClip-Factory/
├── app.py                  # Flask 后端主文件（单文件，含全部路由与全流程编排）
├── index.html              # 前端单页面（启动检测 / 任务提交 / 日志轮询）
├── requirements.txt        # Python 依赖清单
├── downloader.py           # yt-dlp 下载器模块
├── transcriber.py          # faster-whisper 转录器模块
├── ffmpeg_processor.py     # FFmpeg 切片 + scene 镜头打分模块
├── memory_watcher.py       # 系统内存/CPU 监控 + 进程互斥锁 + 分层冷却
├── ollama_agent.py         # Ollama llama3:8b 推理代理（API keep_alive:0 释放内存）
├── static/                 # 静态资源目录（首次运行自动创建）
├── Source_Videos/          # 原始视频存放（本地上传，首次运行自动创建）
├── Temp_Clips/             # 临时切片 + 字幕 + 断点缓存（首次运行自动创建）
├── Output/                 # 项目根输出目录（首次运行自动创建）
└── ~/AutoClip_Factory/     # TCC 权限校验根目录（用户主目录下）
    ├── test_write.tmp      #   TCC 磁盘权限校验文件
    ├── Source_Videos/      #   yt-dlp 下载文件存放处
    └── Output/             #   最终打包输出根目录（日期_任务名/子目录）
```

> **注意**：`Source_Videos/`、`Temp_Clips/`、`Output/`、`static/` 四个目录在首次启动时由 `init_directories()` 自动创建，无需手动建。

---

## 环境要求

### 硬件

- **macOS Apple Silicon**（M1 / M2 / M3），推荐 MacBook Air
- **8GB 统一内存**（已针对此规格做全部内存约束优化；16GB 可正常运行但约束不变）
- 磁盘空间：至少 5GB（含模型与临时文件）

### 软件

- **macOS** 13.0+（Ventura / Sonoma / Sequoia）
- **Python** 3.10 ~ 3.13
- **Homebrew**（用于安装 ffmpeg 和 ollama）
- **Ollama**（用于运行 llama3:8b 本地模型）

### macOS TCC 磁盘权限

首次运行时，应用会在 `~/AutoClip_Factory/` 写入 `test_write.tmp` 校验磁盘读写权限。若 macOS 弹出 TCC 权限请求，请允许终端 / Python 对该目录的访问。校验失败时，前端页面会以红字提示权限路径，并同步写入 `error.log`。

---

## 安装步骤

### 1. 克隆仓库

```bash
git clone https://github.com/KANGKAI1108/AutoClip-Factory.git
cd AutoClip-Factory
```

### 2. 安装系统依赖

```bash
# 安装 Homebrew（如未安装）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装 FFmpeg（切片 + scene 打分）
brew install ffmpeg

# 安装 Ollama（本地大模型运行时）
brew install ollama
```

### 3. 拉取 Ollama 模型

```bash
# 启动 Ollama 服务（后台常驻，脚本绝不通过 pkill/kill 干预它）
ollama serve &

# 拉取 llama3:8b（Q4_K_M 量化，约 4.7GB 下载）
ollama pull llama3:8b
```

### 4. 安装 Python 依赖

```bash
# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip3 install -r requirements.txt
```

> faster-whisper 的 `base.en` 模型（约 140MB）会在首次运行转录时自动联网下载，仅此一次。

---

## 启动运行

```bash
cd AutoClip-Factory
source .venv/bin/activate
python3 app.py
```

启动后控制台输出：

```
 * Running on http://127.0.0.1:5000
```

浏览器打开 **http://127.0.0.1:5000** 即可使用 Web 界面。

### 启动时自动执行

1. 创建工作目录（Source_Videos / Temp_Clips / Output / static）
2. TCC 磁盘权限校验（写 `~/AutoClip_Factory/test_write.tmp`）
3. 前台进程内存扫描（检测 >1GB 的第三方进程，清理残留 yt-dlp/ffmpeg 子进程）
4. Apple Intelligence 活跃进程检测（macOS 15.1+）
5. 全部检测结果写入 `app.log`，前端 `/api/startup_check` 可拉取

---

## 使用方式

### 方式一：Web 界面（推荐）

浏览器打开 http://127.0.0.1:5000，在页面上：

1. 查看启动检测卡片（TCC 权限 / 内存 / 系统信息）
2. 选择切片模式：`auto`（自动 AI 切片）或 `manual`（手动切片）
3. 提交视频：粘贴 URL 或上传本地视频文件
4. 实时查看任务进度与日志
5. 处理完成后在 `~/AutoClip_Factory/Output/日期_任务名/` 查看打包结果

### 方式二：API 调用

#### 端到端全流程（阶段4，推荐）

```bash
# 方式 A：通过 URL 下载后全流程处理
curl -s -X POST http://127.0.0.1:5000/api/run_full_pipeline \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://www.youtube.com/watch?v=xxxxx"}' | python3 -m json.tool

# 方式 B：指定本地视频文件
curl -s -X POST http://127.0.0.1:5000/api/run_full_pipeline \
  -H 'Content-Type: application/json' \
  -d '{"source_video":"/path/to/video.mp4"}' | python3 -m json.tool

# 方式 C：上传视频文件（< 2GB）
curl -s -X POST http://127.0.0.1:5000/api/run_full_pipeline \
  -F 'file=@/path/to/video.mp4' | python3 -m json.tool
```

#### 分步执行（阶段2/3，可单独使用）

```bash
# 仅下载
curl -s -X POST http://127.0.0.1:5000/api/submit_link \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://..."}'

# 仅转录
curl -s -X POST http://127.0.0.1:5000/api/run_transcribe \
  -H 'Content-Type: application/json' \
  -d '{"source_video":"/path/to/video.mp4"}'

# 仅切片
curl -s -X POST http://127.0.0.1:5000/api/run_slicing \
  -H 'Content-Type: application/json' \
  -d '{"source_video":"/path/to/video.mp4","mode":"auto"}'
```

#### 查看状态与日志

```bash
# 任务状态
curl -s http://127.0.0.1:5000/api/task_state | python3 -m json.tool

# 最新 100 行日志
curl -s "http://127.0.0.1:5000/api/logs?n=100" | python3 -m json.tool

# 取消当前任务（仅取消脚本启动的子进程，绝不 kill Ollama 服务）
curl -s -X POST http://127.0.0.1:5000/api/cancel_task
```

---

## API 接口文档

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 返回 `index.html` 前端页面 |
| `/api/startup_check` | GET | 获取启动检测结果（TCC权限/内存/系统信息） |
| `/api/task_state` | GET | 获取当前任务状态（stage/progress/status/clip_mode） |
| `/api/logs` | GET | 获取最新 N 行日志（参数 `n`，默认 100，最大 200） |
| `/api/set_clip_mode` | POST | 设置切片模式（`{"mode":"auto"}` 或 `{"mode":"manual"}`） |
| `/api/submit_link` | POST | 提交 URL 下载视频（`{"url":"https://..."}`） |
| `/api/upload_video` | POST | 上传视频文件（multipart，`< 2GB`） |
| `/api/run_transcribe` | POST | 启动语音转录（`{"source_video":"路径"}`） |
| `/api/run_slicing` | POST | 启动 FFmpeg 切片（支持 auto/manual 模式） |
| `/api/run_full_pipeline` | POST | **端到端全流程**（下载→转录→切片→AI→打包） |
| `/api/cancel_task` | POST | 取消当前运行任务 |

### 任务状态字段说明

```json
{
  "code": 0,
  "data": {
    "task_id": "abc123",
    "status": "running",       // idle / running / success / error / cancelled
    "stage": "阶段4：AI推理中",
    "progress": 65,            // 0-100
    "clip_mode": "auto",       // auto / manual
    "error_msg": ""            // status=error 时填充
  }
}
```

---

## 架构设计

### 模块职责

```
app.py                    ← Flask 主文件，全局路由 + 任务编排 + 全流程后台线程
  ├─ downloader.py         ← yt-dlp 封装：8MB流式写 + 2GB拦截 + 900s超时
  ├─ transcriber.py        ← faster-whisper 封装：int8 + 30s分块 + 可取消
  ├─ ffmpeg_processor.py   ← FFmpeg 封装：scene打分 + auto/manual双模式
  ├─ memory_watcher.py     ← 内存/CPU监控 + 进程互斥锁 + 分层冷却
  └─ ollama_agent.py       ← Ollama API 封装：keep_alive:0 + token分组 + JSON归一化
```

### 全流程后台线程（`_thread_full_pipeline`）

```
S0  入参解析（URL / 本地路径 / 上传文件保存）
S1  下载（若 URL）→ acquire("whisper") → 转录 → release → cool_down(30s)
S2  断点缓存检查 → acquire("ffmpeg") → 切片 → release → cool_down(30s)
S3  acquire("ollama") → 分组推理（每2s内存轮询）→ release → 卸载冷却(15s+120s)
S4  打包输出 4 件套到 ~/AutoClip_Factory/Output/日期_任务名/
S5  全局冷却 cool_down(180s)
S6  release_task(success/error)
```

### 打包输出结构

每个切片在输出目录下生成 4 个文件：

```
~/AutoClip_Factory/Output/20250731_my_video/
├── clip_001.mp4           # 切片视频
├── clip_001_details.json  # 结构化详情（id/title/hook/tags/时间轴/score）
├── clip_001_details.txt   # 人类可读文本（title + hook + 标签）
├── clip_001_score.txt     # 评分（scene打分数字）
├── clip_002.mp4
├── clip_002_details.json
├── clip_002_details.txt
└── clip_002_score.txt
```

### Ollama AI 输出格式

每个切片由 llama3:8b 生成固定 JSON 结构：

```json
{
  "id": 1,
  "title": "不超过50字符的标题",
  "hook": "20 words english hook sentence",
  "tags": ["seo_tag_1", "seo_tag_2", "seo_tag_3"]
}
```

- Token 粗算：`estimated_tokens = len(text) // 4`（不引入 HuggingFace Tokenizer）
- 超过 700 token 自动最多分为 2 组推理
- 输出经 `_normalize_output` 归一化：title 截断 50 字符、hook 截断 20 词、tags 固定 3 个英文标签

---

## 内存防护体系

### v4.5 内存约束落地清单

| 约束 | 实现位置 | 说明 |
|------|----------|------|
| OLLAMA_LOW_VRAM=1 | `app.py` 第 1 行 | 脚本最开头设置，在任何 import 之前 |
| keep_alive:"0" API | `ollama_agent.py` | 请求体内 `keep_alive` 为字符串 `"0"`，API 层释放模型内存 |
| num_ctx 嵌套 options | `ollama_agent.py` | `{"options":{"num_ctx":1024}}`，禁止放顶层 |
| Token 粗算 len//4 | `ollama_agent.py` | `_estimate_tokens()`，零 HF 依赖 |
| 700 token 分 2 组 | `ollama_agent.py` | `_split_into_groups()` |
| 推理每 2s 内存轮询 | `ollama_agent.py` | <1GB→sleep 45s；持续<0.5GB→中断+保存缓存 |
| 模型加载跳过 3.2GB 拦截 | `ollama_agent.py` | `highlight_batch` 显式跳过加载阶段内存检查 |
| 卸载后 15s 校验 | `memory_watcher.py` | `post_unload_cooldown_check()`，<3.2GB→sleep 120s |
| 三类进程互斥锁 | `memory_watcher.py` | Whisper/FFmpeg/Ollama 各自独立互斥 |
| CPU 3s>95% 休眠 60s | `memory_watcher.py` | `cpu_overload_protect_if_needed()` |
| 单步冷却 30s | `memory_watcher.py` | `cool_down_step(30)` |
| 全流程冷却 180s | `memory_watcher.py` | `cool_down_full(180)` |

### 进程互斥锁模型

```
三类互斥锁（各自独立，线程感知）：
  ┌─────────┐     ┌─────────┐     ┌─────────┐
  │ Whisper │     │ FFmpeg  │     │ Ollama  │
  │  Lock   │     │  Lock   │     │  Lock   │
  └────┬────┘     └────┬────┘     └────┬────┘
       │               │               │
       └─────── 串行分时使用 8GB 统一内存 ──────┘

规则：任意时刻仅允许一种大内存负载运行。
     Whisper 释放后可立即启动 FFmpeg，但 Whisper 与 Ollama 绝不并发。
```

---

## 约束规范

本项目严格遵循 v4.5 工程规范，核心红线如下：

### 全局硬性红线

1. **进程清理边界**：仅回收脚本 subprocess 启动的 yt-dlp/ffmpeg 子进程；严禁 pkill/kill Ollama 服务，模型内存释放仅依靠 API `keep_alive:"0"`，不干预 `ollama serve` 系统后台。
2. **macOS TCC 磁盘权限**：启动自动在 `~/AutoClip_Factory/` 写入 `test_write.tmp` 校验读写，失败则网页红字提示权限路径，同步写入 `error.log`。
3. **技术栈固定**：Python3 + Flask 单文件后端 + faster-whisper + ffmpeg + yt-dlp + Ollama llama3:8b Q4_K_M；纯本地离线，无云端接口、无公网部署逻辑。
4. **项目目录规则**：根目录存放 `index.html`；根目录 `static` 文件夹；根目录三个工作文件夹：`Source_Videos`、`Temp_Clips`、`Output`；临时文件处理完成自动清空。
5. **开发调试规范**：调试使用 3~5 分钟英文 MP4 短素材，减少 swap 占用。

### 禁用模块约束

- 全程**不引入 LLaVA 视觉模型**，仅依靠 FFmpeg 镜头打分做高光筛选。

---

## 自测验收

项目已通过以下自测（本地执行）：

- Python 语法检查：`app.py` / `memory_watcher.py` / `ollama_agent.py` 全部通过 `py_compile`
- 纯算法自测：token 粗算、分组逻辑、输出归一化、标签清理、进程互斥锁、可中断 sleep 全部通过
- Flask 接口冒烟测试：
  - `GET /` → 200
  - `GET /api/task_state` → idle
  - `POST /api/run_full_pipeline` 空 JSON → 400
  - `POST /api/run_full_pipeline` 不存在路径 → 404
  - `POST /api/run_full_pipeline` 占位文件 → 200 启动线程，后台自动失败释放锁
  - `POST /api/cancel_task` 无运行任务 → 400

---

## 常见问题

### Q: 启动后页面 TCC 权限红字怎么办？

A: 打开「系统设置 → 隐私与安全性 → 文件与文件夹」（或「完全磁盘访问权限」），找到你的终端 / Python，勾选允许访问。然后删除 `~/AutoClip_Factory/test_write.tmp` 后刷新页面重新校验。

### Q: Ollama 推理很慢正常吗？

A: 正常。8GB 内存在 Q4_K_M 量化下运行 llama3:8b 本身较慢，且本项目强制 `OLLAMA_LOW_VRAM=1` + `keep_alive:"0"`（每次推理后卸载模型释放内存，下次推理需重新加载），这是为保稳定的刻意设计。

### Q: 取消任务会杀掉 Ollama 服务吗？

A: **不会。** 取消任务仅终止脚本通过 subprocess 启动的 yt-dlp / ffmpeg 子进程，并设置取消事件让后台线程退出。Ollama 服务（`ollama serve`）作为系统后台进程，绝不被 pkill/kill 干预。

### Q: 断点缓存在哪里？怎么清理？

A: 缓存位于 `Temp_Clips/.clip_cache/{文件名}_{字节大小}_{修改时间戳}/`。源视频特征变更或文件删除时自动清理。如需手动全量清理，删除该目录即可。

### Q: 支持非英文视频吗？

A: 转录默认使用 `base.en` 模型（英文优化）。如需多语言，可在 `transcriber.py` 中将模型改为 `base`（多语言版），但需自行测试内存占用。

### Q: 输出文件在哪里？

A: 最终打包输出在 `~/AutoClip_Factory/Output/{日期}_{任务名}/` 目录下，每个切片包含 mp4 + details.json + details.txt + score.txt 四个文件。

---

## 许可证

本项目仅供学习和个人使用。
