#!/bin/zsh
# ============================================================
# AutoClipFactory - Mac 一键启动脚本
# 适配 Apple Silicon (M1/M2/M3/M4) macOS，zsh 终端
# 双击即可运行：自动检测并安装全部依赖，启动 Flask 主程序
# ============================================================

# ---- 0. 颜色定义 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ---- 1. 脚本所在目录（即项目根目录）----
SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || { echo "${RED}❌ 无法进入脚本目录: $SCRIPT_DIR${NC}"; exit 1; }

echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "${BOLD}${CYAN}  AutoClip Factory - Mac 本地离线 AI 视频高光剪辑工具${NC}"
echo "${BLUE}  一键启动脚本 (Apple Silicon / zsh)${NC}"
echo "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ============================================================
# 步骤 1：检测并安装 Homebrew
# ============================================================
echo "${BOLD}${GREEN}【步骤 1/6】检测 Homebrew 包管理器...${NC}"

if command -v brew &>/dev/null; then
    echo "  ${GREEN}✅ Homebrew 已安装: $(brew --version | head -1)${NC}"
else
    echo "  ${YELLOW}⚠️  Homebrew 未安装，开始自动安装（需要输入密码）...${NC}"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    if [[ $? -ne 0 ]]; then
        echo "  ${RED}❌ Homebrew 安装失败！${NC}"
        echo "  ${YELLOW}修复方案：${NC}"
        echo "    1. 手动访问 https://brew.sh 获取安装命令"
        echo "    2. 确认网络连接正常"
        echo "    3. 重新运行本脚本"
        exit 1
    fi
    echo "  ${GREEN}✅ Homebrew 安装完成${NC}"
fi

# ---- 自动加载 brew 环境变量（Apple Silicon 路径）----
if [[ -f /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [[ -f /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

# ============================================================
# 步骤 2：检测并安装 Python3
# ============================================================
echo ""
echo "${BOLD}${GREEN}【步骤 2/6】检测 Python3...${NC}"

if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    echo "  ${GREEN}✅ Python3 已安装: $PY_VER${NC}"
else
    echo "  ${YELLOW}⚠️  Python3 未安装，通过 Homebrew 安装...${NC}"
    brew install python3
    if [[ $? -ne 0 ]]; then
        echo "  ${RED}❌ Python3 安装失败！${NC}"
        echo "  ${YELLOW}修复方案：${NC}"
        echo "    手动执行: brew install python3"
        echo "    或从 https://www.python.org/downloads/ 下载安装"
        exit 1
    fi
    echo "  ${GREEN}✅ Python3 安装完成: $(python3 --version)${NC}"
fi

# ============================================================
# 步骤 3：检测并安装 FFmpeg
# ============================================================
echo ""
echo "${BOLD}${GREEN}【步骤 3/6】检测 FFmpeg...${NC}"

if command -v ffmpeg &>/dev/null; then
    echo "  ${GREEN}✅ FFmpeg 已安装: $(ffmpeg -version 2>&1 | head -1)${NC}"
else
    echo "  ${YELLOW}⚠️  FFmpeg 未安装，通过 Homebrew 安装...${NC}"
    brew install ffmpeg
    if [[ $? -ne 0 ]]; then
        echo "  ${RED}❌ FFmpeg 安装失败！${NC}"
        echo "  ${YELLOW}修复方案：${NC}"
        echo "    手动执行: brew install ffmpeg"
        exit 1
    fi
    echo "  ${GREEN}✅ FFmpeg 安装完成${NC}"
fi

# ============================================================
# 步骤 4：检测并安装 Ollama + 拉取模型
# ============================================================
echo ""
echo "${BOLD}${GREEN}【步骤 4/6】检测 Ollama 与 llama3:8b 模型...${NC}"

if command -v ollama &>/dev/null; then
    echo "  ${GREEN}✅ Ollama 已安装: $(ollama --version 2>&1)${NC}"
else
    echo "  ${YELLOW}⚠️  Ollama 未安装，通过 Homebrew 安装...${NC}"
    brew install ollama
    if [[ $? -ne 0 ]]; then
        echo "  ${RED}❌ Ollama 安装失败！${NC}"
        echo "  ${YELLOW}修复方案：${NC}"
        echo "    手动执行: brew install ollama"
        echo "    或从 https://ollama.com 下载 macOS 版安装包"
        exit 1
    fi
    echo "  ${GREEN}✅ Ollama 安装完成${NC}"
fi

# ---- 后台静默启动 ollama 服务（屏蔽冗余日志，防止终端刷屏）----
echo "  ${CYAN}⏳ 启动 Ollama 后台服务...${NC}"
if pgrep -x "ollama" &>/dev/null; then
    echo "  ${GREEN}✅ Ollama 服务已在运行${NC}"
else
    # 后台启动，输出重定向到 /dev/null 防止刷屏
    nohup ollama serve >/dev/null 2>&1 &
    # 等待服务就绪（最多等待 15 秒）
    for i in {1..15}; do
        if curl -s http://127.0.0.1:11434/api/tags &>/dev/null; then
            echo "  ${GREEN}✅ Ollama 服务已启动（等待 ${i}s）${NC}"
            break
        fi
        sleep 1
        if [[ $i -eq 15 ]]; then
            echo "  ${YELLOW}⚠️  Ollama 服务启动较慢，继续等待...${NC}"
        fi
    done
fi

# ---- 校验并拉取 llama3:8b 模型 ----
echo "  ${CYAN}⏳ 检查 llama3:8b 模型是否已拉取...${NC}"
# 检查模型是否已存在
MODEL_CHECK=$(ollama list 2>/dev/null | grep "llama3:8b" || true)
if [[ -n "$MODEL_CHECK" ]]; then
    echo "  ${GREEN}✅ llama3:8b 模型已存在，跳过下载${NC}"
else
    echo "  ${YELLOW}⚠️  llama3:8b 模型未拉取，开始下载（约 4.7GB，请耐心等待）...${NC}"
    ollama pull llama3:8b
    if [[ $? -ne 0 ]]; then
        echo "  ${RED}❌ llama3:8b 模型拉取失败！${NC}"
        echo "  ${YELLOW}修复方案：${NC}"
        echo "    1. 确认 Ollama 服务已启动: ollama serve"
        echo "    2. 手动拉取: ollama pull llama3:8b"
        echo "    3. 检查网络连接"
        exit 1
    fi
    echo "  ${GREEN}✅ llama3:8b 模型拉取完成${NC}"
fi

# ============================================================
# 步骤 5：创建项目工作文件夹 + 安装 Python 依赖
# ============================================================
echo ""
echo "${BOLD}${GREEN}【步骤 5/6】初始化项目环境...${NC}"

# ---- 5.1 创建工作文件夹 ----
echo "  ${CYAN}📁 创建项目工作文件夹...${NC}"
mkdir -p "$SCRIPT_DIR/Source_Videos"
mkdir -p "$SCRIPT_DIR/Temp_Clips"
mkdir -p "$SCRIPT_DIR/Output"
mkdir -p "$SCRIPT_DIR/static"
# TCC 权限校验目录（app.py 启动时会用到）
mkdir -p "$HOME/AutoClip_Factory/Source_Videos"
mkdir -p "$HOME/AutoClip_Factory/Output"
echo "  ${GREEN}✅ 工作文件夹已就绪 (Source_Videos / Temp_Clips / Output / static)${NC}"

# ---- 5.2 创建虚拟环境（如不存在）----
VENV_DIR="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    echo "  ${CYAN}🐍 创建 Python 虚拟环境...${NC}"
    python3 -m venv "$VENV_DIR"
    if [[ $? -ne 0 ]]; then
        echo "  ${RED}❌ 虚拟环境创建失败！${NC}"
        echo "  ${YELLOW}修复方案：手动执行 python3 -m venv .venv${NC}"
        exit 1
    fi
    echo "  ${GREEN}✅ 虚拟环境已创建${NC}"
else
    echo "  ${GREEN}✅ 虚拟环境已存在${NC}"
fi

# ---- 5.3 激活虚拟环境 ----
source "$VENV_DIR/bin/activate"
if [[ $? -ne 0 ]]; then
    echo "  ${RED}❌ 虚拟环境激活失败！${NC}"
    exit 1
fi

# ---- 5.4 使用清华 pip 镜像源安装依赖 ----
if [[ -f "$SCRIPT_DIR/requirements.txt" ]]; then
    echo "  ${CYAN}📦 安装 Python 依赖（使用清华镜像源加速）...${NC}"
    pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
    pip install -r "$SCRIPT_DIR/requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
    if [[ $? -ne 0 ]]; then
        echo "  ${RED}❌ Python 依赖安装失败！${NC}"
        echo "  ${YELLOW}修复方案：${NC}"
        echo "    1. 手动执行: source .venv/bin/activate && pip install -r requirements.txt"
        echo "    2. 如网络问题，尝试更换镜像源:"
        echo "       pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/"
        exit 1
    fi
    echo "  ${GREEN}✅ Python 依赖安装完成${NC}"
else
    echo "  ${RED}❌ 未找到 requirements.txt 文件！${NC}"
    echo "  ${YELLOW}请确认脚本位于项目根目录（与 app.py 同级）${NC}"
    exit 1
fi

# ============================================================
# 步骤 6：启动 Flask 主程序 + 打开浏览器
# ============================================================
echo ""
echo "${BOLD}${GREEN}【步骤 6/6】启动 AutoClipFactory 主程序...${NC}"
echo ""

# ---- 6.1 延迟 2 秒后打开浏览器 ----
(
    sleep 2
    echo "${CYAN}🌐 正在打开浏览器...${NC}"
    open "http://127.0.0.1:5000"
) &

# ---- 6.2 启动 Flask（前台运行，Ctrl+C 退出）----
echo "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "${BOLD}${GREEN}  ✅ 全部环境就绪！AutoClipFactory 已启动${NC}"
echo "${BOLD}${CYAN}  📎 访问地址: http://127.0.0.1:5000${NC}"
echo "${BOLD}${YELLOW}  ⚠️  首次转录会联网下载 faster-whisper 模型(约140MB)，请耐心等待${NC}"
echo "${BOLD}${CYAN}  🛑 按 Ctrl+C 可停止服务${NC}"
echo "${BOLD}${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# 设置 Ollama 低内存环境变量（v4.5 规范约束1.1，app.py 内部也会设置，此处双重保障）
export OLLAMA_LOW_VRAM=1

python3 app.py

# ---- Flask 退出后清理提示 ----
echo ""
echo "${YELLOW}AutoClipFactory 已停止运行。${NC}"
echo "${YELLOW}如需重新启动，双击 start.command 即可。${NC}"
