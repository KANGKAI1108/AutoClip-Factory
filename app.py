# -*- coding: utf-8 -*-
"""
AutoClip Factory - 本地AI高光剪辑工具 (阶段1：基础骨架)
技术栈：Python3 + Flask + faster-whisper + ffmpeg + yt-dlp + Ollama llama3:8b
适配硬件：macOS Apple Silicon 8GB 统一内存 MacBook Air
核心原则：优先稳定，牺牲速度；纯本地离线运行
"""

# ---- 阶段4前置强制：os.environ['OLLAMA_LOW_VRAM']='1' 必须在任何 import 之前（需求1.1）----
# 仅通过环境变量启用 Ollama 低内存模式；绝不通过 pkill/kill/CLI 干预 ollama serve 后台（红线1）
import os as _env_os
_env_os.environ.setdefault("OLLAMA_LOW_VRAM", "1")

import os
import sys
import io
import time
import json
import uuid
import threading
import platform
import subprocess
import signal
import atexit
from datetime import datetime
from pathlib import Path
# ---- 阶段2新增：模块延迟导入（避免未安装依赖时 Flask 启动失败）----
# downloader / transcriber 在需要时才 import，见各路由函数内的 lazy import。
# 这样即使 yt-dlp / faster-whisper 尚未安装，阶段1骨架仍可正常启动。
_DOWNLOADER_IMPORTED = False
_TRANSCRIBER_IMPORTED = False
_FFMPEG_PROC_IMPORTED = False
_MEMORY_WATCHER_IMPORTED = False
_OLLAMA_AGENT_IMPORTED = False

# ============================================================
# 0. 全局路径与基础常量定义
# ============================================================

# 项目根目录（app.py 所在目录）
BASE_DIR = Path(__file__).resolve().parent

# TCC权限校验目录：~/AutoClip_Factory（必须先定义，下面的子目录依赖它）
TCC_TEST_DIR = Path.home() / "AutoClip_Factory"
TCC_TEST_FILE = TCC_TEST_DIR / "test_write.tmp"

# 工作文件夹（按规范定义）
SOURCE_VIDEOS_DIR = BASE_DIR / "Source_Videos"   # 原始视频存放
TEMP_CLIPS_DIR = BASE_DIR / "Temp_Clips"         # 临时切片
OUTPUT_DIR = BASE_DIR / "Output"                 # 最终输出
FINISHED_CLIPS_DIR = BASE_DIR / "Finished_Clips" # 高光成片输出目录（阶段5新增）
STATIC_DIR = BASE_DIR / "static"                 # 静态资源

# ---- 阶段2新增：下载保存目录（按约束 1.3 要求）----
#   下载文件必须存入 ~/AutoClip_Factory/Source_Videos，而非项目根目录 Source_Videos。
#   同时保留项目根目录 Source_Videos 用于本地上传，两处统一可用。
TCC_SOURCE_VIDEOS_DIR = TCC_TEST_DIR / "Source_Videos"  # ~/AutoClip_Factory/Source_Videos
# ---- 阶段2新增：切片候选 / 字幕输出目录（与转录工作目录对应）----
#   转录产物（SRT + slice_candidates.json）统一写入 Temp_Clips（阶段4结束后统一清理）

ERROR_LOG_FILE = BASE_DIR / "error.log"          # 错误日志
APP_LOG_FILE = BASE_DIR / "app.log"              # 主日志（5MB轮转）
LOG_MAX_SIZE = 5 * 1024 * 1024                   # 单日志文件上限 5MB
LOG_MAX_LINES_FRONTEND = 200                     # 前端页面保留最新日志行数

# 系统检测阈值
FOREGROUND_MEM_THRESHOLD_MB = 1024               # 前台第三方进程总内存阈值 1GB (1024MB)

# 切片模式枚举
CLIP_MODES = ["auto", "manual"]                  # auto=自动AI切片，manual=手动切片

# ---- 阶段2新增：全局后台线程引用 + 取消钩子 ----
#   downloader / transcriber 都可被用户 / cancel_task / atexit 取消。
#   使用 dict 存放当前运行的下载器/转录器引用（线程安全 + 与阶段1 单任务互斥一致）
_pipeline_objects = {
    "downloader": None,   # YtDlpDownloader 实例引用（download_only 任务）
    "transcriber": None,  # FasterWhisperTranscriber 实例引用
    # ---- 阶段3新增 ----
    "ffmpeg_processor": None,  # FFmpegProcessor 实例引用（切片+scene打分）
    # ---- 阶段4新增 ----
    "memory_watcher": None,    # MemoryWatcher 实例引用（可中断的取消钩子）
    "ollama_agent": None,      # OllamaAgent 实例引用（只置 cancel_event，不碰 ollama serve 进程）
}
_pipeline_lock = threading.Lock()


# ============================================================
# 0.1 阶段4新增：断点续存缓存 key + ~/AutoClip_Factory/Output 目录
#     约束2：缓存 key = {文件名}_{文件字节大小}_{文件修改时间戳}（不使用哈希）
# ============================================================
TCC_OUTPUT_ROOT_DIR = TCC_TEST_DIR / "Output"   # ~/AutoClip_Factory/Output（需求4 打包输出根）
CACHE_ROOT_DIR = TEMP_CLIPS_DIR / ".clip_cache"  # 断点缓存目录
CLIP_TEMP_JSON_NAME = "clip_temp.json"          # clip_temp.json 缓存文件名（需求2）


def build_source_cache_key(source_video_path: Path) -> Optional[str]:
    """
    需求 2.1：缓存 key 拼接 {文件名}_{字节大小}_{mtime}
    文件不存在 → 返回 None，调用方应自动清空相关缓存
    """
    try:
        p = Path(source_video_path)
        if not p.exists():
            return None
        st = p.stat()
        return f"{p.name}_{st.st_size}_{int(st.st_mtime)}"
    except Exception:
        return None


def cleanup_stale_caches_if_needed(source_video_path: Optional[Path] = None):
    """
    需求 2.2：
      A. source_video 提供 → 特征(key)变更 → 自动删除 CACHE_ROOT_DIR 下 以旧文件名前缀开头的缓存
      B. 遍历 CACHE_ROOT_DIR 所有缓存，对应源视频已删除 → 自动清理废弃缓存（防止磁盘堆积）
    """
    try:
        CACHE_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    # B：全局清理（废弃源视频对应的缓存）
    try:
        for f in CACHE_ROOT_DIR.iterdir():
            if not f.is_file() or f.suffix != ".json":
                continue
            # 文件名格式：ollama_cache_<key>.json；解析 key -> file_name
            name = f.name
            if not name.startswith("ollama_cache_"):
                continue
            key_body = name[len("ollama_cache_"):-len(".json")] if name.endswith(".json") else name[len("ollama_cache_"):]
            # key_body = name_size_mtime → 第1段是 file_name（最后两个 _ 切出来 size/mtime）
            parts = key_body.rsplit("_", 2)
            if len(parts) < 3:
                continue
            src_name = parts[0]
            # 尝试在所有已知源视频目录里找这个文件
            candidates = [
                SOURCE_VIDEOS_DIR / src_name,
                TCC_SOURCE_VIDEOS_DIR / src_name,
            ]
            exists = any(c.exists() for c in candidates)
            if not exists:
                try:
                    f.unlink()
                    write_log(f"[cache_cleanup] 源视频已删除，自动清理废弃缓存 {f.name}", "INFO")
                except Exception:
                    pass
    except Exception as e:
        _write_error_log(f"[cleanup_stale_caches_if_needed] B global cleanup exception: {e}")

    # A：源视频 key 不一致 → 清该源的所有缓存
    if source_video_path is not None:
        try:
            p = Path(source_video_path)
            if not p.exists():
                return
            current_key = build_source_cache_key(p)
            if not current_key:
                return
            # 删除前缀为 "ollama_cache_" + file_stem + "_" 的非当前 key 缓存
            stem_part = p.name  # 文件名（带后缀）作为 key 第一段
            for f in CACHE_ROOT_DIR.iterdir():
                if not f.is_file() or not f.name.startswith("ollama_cache_") or f.suffix != ".json":
                    continue
                if f"ollama_cache_{current_key}.json" == f.name:
                    continue
                # 判断前缀是否为 ollama_cache_<stem_part>_（避免误删）
                if f.name.startswith(f"ollama_cache_{stem_part}_"):
                    try:
                        f.unlink()
                        write_log(f"[cache_cleanup] 源视频特征变更，自动删除旧缓存 {f.name}", "INFO")
                    except Exception:
                        pass
        except Exception as e:
            _write_error_log(f"[cleanup_stale_caches_if_needed] A per-source cleanup exception: {e}")

# ============================================================
# 1. 项目目录自动初始化
# ============================================================

def init_directories():
    """启动时自动创建项目所需的全部文件夹"""
    for _dir in [SOURCE_VIDEOS_DIR, TEMP_CLIPS_DIR, OUTPUT_DIR, FINISHED_CLIPS_DIR, STATIC_DIR,
                 TCC_TEST_DIR, TCC_SOURCE_VIDEOS_DIR, TCC_OUTPUT_ROOT_DIR, CACHE_ROOT_DIR]:
        try:
            _dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            # 目录创建失败写入 error.log
            _write_error_log(f"[init_directories] 创建目录失败 {_dir}: {str(e)}")

# ============================================================
# 2. 日志系统（增量读取 + 5MB轮转 + byte_offset 维护）
# ============================================================

# 日志文件读取偏移量（全局变量，仅读取新增内容）
_log_byte_offset = 0
_log_lock = threading.Lock()                    # 日志写锁
_app_log_handle = None                           # 当前日志文件句柄


def _write_error_log(msg: str):
    """写入 error.log（TCC/启动级错误专用，不受轮转控制）"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        # error.log 写入失败时降级到 stderr
        print(f"[ERROR_LOG_FAIL] {msg}", file=sys.stderr)


def _rotate_log_if_needed():
    """检查主日志文件大小，超过5MB则轮转备份"""
    global _app_log_handle
    if APP_LOG_FILE.exists() and APP_LOG_FILE.stat().st_size >= LOG_MAX_SIZE:
        try:
            # 关闭当前句柄
            if _app_log_handle and not _app_log_handle.closed:
                _app_log_handle.close()
            # 重命名为 .bak（覆盖旧备份）
            bak = APP_LOG_FILE.with_suffix(".log.bak")
            if bak.exists():
                bak.unlink()
            APP_LOG_FILE.rename(bak)
        except Exception as e:
            _write_error_log(f"[_rotate_log_if_needed] 日志轮转失败: {str(e)}")
        finally:
            # 重置偏移量，重新打开新日志文件
            global _log_byte_offset
            _log_byte_offset = 0
            _open_app_log()


def _open_app_log():
    """打开（或重新打开）主日志文件句柄，追加模式"""
    global _app_log_handle
    try:
        _app_log_handle = open(APP_LOG_FILE, "a", encoding="utf-8", buffering=1)  # 行缓冲
    except Exception as e:
        _write_error_log(f"[_open_app_log] 打开主日志失败: {str(e)}")
        _app_log_handle = None


def write_log(msg: str, level: str = "INFO"):
    """写入一行主日志（自动加时间戳、自动轮转）"""
    _rotate_log_if_needed()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}\n"
    with _log_lock:
        if _app_log_handle and not _app_log_handle.closed:
            try:
                _app_log_handle.write(line)
                _app_log_handle.flush()
            except Exception as e:
                _write_error_log(f"[write_log] 写入失败: {str(e)}; 原消息: {msg}")


def read_new_logs() -> list:
    """
    增量读取日志：仅返回自上次读取后的新增行
    维护全局 _log_byte_offset，避免重复读取
    返回 list[str]，每行不含末尾换行符
    """
    global _log_byte_offset
    new_lines = []
    if not APP_LOG_FILE.exists():
        return new_lines
    try:
        with open(APP_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            f.seek(_log_byte_offset)
            chunk = f.read()
            if chunk:
                new_lines = [ln for ln in chunk.split("\n") if ln]
                # 更新偏移量为当前文件末尾
                _log_byte_offset = f.tell()
    except Exception as e:
        _write_error_log(f"[read_new_logs] 增量读取失败: {str(e)}")
    return new_lines


def read_latest_logs_all(n: int = LOG_MAX_LINES_FRONTEND) -> list:
    """
    读取日志文件末尾 n 行（用于前端初次加载）
    返回 list[str]
    """
    if not APP_LOG_FILE.exists():
        return []
    try:
        # 从文件尾部倒推，读取足够的字节
        approx_bytes_per_line = 300
        read_bytes = min(n * approx_bytes_per_line, LOG_MAX_SIZE)
        file_size = APP_LOG_FILE.stat().st_size
        offset = max(0, file_size - read_bytes)
        with open(APP_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            content = f.read()
        lines = [ln for ln in content.split("\n") if ln]
        return lines[-n:]
    except Exception as e:
        _write_error_log(f"[read_latest_logs_all] 读取末尾失败: {str(e)}")
        return []

# ============================================================
# 3. 全局单任务互斥锁 + 任务状态字典
# ============================================================

# 互斥锁：同一时间仅允许 1 条任务运行
_task_mutex = threading.Lock()

# 全局任务状态字典（单例）
_task_state = {
    "task_id": None,        # 当前任务 UUID
    "status": "idle",       # idle / running / done / error
    "stage": "",            # 当前阶段描述文字（给前端看）
    "progress": 0,          # 0-100 整数进度
    "pid": None,            # 当前子进程 PID（yt-dlp / ffmpeg 等）
    "clip_mode": "auto",    # 当前切片模式 auto / manual
    "error_msg": "",        # 错误信息
    "start_time": None,     # 任务开始时间
    "end_time": None,       # 任务结束时间
    "video_full_name": "",      # 高光成片完整文件名（阶段5新增）
    "video_save_dir": "",       # 成品统一存储目录 Finished_Clips（阶段5新增）
    "video_absolute_path": "",  # 成片本地完整绝对路径（阶段5新增）
}
_state_lock = threading.Lock()  # 任务状态读写锁


def get_task_state_snapshot() -> dict:
    """线程安全地获取任务状态的副本（深拷贝）"""
    with _state_lock:
        return json.loads(json.dumps(_task_state))


def update_task_state(**kwargs):
    """线程安全地更新任务状态字段（仅更新传入的字段）"""
    with _state_lock:
        for k, v in kwargs.items():
            if k in _task_state:
                _task_state[k] = v


def try_acquire_task(task_id: str, clip_mode: str = "auto") -> bool:
    """
    尝试获取任务互斥锁
    返回 True=获取成功，False=已有任务在运行
    """
    if _task_mutex.acquire(blocking=False):
        with _state_lock:
            _task_state["task_id"] = task_id
            _task_state["status"] = "running"
            _task_state["stage"] = "任务初始化"
            _task_state["progress"] = 0
            _task_state["pid"] = None
            _task_state["clip_mode"] = clip_mode
            _task_state["error_msg"] = ""
            _task_state["start_time"] = datetime.now().isoformat()
            _task_state["end_time"] = None
            _task_state["video_full_name"] = ""
            _task_state["video_save_dir"] = ""
            _task_state["video_absolute_path"] = ""
        write_log(f"任务启动: task_id={task_id}, mode={clip_mode}")
        return True
    else:
        write_log(f"任务启动被拒绝：已有任务在运行", level="WARN")
        return False


def release_task(status: str = "done", error_msg: str = ""):
    """
    释放任务互斥锁，标记任务结束
    status: done / error
    """
    global _log_byte_offset
    with _state_lock:
        _task_state["status"] = status
        _task_state["stage"] = "任务结束" if status == "done" else "任务异常结束"
        _task_state["pid"] = None
        _task_state["error_msg"] = error_msg
        _task_state["end_time"] = datetime.now().isoformat()
        if status == "done":
            _task_state["progress"] = 100
    # 回收本脚本启动的 yt-dlp/ffmpeg 僵尸子进程（红线1：仅回收脚本 subprocess 启动的）
    _reap_our_children()
    try:
        _task_mutex.release()
    except RuntimeError:
        # 重复释放忽略
        pass
    write_log(f"任务结束: status={status}, error={error_msg if error_msg else '无'}",
              level=("ERROR" if status == "error" else "INFO"))


# ============================================================
# 4. 子进程回收（严格遵守红线1：仅回收本脚本 subprocess 启动的）
# ============================================================

# 记录本脚本通过 subprocess.Popen 启动的子进程 PID 集合
_our_child_pids = set()
_child_pids_lock = threading.Lock()


def register_child_pid(pid: int):
    """子进程启动后登记 PID（后续阶段下载/转码模块使用）"""
    if pid and pid > 0:
        with _child_pids_lock:
            _our_child_pids.add(pid)


def unregister_child_pid(pid: int):
    """子进程正常结束后从集合移除"""
    with _child_pids_lock:
        _our_child_pids.discard(pid)


def _reap_our_children():
    """
    回收本脚本启动的 yt-dlp / ffmpeg 残留子进程
    【红线1】严禁 pkill/kill Ollama 服务，仅清理我们自己 subprocess 启动的进程
    """
    with _child_pids_lock:
        pids_to_kill = list(_our_child_pids)
    for pid in pids_to_kill:
        try:
            # 使用 os.kill 发送 SIGTERM（温和终止），不使用 pkill/killall
            os.kill(pid, signal.SIGTERM)
            write_log(f"[子进程回收] 已发送 SIGTERM 给 PID={pid}")
            # 短暂等待后若仍存活则 SIGKILL
            time.sleep(0.3)
            try:
                os.kill(pid, 0)  # 检测进程是否存在
                os.kill(pid, signal.SIGKILL)
                write_log(f"[子进程回收] PID={pid} 未响应 SIGTERM，已 SIGKILL")
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            # 进程已不存在，正常
            with _child_pids_lock:
                _our_child_pids.discard(pid)
        except Exception as e:
            _write_error_log(f"[_reap_our_children] PID={pid} 回收失败: {str(e)}")
    # 清空集合
    with _child_pids_lock:
        _our_child_pids.clear()


# 程序退出时的兜底清理
@atexit.register
def _atexit_cleanup():
    """解释器退出时自动回收子进程 + 取消下载/转录 + 关闭日志句柄"""
    # ---- 阶段2新增：先取消 pipeline 内对象（取消内部 Popen，不碰 Ollama）----
    try:
        cancel_all_pipeline_objects(silent=True)
    except Exception:
        pass
    _reap_our_children()
    global _app_log_handle
    if _app_log_handle and not _app_log_handle.closed:
        try:
            _app_log_handle.close()
        except Exception:
            pass


# ============================================================
# 4.5 阶段2新增：pipeline 对象取消辅助函数（cancel_task / atexit 都用）
# ============================================================

def cancel_all_pipeline_objects(silent: bool = False):
    """
    取消当前登记的 downloader / transcriber / ffmpeg_processor / memory_watcher / ollama_agent。
    严格遵守红线1：只取消我们自己 Popen 启动的 yt-dlp / ffmpeg，Ollama 只取消本脚本侧 cancel_event，不碰 ollama serve 进程。
    """
    with _pipeline_lock:
        objs = dict(_pipeline_objects)
        _pipeline_objects["downloader"] = None
        _pipeline_objects["transcriber"] = None
        _pipeline_objects["ffmpeg_processor"] = None  # 阶段3新增
        _pipeline_objects["memory_watcher"] = None    # 阶段4新增
        _pipeline_objects["ollama_agent"] = None      # 阶段4新增
    for name, obj in objs.items():
        if obj is None:
            continue
        cancel_fn = getattr(obj, "cancel", None)
        if callable(cancel_fn):
            try:
                if not silent:
                    write_log(f"[pipeline_cancel] 正在取消 {name}", "WARN")
                cancel_fn()
            except Exception as e:
                _write_error_log(f"[cancel_all_pipeline_objects] {name}.cancel() 异常: {e}")


def set_pipeline_object(name: str, obj):
    """登记 downloader / transcriber / ffmpeg_processor / memory_watcher / ollama_agent 实例（线程安全）"""
    if name not in _pipeline_objects:
        return
    with _pipeline_lock:
        _pipeline_objects[name] = obj


def clear_pipeline_object(name: str):
    """清除登记实例"""
    if name not in _pipeline_objects:
        return
    with _pipeline_lock:
        _pipeline_objects[name] = None


# ============================================================
# 5. 启动前置全套系统检测模块
# ============================================================

def _run_cmd(cmd: list, timeout: int = 5) -> str:
    """
    安全执行 shell 命令，返回 stdout 字符串；失败返回空串
    不抛异常，统一在调用处处理
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False
        )
        return result.stdout or ""
    except Exception:
        return ""


def _get_macos_version() -> tuple:
    """获取 macOS 版本号，返回 (major, minor) 元组；非 macOS 返回 (0,0)"""
    if platform.system() != "Darwin":
        return (0, 0)
    try:
        ver = platform.mac_ver()[0]  # 例如 "15.1.2"
        parts = ver.split(".")
        major = int(parts[0]) if parts else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        return (major, minor)
    except Exception:
        return (0, 0)


def check_foreground_memory() -> dict:
    """
    4.1 扫描前台第三方进程总内存占用
    返回 dict: {
        "total_mb": 总占用MB,
        "over_threshold": bool 是否超过1GB,
        "top_processes": [{"name":..., "mb":...}, ...], 前5大
        "cleaned_count": 清理的闲置yt-dlp/ffmpeg残留进程数（非本脚本启动的）
    }
    说明：此处"清理残留"仅清理 idle 状态、非本脚本启动的 yt-dlp/ffmpeg；
         不触碰 Ollama / 其它系统服务（红线1）
    """
    result = {
        "total_mb": 0,
        "over_threshold": False,
        "top_processes": [],
        "cleaned_count": 0,
    }
    if platform.system() != "Darwin":
        # 非 macOS 环境跳过
        return result

    # 使用 ps 命令获取所有用户进程（非系统内核进程）的 RSS 和命令名
    # ps -axo pid,rss,comm  => RSS 单位 KB
    out = _run_cmd(["ps", "-axo", "pid=,rss=,comm="])
    if not out:
        return result

    our_pid = os.getpid()
    proc_list = []  # [(pid, rss_kb, comm)]
    # 排除的系统/自有进程关键字（不计入"前台第三方"）
    exclude_keywords = {
        "kernel", "launchd", "WindowServer", "coreaudiod",
        "configd", "syslogd", "mds", "mds_stores", "metadata",
        "ollama",         # 【红线1】Ollama 不统计、不杀
        "python",         # 自己（本 Flask 进程）不计入
        "Python",
        "sleep", "ps", "bash", "zsh", "sh", "Terminal", "iTerm2",
        "Finder", "Dock", "SystemUIServer", "ControlCenter",
        "loginwindow", "UserEventAgent", "DistributedNotificationCenter",
        "cfprefsd", "corespotlightd", "trustd", "securityd",
        "sandboxd", "mobileassetd", "installd", "syspolicyd",
    }
    # 本脚本登记的子进程也不计入（自己控制的）
    with _child_pids_lock:
        our_children = set(_our_child_pids)

    for line in out.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)  # pid rss comm
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
            comm = parts[2]
        except ValueError:
            continue
        # 跳过自己 & 本子进程
        if pid == our_pid or pid in our_children:
            continue
        # 跳过排除列表
        comm_base = os.path.basename(comm)
        skip = False
        for kw in exclude_keywords:
            if kw.lower() in comm_base.lower():
                skip = True
                break
        if skip:
            continue
        proc_list.append((pid, rss_kb, comm_base))

    # 计算总占用（MB）
    total_kb = sum(p[1] for p in proc_list)
    result["total_mb"] = round(total_kb / 1024.0, 1)
    result["over_threshold"] = result["total_mb"] > FOREGROUND_MEM_THRESHOLD_MB

    # 前 5 大进程
    proc_list_sorted = sorted(proc_list, key=lambda x: x[1], reverse=True)[:5]
    result["top_processes"] = [
        {"name": p[2], "mb": round(p[1] / 1024.0, 1)}
        for p in proc_list_sorted
    ]

    # 清理闲置的 yt-dlp / ffmpeg 残留（非本脚本启动的 idle 进程）
    # 仅清理命令名匹配且未登记在 our_children 中的进程
    cleaned = 0
    for pid, rss_kb, comm_base in proc_list:
        if pid in our_children:
            continue
        name_lower = comm_base.lower()
        if "yt-dlp" in name_lower or "ffmpeg" in name_lower:
            try:
                os.kill(pid, signal.SIGTERM)
                cleaned += 1
                write_log(f"[系统检测] 清理残留进程 {comm_base} (PID={pid}, {round(rss_kb/1024.0,1)}MB)")
            except Exception as e:
                _write_error_log(f"[check_foreground_memory] 清理 {comm_base} PID={pid} 失败: {str(e)}")
    result["cleaned_count"] = cleaned
    return result


def check_apple_intelligence() -> dict:
    """
    4.2 识别 macOS >= 15.1，检测 AppleIntelligence / Genmoji / WritingTools 后台进程
    返回 dict: {"is_sequoia_15_1": bool, "active_processes": [name,...], "total_mb": float}
    """
    result = {
        "is_sequoia_15_1": False,
        "active_processes": [],
        "total_mb": 0.0,
    }
    major, minor = _get_macos_version()
    # macOS 15 = Sequoia，要求 >= 15.1
    result["is_sequoia_15_1"] = (major == 15 and minor >= 1) or (major > 15)
    if not result["is_sequoia_15_1"]:
        return result
    if platform.system() != "Darwin":
        return result

    # 目标进程关键字（Apple Intelligence 相关）
    ai_keywords = [
        "AppleIntelligence",
        "Genmoji",
        "WritingTools",
        "WritingToolsUI",
        "AAClient",
        "AAUIService",
    ]
    out = _run_cmd(["ps", "-axo", "rss=,comm="])
    if not out:
        return result
    total_kb = 0
    matched = set()
    for line in out.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            rss_kb = int(parts[0])
            comm = parts[1]
        except ValueError:
            continue
        comm_base = os.path.basename(comm)
        for kw in ai_keywords:
            if kw.lower() in comm_base.lower():
                matched.add(comm_base)
                total_kb += rss_kb
                break
    result["active_processes"] = sorted(list(matched))
    result["total_mb"] = round(total_kb / 1024.0, 1)
    return result


def check_tcc_permission() -> dict:
    """
    4.3 磁盘读写权限自动校验：在 ~/AutoClip_Factory/ 写入 test_write.tmp
    返回 dict: {"ok": bool, "error_path": str 或 None, "detail": str}
    失败时同步写入 error.log
    """
    result = {
        "ok": False,
        "error_path": None,
        "detail": "",
    }
    # 先确保目录存在
    try:
        TCC_TEST_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        result["detail"] = f"创建TCC测试目录失败: {str(e)}"
        result["error_path"] = str(TCC_TEST_DIR)
        _write_error_log(f"[check_tcc_permission] {result['detail']} path={result['error_path']}")
        return result

    test_content = f"tcc_write_test_{datetime.now().isoformat()}_{uuid.uuid4().hex[:8]}\n"
    try:
        # 写入测试
        with open(TCC_TEST_FILE, "w", encoding="utf-8") as f:
            f.write(test_content)
        # 读取校验
        with open(TCC_TEST_FILE, "r", encoding="utf-8") as f:
            read_back = f.read()
        if read_back != test_content:
            raise IOError("写入内容与读取不一致")
        # 删除临时文件（失败不影响权限判定）
        try:
            TCC_TEST_FILE.unlink()
        except Exception:
            pass
        result["ok"] = True
        result["detail"] = "TCC 磁盘权限校验通过"
        return result
    except Exception as e:
        result["detail"] = f"TCC 写入/读取失败: {str(e)}"
        result["error_path"] = str(TCC_TEST_FILE)
        _write_error_log(f"[check_tcc_permission] {result['detail']} path={result['error_path']}")
        return result


def run_all_startup_checks() -> dict:
    """
    一次性运行全部启动前置检测
    返回聚合结果 dict
    """
    return {
        "foreground_memory": check_foreground_memory(),
        "apple_intelligence": check_apple_intelligence(),
        "tcc_permission": check_tcc_permission(),
        "macos_version": list(_get_macos_version()),
        "platform": platform.system(),
    }


# ============================================================
# 5.5 阶段2新增：下载/转录管道工具函数（对接互斥锁 + 日志 + PID登记）
# ============================================================

def _stage2_progress_cb(pct: int, stage_msg: str):
    """通用进度回调：写入 update_task_state（threaded Flask 多线程安全）"""
    pct_int = max(0, min(100, int(pct)))
    update_task_state(progress=pct_int, stage=stage_msg or "")


def _thread_download_then_transcribe(task_id: str, url: Optional[str] = None,
                                     local_video_path: Optional[Path] = None):
    """
    后台线程函数（Flask 多线程模型下的 worker）。
    工作流：
      A. 若有 url → yt-dlp 下载视频到 ~/AutoClip_Factory/Source_Videos；
         若有 local_video_path → 跳过下载，直接转录（上传模式）。
      B. faster-whisper 转录字幕 + 生成 30~60s 切片候选 JSON。
      C. 临时 wav 自动清理，临时文件在阶段4结束统一清空。
    任何异常 => release_task(error)。
    """
    video_path: Optional[Path] = Path(local_video_path) if local_video_path else None
    download_result = None
    transcribe_result = None

    try:
        # ---- 阶段 A 下载 ----
        if url is not None:
            update_task_state(stage="阶段2A：检查 yt-dlp 自更新 + 代理连通性", progress=1)
            # Lazy import（避免未安装时 Flask 启动崩溃）
            global _DOWNLOADER_IMPORTED
            if not _DOWNLOADER_IMPORTED:
                from downloader import YtDlpDownloader  # noqa: F401
                _DOWNLOADER_IMPORTED = True
            from downloader import YtDlpDownloader

            # 1. 构建下载器实例（日志 + PID 登记全部对接阶段1）
            dler = YtDlpDownloader(
                output_dir=TCC_SOURCE_VIDEOS_DIR,
                log_fn=write_log,
                register_pid_fn=register_child_pid,
                unregister_pid_fn=unregister_child_pid,
                error_log_fn=_write_error_log,
            )
            set_pipeline_object("downloader", dler)

            # 2. 自更新（失败不阻塞）
            dler.self_update()

            # 3. 代理连通检测（失败不阻塞，但写入日志+状态字段供前端提示）
            proxy_info = dler.check_proxy_connectivity()
            if not proxy_info["ok"]:
                write_log(
                    f"[阶段2A] 代理/网络连通检测失败: {proxy_info['detail']}。"
                    f"建议切换为【上传本地视频】模式。",
                    "WARN"
                )
                update_task_state(
                    stage="⚠️ 网络/代理不可达，正在重试下载；若持续失败请切换本地上传模式",
                    progress=2,
                )

            # 4. 执行下载（内部已含 2GB 上限校验 + 单线程 + 512K buffer）
            update_task_state(stage="阶段2A：yt-dlp 正在下载（单线程+512K缓存，稳定优先）", progress=3)
            download_result = dler.download(url, progress_cb=_stage2_progress_cb)

            # 下载失败自动跳过（约束 1.3）
            if not download_result.get("ok"):
                err = download_result.get("error") or "下载失败（已自动跳过）"
                write_log(f"[阶段2A] 下载失败，自动跳过: {err}", "ERROR")
                _write_error_log(
                    f"[_thread_download_then_transcribe] download fail url={str(url)[:120]} "
                    f"err={err}"
                )
                release_task(status="error", error_msg=err)
                return
            video_path = Path(download_result["file_path"])
            # 将下载后视频的 PID 同步给全局 task_state 展示
            update_task_state(pid=None)  # 下载子进程已结束，清空 PID 显示

        # 下载器用完及时清理引用（内部子进程已结束）
        clear_pipeline_object("downloader")

        # ---- 阶段 B 转录 ----
        if video_path is None or not video_path.exists():
            release_task(status="error", error_msg="下载后视频文件不存在，请切换本地上传模式重试")
            return

        update_task_state(stage="阶段2B：启动 faster-whisper 字幕提取", progress=80)
        # Lazy import
        global _TRANSCRIBER_IMPORTED
        if not _TRANSCRIBER_IMPORTED:
            from transcriber import FasterWhisperTranscriber  # noqa: F401
            _TRANSCRIBER_IMPORTED = True
        from transcriber import FasterWhisperTranscriber

        tr = FasterWhisperTranscriber(
            work_dir=TEMP_CLIPS_DIR,
            log_fn=write_log,
            error_log_fn=_write_error_log,
            ffmpeg_path="ffmpeg",
        )
        set_pipeline_object("transcriber", tr)

        transcribe_result = tr.transcribe(video_path, progress_cb=_stage2_progress_cb)

        # 转录完成/取消
        clear_pipeline_object("transcriber")
        if transcribe_result.get("canceled"):
            release_task(status="error", error_msg="转录被用户取消")
            return
        if not transcribe_result.get("ok"):
            err = transcribe_result.get("error") or "转录失败（已自动跳过）"
            write_log(f"[阶段2B] 转录失败: {err}", "ERROR")
            _write_error_log(
                f"[_thread_download_then_transcribe] transcribe fail "
                f"video={video_path} err={err}"
            )
            release_task(status="error", error_msg=err)
            return

        # ---- 完成 ----
        update_task_state(
            stage="阶段2完成：下载+转录完毕。SRT/切片候选JSON已写入Temp_Clips（FFmpeg切片+AI高光：阶段3/4实现）",
            progress=100,
        )
        write_log(
            "[阶段2] ✅ 全部完成："
            f"视频={video_path.name} | "
            f"原始片段={transcribe_result.get('segments_total')} | "
            f"完整台词={transcribe_result.get('sentences_total')} | "
            f"30~60s 切片候选={transcribe_result.get('slice_candidates_total')} | "
            f"字幕SRT={transcribe_result.get('srt_path')}",
            "INFO"
        )
        release_task(status="done", error_msg="")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _write_error_log(f"[_thread_download_then_transcribe] unhandled: {e}\n{tb}")
        write_log(f"[阶段2] 未捕获异常: {e}", "ERROR")
        try:
            release_task(status="error", error_msg=str(e)[:500])
        except Exception:
            pass
    finally:
        # 兜底：清空 pipeline 对象引用
        clear_pipeline_object("downloader")
        clear_pipeline_object("transcriber")
        clear_pipeline_object("ffmpeg_processor")  # 阶段3新增兜底


# ============================================================
#  阶段3新增：FFmpeg 批量切片 + scene 打分后台线程
# ============================================================

def _thread_slice_only(
    task_id: str,
    source_video: Path,
    candidates_json_path: Optional[Path] = None,
    candidates_override: Optional[List[Dict[str, Any]]] = None,
):
    """
    阶段3 后台线程：只跑 FFmpeg 切片 + scene 镜头打分。
    对接阶段2产物：slice_candidates.json（路径由 Temp_Clips 下按源视频名推断，或显式传入）
    也可直接接收 candidates_override（API 直接传列表）。
    切片模式：读取当前 task_state 的 clip_mode（auto=模式A -c copy，manual=模式B VideoToolbox 重编码）
    """
    source_video = Path(source_video)
    try:
        # 1) 确定 candidates
        candidates: List[Dict[str, Any]] = []
        if isinstance(candidates_override, list) and len(candidates_override) > 0:
            candidates = [c for c in candidates_override if isinstance(c, dict)]
        elif candidates_json_path is not None and Path(candidates_json_path).exists():
            try:
                import json as _json
                with open(str(candidates_json_path), "r", encoding="utf-8") as fj:
                    obj = _json.load(fj)
                if isinstance(obj, list):
                    candidates = [c for c in obj if isinstance(c, dict)]
                elif isinstance(obj, dict):
                    cands = obj.get("candidates") or obj.get("slice_candidates") or []
                    candidates = [c for c in cands if isinstance(c, dict)]
            except Exception as e:
                write_log(f"[阶段3] 读取切片候选JSON失败: {e}", "ERROR")
                _write_error_log(f"[_thread_slice_only] load json fail: {e}")
        else:
            # 自动推断：Temp_Clips / <source_stem>.slice_candidates.json（阶段2输出）
            auto_json = TEMP_CLIPS_DIR / f"{source_video.stem}.slice_candidates.json"
            if auto_json.exists():
                try:
                    import json as _json
                    with open(str(auto_json), "r", encoding="utf-8") as fj:
                        obj = _json.load(fj)
                    if isinstance(obj, list):
                        candidates = [c for c in obj if isinstance(c, dict)]
                    elif isinstance(obj, dict):
                        cands = obj.get("candidates") or obj.get("slice_candidates") or []
                        candidates = [c for c in cands if isinstance(c, dict)]
                    write_log(f"[阶段3] 自动加载切片候选: {auto_json} (共{len(candidates)}条)", "INFO")
                except Exception as e:
                    write_log(f"[阶段3] 自动加载候选失败 {auto_json}: {e}", "ERROR")

        if not candidates:
            release_task(status="error", error_msg="切片候选列表为空，请先完成转录后再切片。")
            return

        # 2) 读取切片模式
        st = get_task_state_snapshot()
        mode = st["clip_mode"] or "auto"
        mode_label = "A-无损复制(-c copy)" if mode == "auto" else "B-VideoToolbox重编码"
        write_log(f"[阶段3] 启动切片后台线程：模式={mode_label}，候选={len(candidates)}，源={source_video.name}", "INFO")
        update_task_state(stage=f"阶段3：FFmpeg切片+scene打分（模式{mode_label}，候选{len(candidates)}）", progress=2)

        # 3) Lazy import
        global _FFMPEG_PROC_IMPORTED
        if not _FFMPEG_PROC_IMPORTED:
            from ffmpeg_processor import FFmpegProcessor  # noqa: F401
            _FFMPEG_PROC_IMPORTED = True
        from ffmpeg_processor import FFmpegProcessor

        ffp = FFmpegProcessor(
            temp_clips_dir=TEMP_CLIPS_DIR,
            log_fn=write_log,
            register_pid_fn=register_child_pid,
            unregister_pid_fn=unregister_child_pid,
            error_log_fn=_write_error_log,
            ffmpeg_path="ffmpeg",
            ffprobe_path="ffprobe",
        )
        set_pipeline_object("ffmpeg_processor", ffp)

        result = ffp.slice_and_score(
            source_video=source_video,
            candidates=candidates,
            clip_mode=mode,
            progress_cb=_stage2_progress_cb,
        )
        clear_pipeline_object("ffmpeg_processor")

        if result.get("canceled"):
            release_task(status="error", error_msg="切片被用户取消")
            return
        if not result.get("ok"):
            err = result.get("error") or "切片失败"
            write_log(f"[阶段3] 切片失败: {err}", "ERROR")
            _write_error_log(f"[_thread_slice_only] fail: {err}")
            release_task(status="error", error_msg=err)
            return

        # 成功
        clips = result.get("clips") or []
        # 按分数降序排一下，供前端 task_state 字段展示
        clips_sorted = sorted(clips, key=lambda c: int(c.get("score") or 0), reverse=True)
        top_scores = [f"{Path(c.get('clip_path','')).name}:score={c.get('score')}" for c in clips_sorted[:5]]
        summary = (
            f"[阶段3] ✅ 切片+打分完成：成功{len(clips_sorted)}条 | "
            f"Top5分数 = {', '.join(top_scores)} | "
            f"score_txt={result.get('score_txt_path')} score_json={result.get('score_json_path')}"
        )
        write_log(summary, "INFO")
        # 把汇总（只读字段）写入 task_state 方便前端展示
        update_task_state(
            stage=f"阶段3完成：FFmpeg切片+scene打分完毕。成功 {len(clips_sorted)} 条，最高分 = {clips_sorted[0].get('score') if clips_sorted else 0}",
            progress=100,
        )
        release_task(status="done", error_msg="")

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _write_error_log(f"[_thread_slice_only] unhandled: {e}\n{tb}")
        write_log(f"[阶段3] 未捕获异常: {e}", "ERROR")
        try:
            release_task(status="error", error_msg=str(e)[:500])
        except Exception:
            pass
    finally:
        clear_pipeline_object("ffmpeg_processor")
        clear_pipeline_object("downloader")
        clear_pipeline_object("transcriber")
        clear_pipeline_object("memory_watcher")  # 阶段4兜底
        clear_pipeline_object("ollama_agent")    # 阶段4兜底


# ============================================================
#  阶段4新增：全流程后台线程（下载→转录→切片→AI→打包输出）
# ============================================================
def _thread_full_pipeline(
    task_id: str,
    url: Optional[str] = None,
    local_video_path: Optional[Path] = None,
):
    """
    阶段4全流程 worker：
      0. 初始化 MemoryWatcher + 取消事件 + 缓存清理
      1. 下载 or 上传 → video_path
      2. Whisper 转录（acquire_proc_class("whisper") → 步骤冷却30s）
      3. FFmpeg 切片+scene打分（acquire_proc_class("ffmpeg") → 步骤冷却30s）
      4. Ollama AI 推理（acquire_proc_class("ollama") + keep_alive:"0" + 每2s内存轮询 + 卸载15s校验+3.2GB不足120s休眠）
      5. 打包输出到 ~/AutoClip_Factory/Output/<日期_任务名>/：每个切片配套 mp4、details.json、details.txt、score.txt
      6. 全流程结束全局冷却180s（需求3.3）
    所有阶段单链路失败 → 写入日志 + 跳过当前子任务不中断整体；整条链路致命异常则 release_task(error)。
    """
    # 阶段级产物变量
    download_result = None
    transcribe_result = None
    slice_result = None
    ai_result = None
    output_session_dir: Optional[Path] = None

    # 共享取消事件（给 memory_watcher / ollama_agent / 其他类用）
    cancel_ev = threading.Event()

    def _pbar(pct: int, msg: str):
        _stage2_progress_cb(pct, msg)
        # 每隔一段时间跑一次 CPU 过载保护（需求3.2：连续3s>95%→60s休眠）
        if (pct % 10) == 0:
            try:
                mw_ref = getattr(_thread_full_pipeline, "_mw_ref", None)
                if mw_ref is not None:
                    mw_ref.cpu_overload_protect_if_needed()
            except Exception:
                pass

    try:
        # ===== 0. 初始化 =====
        # 0a. MemoryWatcher（注入共享 cancel_ev）
        global _MEMORY_WATCHER_IMPORTED
        if not _MEMORY_WATCHER_IMPORTED:
            from memory_watcher import MemoryWatcher  # noqa: F401
            _MEMORY_WATCHER_IMPORTED = True
        from memory_watcher import MemoryWatcher
        mw = MemoryWatcher(
            log_fn=write_log,
            error_log_fn=_write_error_log,
            cancel_event=cancel_ev,
        )
        set_pipeline_object("memory_watcher", mw)
        _thread_full_pipeline._mw_ref = mw  # type: ignore[attr-defined]

        # 0b. 提前把 pipeline cancel 映射到 cancel_ev（cancel_all_pipeline_objects 会调各对象 cancel）
        # 但 mw.cancel 不存在（MemoryWatcher 没有 cancel 方法）→ 用 wrapper 对象替代 set_pipeline_object 是安全的
        # 这里为 memory_watcher 做一个仅置 cancel_ev 的 cancel 适配器：
        class _MWCancelWrapper:
            def __init__(self, ev: threading.Event, real_mw: MemoryWatcher, real_log_fn):
                self._ev = ev
                self._mw = real_mw
                self._log = real_log_fn

            def cancel(self):
                self._log("[pipeline_cancel] MemoryWatcher 收到取消：置 cancel_ev", "WARN")
                self._ev.set()
                # 给 set_cancel_event 转一次，确保内部一致
                try:
                    self._mw.set_cancel_event(self._ev)
                except Exception:
                    pass

            def __getattr__(self, item):
                return getattr(self._mw, item)

        mw_wrap = _MWCancelWrapper(cancel_ev, mw, write_log)
        set_pipeline_object("memory_watcher", mw_wrap)

        update_task_state(stage="阶段4A：启动缓存失效机制（源视频特征/删除清理）", progress=0)
        # ===== 1. 下载 / 确定源视频 =====
        video_path: Optional[Path] = Path(local_video_path) if local_video_path else None
        if url is not None:
            update_task_state(stage="阶段4B：yt-dlp 下载（单线程+512K缓存+2GB拦截）", progress=1)
            global _DOWNLOADER_IMPORTED
            if not _DOWNLOADER_IMPORTED:
                from downloader import YtDlpDownloader  # noqa: F401
                _DOWNLOADER_IMPORTED = True
            from downloader import YtDlpDownloader
            dler = YtDlpDownloader(
                output_dir=TCC_SOURCE_VIDEOS_DIR,
                log_fn=write_log,
                register_pid_fn=register_child_pid,
                unregister_pid_fn=unregister_child_pid,
                error_log_fn=_write_error_log,
            )
            set_pipeline_object("downloader", dler)
            try:
                dler.self_update()
                proxy_info = dler.check_proxy_connectivity()
                if not proxy_info["ok"]:
                    write_log(f"[阶段4B] 代理检测失败: {proxy_info['detail']}", "WARN")
                    update_task_state(stage="⚠️ 网络不可达；若失败请切换上传模式", progress=2)
                download_result = dler.download(url, progress_cb=_pbar)
            finally:
                clear_pipeline_object("downloader")

            if not (download_result and download_result.get("ok")):
                err = (download_result or {}).get("error") or "下载失败（自动跳过整条链路）"
                write_log(f"[阶段4B] 下载失败: {err}", "ERROR")
                _write_error_log(f"[_thread_full_pipeline] download fail: {err}")
                release_task(status="error", error_msg=str(err)[:500])
                return
            video_path = Path(download_result["file_path"])

        if video_path is None or not video_path.exists():
            release_task(status="error", error_msg="未找到源视频（下载不存在/上传路径无效）")
            return

        # ===== 2. 缓存失效（需求2）：源视频变更/删除自动清理 =====
        try:
            cleanup_stale_caches_if_needed(source_video_path=video_path)
            # 再跑一次全局清理（B部分：所有废弃源视频对应缓存）
            cleanup_stale_caches_if_needed(source_video_path=None)
        except Exception as e:
            _write_error_log(f"[cache_cleanup] exception: {e}")

        # 源视频 key
        source_key = build_source_cache_key(video_path)
        if not source_key:
            write_log("[cache] 源视频无法获取缓存key，跳过缓存读写（无缓存仍可正常推理）", "WARN")

        # CPU 负载保护（每进入一个大阶段都跑一次；且在 _pbar 每10%进度也会触发）
        mw_wrap.cpu_overload_protect_if_needed()

        # ===== 3. Whisper 转录 =====
        # 全局互斥：Whisper、FFmpeg、Ollama 不同时占内存（需求3.1）
        if not mw_wrap.acquire_proc_class("whisper", timeout_sec=60.0):
            write_log("[阶段4C] 获取 whisper 进程类互斥锁超时/取消", "WARN")
        update_task_state(stage="阶段4C：faster-whisper 转录字幕+生成切片候选（base.en CPU float16 + VAD）", progress=10)
        global _TRANSCRIBER_IMPORTED
        if not _TRANSCRIBER_IMPORTED:
            from transcriber import FasterWhisperTranscriber  # noqa: F401
            _TRANSCRIBER_IMPORTED = True
        from transcriber import FasterWhisperTranscriber
        tr = FasterWhisperTranscriber(
            work_dir=TEMP_CLIPS_DIR,
            log_fn=write_log,
            error_log_fn=_write_error_log,
            ffmpeg_path="ffmpeg",
        )
        set_pipeline_object("transcriber", tr)
        try:
            transcribe_result = tr.transcribe(video_path, progress_cb=_pbar)
        finally:
            clear_pipeline_object("transcriber")
            try:
                mw_wrap.release_proc_class("whisper")
            except Exception:
                pass
        if cancel_ev.is_set():
            release_task(status="error", error_msg="用户取消（转录阶段）")
            return
        if not (transcribe_result and transcribe_result.get("ok")):
            err = (transcribe_result or {}).get("error") or "转录失败（批量容错：跳过AI/打包，提前结束）"
            write_log(f"[阶段4C] 转录失败: {err}", "ERROR")
            _write_error_log(f"[_thread_full_pipeline] transcribe fail: {err}")
            release_task(status="error", error_msg=str(err)[:500])
            return
        # 转录完成步骤冷却30s（需求3.3）
        mw_wrap.cool_down_step(30)

        # ===== 4. FFmpeg 切片 =====
        if not mw_wrap.acquire_proc_class("ffmpeg", timeout_sec=60.0):
            write_log("[阶段4D] 获取 ffmpeg 进程类互斥锁超时/取消", "WARN")
        st = get_task_state_snapshot()
        clip_mode = st["clip_mode"] or "auto"
        candidates = transcribe_result.get("slice_candidates") or []
        if not candidates:
            # 兜底：尝试读 Temp_Clips/<stem>.slice_candidates.json
            auto_json = TEMP_CLIPS_DIR / f"{video_path.stem}.slice_candidates.json"
            if auto_json.exists():
                try:
                    with open(auto_json, "r", encoding="utf-8") as fj:
                        obj = json.load(fj)
                    if isinstance(obj, list):
                        candidates = [c for c in obj if isinstance(c, dict)]
                    elif isinstance(obj, dict):
                        cands = obj.get("candidates") or obj.get("slice_candidates") or []
                        candidates = [c for c in cands if isinstance(c, dict)]
                except Exception as e:
                    _write_error_log(f"[_thread_full_pipeline] read candidates json fail: {e}")
        if not candidates:
            release_task(status="error", error_msg="转录阶段没有产出任何30~60s切片候选（请用3~5分钟英文素材重试）")
            return
        update_task_state(
            stage=f"阶段4D：FFmpeg切片+scene打分（模式{'A-c copy' if clip_mode=='auto' else 'B-VideoToolbox'}，共 {len(candidates)} 条候选）",
            progress=30,
        )
        global _FFMPEG_PROC_IMPORTED
        if not _FFMPEG_PROC_IMPORTED:
            from ffmpeg_processor import FFmpegProcessor  # noqa: F401
            _FFMPEG_PROC_IMPORTED = True
        from ffmpeg_processor import FFmpegProcessor
        ffp = FFmpegProcessor(
            temp_clips_dir=TEMP_CLIPS_DIR,
            log_fn=write_log,
            register_pid_fn=register_child_pid,
            unregister_pid_fn=unregister_child_pid,
            error_log_fn=_write_error_log,
            ffmpeg_path="ffmpeg",
            ffprobe_path="ffprobe",
        )
        set_pipeline_object("ffmpeg_processor", ffp)
        try:
            slice_result = ffp.slice_and_score(
                source_video=video_path,
                candidates=candidates,
                clip_mode=clip_mode,
                progress_cb=_pbar,
            )
        finally:
            clear_pipeline_object("ffmpeg_processor")
            try:
                mw_wrap.release_proc_class("ffmpeg")
            except Exception:
                pass
        if cancel_ev.is_set():
            release_task(status="error", error_msg="用户取消（切片阶段）")
            return
        clips_meta = (slice_result or {}).get("clips") or []
        if not (slice_result and slice_result.get("ok")) or not clips_meta:
            err = (slice_result or {}).get("error") or "切片无产物失败（批量容错：跳过AI/打包）"
            write_log(f"[阶段4D] 切片失败: {err}", "ERROR")
            _write_error_log(f"[_thread_full_pipeline] slice fail: {err}")
            release_task(status="error", error_msg=str(err)[:500])
            return
        # 切片完成步骤冷却30s（需求3.3）
        mw_wrap.cool_down_step(30)

        # ===== 5. Ollama AI 推理 =====
        if not mw_wrap.acquire_proc_class("ollama", timeout_sec=60.0):
            write_log("[阶段4E] 获取 ollama 进程类互斥锁超时/取消", "WARN")
        update_task_state(stage="阶段4E：Ollama llama3:8b Q4_K_M 文案推理（len//4 tokens + keep_alive:0 释放）", progress=55)
        global _OLLAMA_AGENT_IMPORTED
        if not _OLLAMA_AGENT_IMPORTED:
            from ollama_agent import OllamaAgent  # noqa: F401
            _OLLAMA_AGENT_IMPORTED = True
        from ollama_agent import OllamaAgent
        oll = OllamaAgent(
            endpoint="http://127.0.0.1:11434",
            model="llama3:8b",
            log_fn=write_log,
            error_log_fn=_write_error_log,
            memory_watcher=mw_wrap,
            cancel_event=cancel_ev,
            cache_dir=CACHE_ROOT_DIR,
        )
        set_pipeline_object("ollama_agent", oll)
        # 先探测（红线1：探测只发 HTTP，不杀不启）
        probe = oll.probe()
        if not probe.get("ok"):
            write_log(
                "[阶段4E] ⚠️ Ollama 探测失败：请先在 macOS 后台启动 ollama serve，并 `ollama pull llama3:8b`（Q4_K_M）。"
                "本步骤不中断整个流水线：后续直接跳过AI推理，空结果仍可打包。",
                "WARN"
            )
            _write_error_log(f"[OllamaAgent.probe] fail: {probe.get('reason')}")
        ai_results: List[Dict[str, Any]] = []
        try:
            if probe.get("ok"):
                # 需求1.3：模型加载阶段跳过3.2GB拦截（只在推理期间轮询；这里仅做可用内存提示）
                avail_before_mb = mw_wrap.get_available_bytes() / (1024 * 1024)
                write_log(f"[阶段4E] AI推理前可用内存≈ {avail_before_mb:.0f}MB（OLLAMA_LOW_VRAM=1）", "INFO")
                ai_result = oll.highlight_batch(
                    clips_meta=clips_meta,
                    cache_key=source_key,
                    progress_cb=_pbar,
                    request_timeout=1800,
                )
                ai_results = (ai_result or {}).get("results_normalized") or []
                partial_path = (ai_result or {}).get("partial_saved_path") or ""
                if partial_path:
                    write_log(f"[阶段4E] AI推理已保存部分结果: {partial_path}", "WARN")
                if cancel_ev.is_set():
                    release_task(status="error", error_msg="用户取消（AI推理阶段）")
                    return
                if not (ai_result and ai_result.get("ok")) and not ai_results:
                    err = (ai_result or {}).get("error") or "AI推理无返回，使用占位结果继续打包"
                    write_log(f"[阶段4E] AI推理异常: {err}，降级使用占位title/hook/tags（批量容错不中断）", "WARN")
                    _write_error_log(f"[_thread_full_pipeline] AI no-result: {err}")
            else:
                write_log("[阶段4E] Ollama不可用，跳过纯文本AI推理（需求5：不引入LLaVA视觉；仅保留scene分数）", "WARN")
        finally:
            clear_pipeline_object("ollama_agent")
            try:
                mw_wrap.release_proc_class("ollama")
            except Exception:
                pass

        # 需求1.3：卸载模型后15秒校验 → 不足3.2GB → 全局120s休眠
        mw_wrap.after_unload_cooldown(wait_1=15.0, required_bytes=int(3.2 * 1024 * 1024 * 1024),
                                      global_sleep_if_short=120.0)

        # AI 推理结束步骤冷却30s（需求3.3分层冷却）
        mw_wrap.cool_down_step(30)

        # ===== 6. 打包输出到 ~/AutoClip_Factory/Output/<日期_任务名>/ =====
        update_task_state(stage="阶段4F：打包输出（每个切片mp4+details.json+details.txt+score.txt）", progress=90)
        date_str = datetime.now().strftime("%Y%m%d")
        safe_stem = "".join(c for c in video_path.stem if c.isalnum() or c in "-_ ").strip() or "task"
        task_dir_name = f"{date_str}_{safe_stem}_{task_id[:6]}"
        output_session_dir = TCC_OUTPUT_ROOT_DIR / task_dir_name
        try:
            output_session_dir.mkdir(parents=True, exist_ok=False)
        except Exception as e:
            _write_error_log(f"[pack_output] mkdir fail {output_session_dir}: {e}")
            release_task(status="error", error_msg=f"创建输出目录失败: {e}")
            return
        # id -> ai result 映射
        ai_by_id = {int(r.get("id") or 0): r for r in ai_results if isinstance(r, dict)}
        packed_ok = 0
        for c in clips_meta:
            try:
                idx = int(c.get("index") or 0)
                clip_src = Path(c.get("clip_path") or "")
                score = int(c.get("score") or 0)
                start = float(c.get("start") or 0.0)
                end = float(c.get("end") or start + 30)
                text = str(c.get("text") or "")
                mode = str(c.get("mode") or "")
                if not clip_src.exists():
                    write_log(f"[pack_output] 切片文件不存在，跳过 idx={idx}: {clip_src}", "WARN")
                    continue
                out_clip_name = f"clip_{idx:03d}.mp4"
                out_score_name = f"clip_{idx:03d}_score.txt"
                out_details_json_name = f"clip_{idx:03d}_details.json"
                out_details_txt_name = f"clip_{idx:03d}_details.txt"
                # 拷贝 mp4（避免修改临时目录结构）
                dst_clip = output_session_dir / out_clip_name
                try:
                    import shutil as _shutil
                    _shutil.copy2(str(clip_src), str(dst_clip))
                except Exception as ce:
                    _write_error_log(f"[pack_output] copy clip fail idx={idx}: {ce}")
                    continue
                ai = ai_by_id.get(idx) or {
                    "id": idx,
                    "title": f"Clip {idx}",
                    "hook": "Watch this highlight moment now.",
                    "tags": [f"clip{idx}", "highlight", "shorts"],
                }
                title = str(ai.get("title") or f"Clip {idx}")[:50]
                hook = str(ai.get("hook") or "Watch this highlight.")
                tags_raw = ai.get("tags") or ["highlight", "shorts", "clip"]
                tags_list = [str(t)[:30] for t in tags_raw][:3]
                while len(tags_list) < 3:
                    tags_list.append(f"clip{idx}")
                # score.txt
                score_txt = output_session_dir / out_score_name
                with open(score_txt, "w", encoding="utf-8") as fs:
                    fs.write(f"index: {idx}\n")
                    fs.write(f"source_video: {video_path.name}\n")
                    fs.write(f"start_sec: {start:.3f}\n")
                    fs.write(f"end_sec: {end:.3f}\n")
                    fs.write(f"duration_sec: {(end-start):.3f}\n")
                    fs.write(f"clip_mode: {mode}\n")
                    fs.write(f"scene_score: {score}\n")
                    fs.write(f"scene_change_count: {int(c.get('scene_count') or score)}\n")
                # details.json
                details_json = output_session_dir / out_details_json_name
                with open(details_json, "w", encoding="utf-8") as fd:
                    json.dump(
                        {
                            "index": idx,
                            "source_video": str(video_path),
                            "clip_mp4": out_clip_name,
                            "timestamps_sec": {"start": round(start, 3), "end": round(end, 3)},
                            "scene": {"score": score, "scene_change_count": int(c.get("scene_count") or score)},
                            "transcript": text,
                            "ai": {
                                "title": title,
                                "hook": hook,
                                "tags": tags_list,
                            },
                        },
                        fd, ensure_ascii=False, indent=2,
                    )
                # details.txt（人类可读）
                details_txt = output_session_dir / out_details_txt_name
                with open(details_txt, "w", encoding="utf-8") as ft:
                    ft.write(f"=== Clip {idx:03d} Details ===\n")
                    ft.write(f"Timestamps: {start:.2f}s ~ {end:.2f}s  (duration {(end-start):.2f}s)\n")
                    ft.write(f"Scene score: {score} (scene changes {int(c.get('scene_count') or score)})\n")
                    ft.write(f"Slice mode: {mode}\n")
                    ft.write(f"Title: {title}\n")
                    ft.write(f"Hook: {hook}\n")
                    ft.write(f"SEO tags: {', '.join('#' + t.lower().replace(' ', '_') for t in tags_list)}\n\n")
                    ft.write(f"Transcript:\n{text}\n")
                packed_ok += 1
            except Exception as e:
                _write_error_log(f"[pack_output] clip error: {type(e).__name__}: {e}")
                write_log(f"[阶段4F] 打包单切片失败，跳过: {e}", "ERROR")
                continue
        if packed_ok == 0:
            release_task(status="error", error_msg=f"打包输出0条成功（请检查Temp_Clips）")
            return

        # ===== 6.5 阶段5：FFmpeg 高光裁剪 + 拼接导出成品 MP4 =====
        update_task_state(stage="阶段5：FFmpeg 高光裁剪拼接导出成品", progress=92)
        write_log("[阶段5] 启动高光成片合成（ClipAssembler）", "INFO")
        try:
            from clip_assembler import ClipAssembler  # noqa: F401
            assembler = ClipAssembler(
                temp_clips_dir=TEMP_CLIPS_DIR,
                finished_clips_dir=FINISHED_CLIPS_DIR,
                log_fn=write_log,
                error_log_fn=_write_error_log,
                ffmpeg_path="ffmpeg",
            )
            asm_result = assembler.assemble(
                source_video=video_path,
                progress_cb=_progress_cb,
            )
            if asm_result.get("ok") and asm_result.get("video_absolute_path"):
                v_full_name = asm_result.get("video_full_name") or ""
                v_save_dir = asm_result.get("video_save_dir") or ""
                v_abs_path = asm_result.get("video_absolute_path") or ""
                write_log(
                    f"[阶段5] ✅ 高光成片导出成功: {v_full_name} ({asm_result.get('clip_count', 0)}段拼接)",
                    "INFO"
                )
                # 写入任务状态供前端展示与下载
                update_task_state(
                    video_full_name=v_full_name,
                    video_save_dir=v_save_dir,
                    video_absolute_path=v_abs_path,
                )
            else:
                asm_err = asm_result.get("error") or "未知错误"
                write_log(f"[阶段5] 高光成片合成失败: {asm_err}（任务标记完成但无成片）", "WARN")
                _write_error_log(f"[_thread_full_pipeline] assemble fail: {asm_err}")
                # 不中断主流程，任务仍标记 done（切片已打包到 Output 目录）
        except Exception as asm_e:
            write_log(f"[阶段5] 高光成片合成异常: {asm_e}", "ERROR")
            _write_error_log(f"[_thread_full_pipeline] assemble exception: {type(asm_e).__name__}: {asm_e}")

        # ===== 7. 全流程结束，全局冷却180s（需求3.3） =====
        update_task_state(
            stage=f"阶段4完成：全流程成功 {packed_ok} 条 → {output_session_dir}。进入全局冷却180s（swap保护）",
            progress=98,
        )
        summary_line = (
            f"[阶段4F] ✅ 打包输出完成：目录={output_session_dir}，成功打包 {packed_ok}/{len(clips_meta)} 条切片；"
            f"AI成功={len(ai_results)} 条；OLLAMA_LOW_VRAM=1；keep_alive:0 已通过API释放；"
            f"cache_key={source_key or '(无)'}"
        )
        write_log(summary_line, "INFO")
        mw_wrap.cool_down_full(180)
        release_task(status="done", error_msg="")
        return

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _write_error_log(f"[_thread_full_pipeline] unhandled: {e}\n{tb}")
        write_log(f"[阶段4] 未捕获异常: {e}", "ERROR")
        try:
            release_task(status="error", error_msg=str(e)[:500])
        except Exception:
            pass
        return
    finally:
        # 兜底取消事件
        cancel_ev.set()
        clear_pipeline_object("downloader")
        clear_pipeline_object("transcriber")
        clear_pipeline_object("ffmpeg_processor")
        clear_pipeline_object("memory_watcher")
        clear_pipeline_object("ollama_agent")


# ============================================================
# 6. Flask 主服务与路由
# ============================================================

from flask import Flask, render_template_string, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")

# 上传文件大小限制 2GB（后续阶段上传视频用）
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024


@app.route("/")
def index():
    """首页：渲染 index.html"""
    index_path = BASE_DIR / "index.html"
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        return render_template_string(html)
    except Exception as e:
        _write_error_log(f"[index] 读取 index.html 失败: {str(e)}")
        return f"<h1 style='color:red'>index.html 加载失败: {str(e)}</h1>", 500


@app.route("/api/startup_check", methods=["GET"])
def api_startup_check():
    """前端轮询/页面加载时调用，获取全套系统检测结果"""
    write_log("[API] 执行启动系统检测")
    result = run_all_startup_checks()
    return jsonify({"code": 0, "data": result, "msg": "ok"})


@app.route("/api/task_state", methods=["GET"])
def api_task_state():
    """获取当前任务状态快照"""
    return jsonify({"code": 0, "data": get_task_state_snapshot()})


@app.route("/api/download_clip", methods=["GET"])
def api_download_clip():
    """
    下载高光成片 MP4
    Query: 无（直接读取当前任务状态的 video_absolute_path）
    返回：文件流（Content-Disposition: attachment）或错误 JSON
    """
    from flask import send_file
    snap = get_task_state_snapshot()
    vpath = snap.get("video_absolute_path") or ""
    if not vpath:
        return jsonify({"code": 1, "msg": "未生成剪辑视频（video_absolute_path 为空）"}), 404
    p = Path(vpath)
    if not p.exists() or p.stat().st_size == 0:
        return jsonify({"code": 2, "msg": f"成片文件不存在或为空: {p.name}"}), 404
    try:
        return send_file(
            str(p.resolve()),
            as_attachment=True,
            download_name=p.name,
            mimetype="video/mp4",
        )
    except Exception as e:
        _write_error_log(f"[api_download_clip] send_file fail: {e}")
        return jsonify({"code": 3, "msg": f"下载失败: {e}"}), 500


@app.route("/api/open_folder", methods=["GET"])
def api_open_folder():
    """
    打开 Finished_Clips 文件夹（Mac 访达）
    读取当前任务状态 video_save_dir，调用 macOS open 命令打开访达
    """
    import subprocess as _sp
    snap = get_task_state_snapshot()
    save_dir = snap.get("video_save_dir") or ""
    if not save_dir:
        return jsonify({"code": 1, "msg": "未生成剪辑视频（video_save_dir 为空）"}), 404
    d = Path(save_dir)
    if not d.exists():
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return jsonify({"code": 2, "msg": f"目录不存在且创建失败: {e}"}), 500
    try:
        # macOS 调用访达打开目录
        _sp.Popen(["open", str(d.resolve())])
        return jsonify({"code": 0, "msg": f"已打开文件夹: {d}"})
    except Exception as e:
        _write_error_log(f"[api_open_folder] open fail: {e}")
        return jsonify({"code": 3, "msg": f"打开文件夹失败: {e}"}), 500


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """
    增量获取日志
    Query 参数:
      full=1  => 返回最近 LOG_MAX_LINES_FRONTEND 行（初次加载）
      否则    => 返回自上次调用后新增的行
    """
    full = request.args.get("full", "0") == "1"
    if full:
        lines = read_latest_logs_all(LOG_MAX_LINES_FRONTEND)
    else:
        lines = read_new_logs()
    # 前端最多 200 行保护
    if len(lines) > LOG_MAX_LINES_FRONTEND:
        lines = lines[-LOG_MAX_LINES_FRONTEND:]
    return jsonify({"code": 0, "data": {"lines": lines, "total": len(lines)}})


@app.route("/api/set_clip_mode", methods=["POST"])
def api_set_clip_mode():
    """
    设置切片模式
    POST body JSON: {"mode": "auto" | "manual"}
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        mode = body.get("mode", "auto")
        if mode not in CLIP_MODES:
            return jsonify({"code": -1, "msg": f"mode 必须是 {CLIP_MODES}"}), 400
        # 任务运行中不允许切换模式
        st = get_task_state_snapshot()
        if st["status"] == "running":
            return jsonify({"code": -2, "msg": "任务运行中，禁止切换切片模式"}), 409
        update_task_state(clip_mode=mode)
        write_log(f"[API] 切片模式已切换为: {mode}")
        return jsonify({"code": 0, "msg": "ok", "data": {"clip_mode": mode}})
    except Exception as e:
        _write_error_log(f"[api_set_clip_mode] 异常: {str(e)}")
        return jsonify({"code": -99, "msg": str(e)}), 500


@app.route("/api/submit_link", methods=["POST"])
def api_submit_link():
    """
    提交下载链接（阶段2：抢占互斥锁 → 后台线程执行 下载+转录）
    实际下载通过 yt-dlp subprocess 启动，PID 自动登记到阶段1集合中统一回收。
    POST JSON: {"url": "..."}
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        url = (body.get("url") or "").strip()
        if not url:
            return jsonify({"code": -1, "msg": "链接不能为空"}), 400
        # 仅允许 http/https
        if not (url.startswith("http://") or url.startswith("https://")):
            return jsonify({"code": -2, "msg": "仅支持 http/https 链接"}), 400

        task_id = uuid.uuid4().hex
        st = get_task_state_snapshot()
        mode = st["clip_mode"] or "auto"

        if not try_acquire_task(task_id, clip_mode=mode):
            return jsonify({"code": -3, "msg": "已有任务在运行，请等待结束或刷新页面"}), 409

        # ---- 阶段2新增：启动后台线程（下载→转录串行）----
        write_log(f"[API] 提交链接任务（后台线程执行 下载+转录）: {url[:80]}{'...' if len(url)>80 else ''}")
        update_task_state(stage="阶段2A：正在启动后台线程（yt-dlp + faster-whisper）", progress=0)
        t = threading.Thread(
            target=_thread_download_then_transcribe,
            args=(task_id, url, None),
            name=f"autoclip-link-{task_id[:8]}",
            daemon=True,
        )
        t.start()

        return jsonify({
            "code": 0,
            "msg": "阶段2：任务已提交到后台线程（下载+转录），请通过 /api/task_state + /api/logs 轮询。",
            "data": {
                "task_id": task_id,
                "url": url,
                "clip_mode": mode,
                "save_dir": str(TCC_SOURCE_VIDEOS_DIR),
                "subtitle_dir": str(TEMP_CLIPS_DIR),
            },
        })
    except Exception as e:
        _write_error_log(f"[api_submit_link] 异常: {str(e)}")
        # 异常兜底释放锁
        try:
            release_task(status="error", error_msg=str(e))
        except Exception:
            pass
        return jsonify({"code": -99, "msg": str(e)}), 500


@app.route("/api/upload_video", methods=["POST"])
def api_upload_video():
    """
    本地视频上传（阶段2：抢占互斥锁 + 保存到 Source_Videos → 后台线程执行转录）
    允许格式：mp4 / mov / mkv / webm / avi
    """
    ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
    try:
        if "file" not in request.files:
            return jsonify({"code": -1, "msg": "未找到上传文件字段 file"}), 400
        f = request.files["file"]
        if not f or not f.filename:
            return jsonify({"code": -2, "msg": "文件名为空"}), 400
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return jsonify({"code": -3, "msg": f"仅支持扩展名: {sorted(list(ALLOWED_EXT))}"}), 400

        task_id = uuid.uuid4().hex
        st = get_task_state_snapshot()
        mode = st["clip_mode"] or "auto"

        if not try_acquire_task(task_id, clip_mode=mode):
            return jsonify({"code": -4, "msg": "已有任务在运行，请等待结束或刷新页面"}), 409

        # 保存到项目根目录 Source_Videos（上传用；下载另存 TCC_SOURCE_VIDEOS_DIR）
        safe_stem = "".join(
            c for c in Path(f.filename).stem if c.isalnum() or c in "-_ "
        ).strip() or "video"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_name = f"{safe_stem}_{ts}{ext}"
        save_path = SOURCE_VIDEOS_DIR / save_name
        update_task_state(stage="正在保存上传视频到 Source_Videos", progress=5)

        # 分块保存避免大文件内存问题
        try:
            with open(save_path, "wb") as out:
                while True:
                    chunk = f.stream.read(8 * 1024 * 1024)  # 8MB 块
                    if not chunk:
                        break
                    out.write(chunk)
        except Exception as e:
            release_task(status="error", error_msg=f"保存上传文件失败: {str(e)}")
            _write_error_log(f"[api_upload_video] 保存失败 {save_path}: {str(e)}")
            return jsonify({"code": -5, "msg": f"保存文件失败: {str(e)}"}), 500

        # 2GB 硬校验（上传文件也按 2GB 上限统一约束）
        real_size = save_path.stat().st_size
        MAX_2GB = 2 * 1024 * 1024 * 1024
        if real_size > MAX_2GB:
            try:
                save_path.unlink()
            except Exception:
                pass
            msg = f"文件 {round(real_size/1024/1024,1)}MB 超过 2GB 上限，请手动分段后再上传。"
            release_task(status="error", error_msg=msg)
            _write_error_log(f"[api_upload_video] oversized size={real_size} name={save_name}")
            return jsonify({"code": -6, "msg": msg}), 413

        file_size_mb = round(real_size / (1024 * 1024.0), 2)
        write_log(f"[API] 上传视频已保存: {save_path} ({file_size_mb}MB)，即将启动转录后台线程")

        # ---- 阶段2新增：启动后台线程（仅转录）----
        update_task_state(stage="阶段2：上传完成，启动 faster-whisper 后台转录线程", progress=10)
        t = threading.Thread(
            target=_thread_download_then_transcribe,
            args=(task_id, None, save_path),
            name=f"autoclip-upload-{task_id[:8]}",
            daemon=True,
        )
        t.start()

        return jsonify({
            "code": 0,
            "msg": "阶段2：文件上传成功，已启动后台线程执行转录，请通过 /api/task_state 轮询。",
            "data": {
                "task_id": task_id,
                "file_name": save_name,
                "file_path": str(save_path),
                "size_mb": file_size_mb,
                "clip_mode": mode,
                "subtitle_dir": str(TEMP_CLIPS_DIR),
            },
        })
    except Exception as e:
        _write_error_log(f"[api_upload_video] 异常: {str(e)}")
        try:
            release_task(status="error", error_msg=str(e))
        except Exception:
            pass
        return jsonify({"code": -99, "msg": str(e)}), 500


@app.route("/api/cancel_task", methods=["POST"])
def api_cancel_task():
    """
    取消当前任务（阶段2：取消 pipeline 对象 + 释放互斥锁 + 回收本子进程）
    """
    st = get_task_state_snapshot()
    if st["status"] != "running":
        return jsonify({"code": -1, "msg": "当前无运行中任务"}), 400
    write_log("[API] 用户请求取消任务，正在中断 downloader/transcriber/ffmpeg_processor（不碰 Ollama）", level="WARN")
    # 1) 先取消 downloader / transcriber / ffmpeg_processor 对象内部的 yt-dlp/ffmpeg 子进程
    cancel_all_pipeline_objects(silent=False)
    # 2) 释放互斥锁 + 回收 PID 集合中的子进程（严格红线1：仅回收我们自己启动的）
    release_task(status="error", error_msg="用户手动取消任务")
    return jsonify({"code": 0, "msg": "任务已取消，yt-dlp/ffmpeg 子进程已回收（不碰 Ollama）"})


# ============================================================
#  阶段3新增：启动切片（ffmpeg 切片+scene打分）路由
# ============================================================
@app.route("/api/run_slicing", methods=["POST"])
def api_run_slicing():
    """
    阶段3：运行 FFmpeg 批量切片 + scene 滤镜镜头打分。
    入参（三选一，优先级从高到低）：
      1) POST JSON {"source_video": "<绝对路径或相对路径>", "candidates": [ {start,end,text}, ... ] }
         → 直接用传入的 candidates 切片
      2) POST JSON {"source_video": "...", "candidates_json_path": "<path>"}
         → 读取 candidates_json_path 指定的 JSON 文件切片
      3) POST JSON {"source_video": "..."}（只给源视频路径）
         → 自动读取 Temp_Clips/<source_stem>.slice_candidates.json（阶段2产物）切片
    切片模式：使用 task_state 中用户上次设置的 clip_mode（auto/manual，默认 auto）
    返回：立即启动后台线程，返回 HTTP 200。通过 /api/task_state + /api/logs 轮询。
    """
    try:
        body = request.get_json(force=True, silent=True) or {}
        src = body.get("source_video")
        if not src:
            return jsonify({"code": -1, "msg": "缺少 source_video 字段（源视频绝对/相对路径）"}), 400
        src_path = Path(str(src)).expanduser()
        if not src_path.exists():
            # 尝试相对 BASE_DIR / SOURCE_VIDEOS_DIR / TCC_SOURCE_VIDEOS_DIR 解析
            for _base in [Path.cwd(), BASE_DIR, SOURCE_VIDEOS_DIR, TCC_SOURCE_VIDEOS_DIR]:
                t = _base / str(src)
                if t.exists():
                    src_path = t
                    break
        if not src_path.exists():
            return jsonify({"code": -2, "msg": f"源视频不存在: {src}"}), 404

        # 抢占互斥锁
        task_id = uuid.uuid4().hex
        st = get_task_state_snapshot()
        mode = st["clip_mode"] or "auto"
        if not try_acquire_task(task_id, clip_mode=mode):
            return jsonify({"code": -3, "msg": "已有任务在运行，请等待结束或刷新页面"}), 409

        # 两种候选传参方式
        candidates_override = body.get("candidates") if isinstance(body.get("candidates"), list) else None
        cjp = body.get("candidates_json_path")
        candidates_json_path: Optional[Path] = None
        if isinstance(cjp, str) and cjp:
            candidates_json_path = Path(cjp).expanduser()

        mode_label = "A-无损复制(-c copy)" if mode == "auto" else "B-VideoToolbox重编码"
        write_log(f"[API] 提交切片任务：源={src_path.name}，模式={mode_label}，候选=直接传{len(candidates_override) if candidates_override else 'JSON文件/自动推断'}")
        update_task_state(stage="阶段3：启动切片后台线程（FFmpeg + scene 滤镜）", progress=0)
        t = threading.Thread(
            target=_thread_slice_only,
            args=(task_id, src_path, candidates_json_path, candidates_override),
            name=f"autoclip-slice-{task_id[:8]}",
            daemon=True,
        )
        t.start()

        return jsonify({
            "code": 0,
            "msg": "阶段3：任务已提交到后台线程（FFmpeg切片+scene打分），请轮询 /api/task_state + /api/logs。",
            "data": {
                "task_id": task_id,
                "source_video": str(src_path),
                "clip_mode": mode,
                "temp_clips_dir": str(TEMP_CLIPS_DIR),
                "note_candidates_direct": candidates_override is not None,
                "note_candidates_json_path": str(candidates_json_path) if candidates_json_path else "",
                "note_candidates_auto": not candidates_override and not candidates_json_path,
            },
        })
    except Exception as e:
        _write_error_log(f"[api_run_slicing] 异常: {str(e)}")
        try:
            release_task(status="error", error_msg=str(e))
        except Exception:
            pass
        return jsonify({"code": -99, "msg": str(e)}), 500


# ============================================================
#  阶段4新增：全流程启动路由（下载/上传→转录→切片→AI→打包）
# ============================================================
@app.route("/api/run_full_pipeline", methods=["POST"])
def api_run_full_pipeline():
    """
    阶段4：启动端到端全流程（Ollama + 打包输出）。
    三选一入参（优先级同 submit_link / upload_video）：
      1) {"url": "https://..."} → 先阶段2A下载，再后续所有流程
      2) {"source_video": "路径"} → 直接从本地视频开始（转录→切片→AI→打包）
      3) multipart POST 含字段 file=上传视频 → 保存后启动全流程（与 /api/upload_video 同样的2GB校验）
    切片模式使用 task_state.clip_mode（auto/manual）；输出目录 ~/AutoClip_Factory/Output/<日期_任务名>/
    """
    ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
    try:
        task_id = uuid.uuid4().hex
        st = get_task_state_snapshot()
        mode = st["clip_mode"] or "auto"

        # 抢占互斥锁（阶段1全局单任务互斥）
        if not try_acquire_task(task_id, clip_mode=mode):
            return jsonify({"code": -1, "msg": "已有任务在运行，请等待结束或刷新页面"}), 409

        url = None
        local_video_path: Optional[Path] = None

        # 方式A：JSON url
        if request.is_json:
            body = request.get_json(force=True, silent=True) or {}
            url = (body.get("url") or "").strip() or None
            src = body.get("source_video")
            if not url and src:
                src_path = Path(str(src)).expanduser()
                if not src_path.exists():
                    for _base in [Path.cwd(), BASE_DIR, SOURCE_VIDEOS_DIR, TCC_SOURCE_VIDEOS_DIR]:
                        t = _base / str(src)
                        if t.exists():
                            src_path = t
                            break
                if not src_path.exists():
                    release_task(status="error", error_msg=f"源视频不存在: {src}")
                    return jsonify({"code": -2, "msg": f"源视频不存在: {src}"}), 404
                local_video_path = src_path
            if not url and not local_video_path:
                release_task(status="error", error_msg="必须提供 url 或 source_video 其中之一（JSON）")
                return jsonify({"code": -3, "msg": "必须提供 url 或 source_video"}), 400
            if url and not (url.startswith("http://") or url.startswith("https://")):
                release_task(status="error", error_msg="仅支持 http/https url")
                return jsonify({"code": -4, "msg": "仅支持 http/https url"}), 400
        else:
            # 方式B：multipart 上传文件（与 /api/upload_video 同样的2GB校验）
            if "file" not in request.files:
                release_task(status="error", error_msg="缺少字段 file 或 JSON body")
                return jsonify({"code": -5, "msg": "缺少上传文件字段 file 或 JSON body(url/source_video)"}), 400
            f = request.files["file"]
            if not f or not f.filename:
                release_task(status="error", error_msg="空文件字段")
                return jsonify({"code": -6, "msg": "文件名为空"}), 400
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_EXT:
                release_task(status="error", error_msg=f"仅支持扩展名 {sorted(list(ALLOWED_EXT))}")
                return jsonify({"code": -7, "msg": f"仅支持扩展名 {sorted(list(ALLOWED_EXT))}"}), 400
            safe_stem = "".join(c for c in Path(f.filename).stem if c.isalnum() or c in "-_ ").strip() or "video"
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_name = f"{safe_stem}_{ts}{ext}"
            save_path = SOURCE_VIDEOS_DIR / save_name
            update_task_state(stage="阶段4：正在保存上传视频", progress=2)
            try:
                with open(save_path, "wb") as out:
                    while True:
                        chunk = f.stream.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            except Exception as e:
                release_task(status="error", error_msg=f"保存上传文件失败: {e}")
                _write_error_log(f"[api_run_full_pipeline] save fail {save_path}: {e}")
                return jsonify({"code": -8, "msg": f"保存文件失败: {e}"}), 500
            # 2GB 硬校验
            real_size = save_path.stat().st_size
            MAX_2GB = 2 * 1024 * 1024 * 1024
            if real_size > MAX_2GB:
                try:
                    save_path.unlink()
                except Exception:
                    pass
                msg = f"上传文件 {round(real_size/1024/1024,1)}MB 超过 2GB，请手动分段。"
                release_task(status="error", error_msg=msg)
                _write_error_log(f"[api_run_full_pipeline] oversized size={real_size} name={save_name}")
                return jsonify({"code": -9, "msg": msg}), 413
            file_size_mb = round(real_size / (1024 * 1024.0), 2)
            write_log(f"[阶段4] 上传视频已保存，即将启动全流程: {save_path} ({file_size_mb}MB)")
            local_video_path = save_path

        # 启动后台线程
        trigger = (f"url={url[:80]}" if url else f"local={str(local_video_path)[:120]}")
        write_log(f"[阶段4] 提交全流程任务（后台线程）task_id={task_id}，触发源：{trigger}，clip_mode={mode}")
        update_task_state(stage=f"阶段4：启动端到端全流程线程（OLLAMA_LOW_VRAM=1）clip_mode={mode}", progress=0)
        t = threading.Thread(
            target=_thread_full_pipeline,
            args=(task_id, url, local_video_path),
            name=f"autoclip-full-{task_id[:8]}",
            daemon=True,
        )
        t.start()
        return jsonify({
            "code": 0,
            "msg": "阶段4：全流程任务已提交到后台线程（下载→转录→切片→AI→打包）。请轮询 /api/task_state + /api/logs。",
            "data": {
                "task_id": task_id,
                "clip_mode": mode,
                "output_root": str(TCC_OUTPUT_ROOT_DIR),
                "cache_root": str(CACHE_ROOT_DIR),
                "trigger_url": url,
                "trigger_source_video": str(local_video_path) if local_video_path else "",
                "note_env": {"OLLAMA_LOW_VRAM": os.environ.get("OLLAMA_LOW_VRAM", "")},
                "note_model": "llama3:8b (keep_alive:0 API body, 不碰 ollama serve 进程 —— 红线1)",
            },
        })
    except Exception as e:
        _write_error_log(f"[api_run_full_pipeline] 异常: {str(e)}")
        try:
            release_task(status="error", error_msg=str(e))
        except Exception:
            pass
        return jsonify({"code": -99, "msg": str(e)}), 500


# ============================================================
# 7. 主入口：初始化 + 启动 Flask
# ============================================================

def main():
    # 7.1 初始化目录
    init_directories()

    # 7.2 打开主日志句柄
    _open_app_log()

    # 7.3 启动日志 banner
    write_log("=" * 60)
    write_log("AutoClip Factory 启动 - 阶段4：全流程端到端（下载+转录+切片+Ollama+打包输出，全部v4.5内存约束已落地）")
    write_log(f"平台: {platform.system()} {platform.mac_ver()[0] if platform.system()=='Darwin' else platform.release()}")
    write_log(f"Python: {sys.version.split()[0]}")
    write_log(f"OLLAMA_LOW_VRAM (env)= {os.environ.get('OLLAMA_LOW_VRAM', '')!r}（需求1.1 强制启用低显存模式）")
    write_log(f"项目根目录: {BASE_DIR}")
    write_log(f"下载保存目录（yt-dlp）: {TCC_SOURCE_VIDEOS_DIR}")
    write_log(f"字幕/切片候选/切片临时目录: {TEMP_CLIPS_DIR}")
    write_log(f"项目根 Output: {OUTPUT_DIR}")
    write_log(f"打包输出目录（TCC路径）: {TCC_OUTPUT_ROOT_DIR}")
    write_log(f"断点缓存 clip_temp/ollama_cache 根: {CACHE_ROOT_DIR}")

    # 7.4 启动时先跑一次全套检测（结果写入日志，供前端稍后拉取）
    checks = run_all_startup_checks()
    write_log(f"[启动检测] 前台进程内存合计: {checks['foreground_memory']['total_mb']}MB "
              f"(阈值 {FOREGROUND_MEM_THRESHOLD_MB}MB) "
              f"{'⚠️ 超阈值' if checks['foreground_memory']['over_threshold'] else '✅正常'}")
    write_log(f"[启动检测] 清理残留 yt-dlp/ffmpeg 进程数: {checks['foreground_memory']['cleaned_count']}")
    if checks["apple_intelligence"]["is_sequoia_15_1"]:
        write_log(f"[启动检测] macOS>15.1 AppleIntelligence 活跃进程: "
                  f"{checks['apple_intelligence']['active_processes'] or '无'}, "
                  f"内存 {checks['apple_intelligence']['total_mb']}MB")
    tcc = checks["tcc_permission"]
    if tcc["ok"]:
        write_log(f"[启动检测] TCC 磁盘权限: ✅ 通过")
    else:
        write_log(f"[启动检测] TCC 磁盘权限: ❌ 失败 - {tcc['detail']} path={tcc['error_path']}", level="ERROR")
        # 失败时一定写入 error.log（check_tcc_permission 内部已写，此处冗余保险）
        _write_error_log(f"[启动检测] TCC 磁盘权限失败: {tcc['detail']} path={tcc['error_path']}")

    # 7.5 启动 Flask（严格遵守规范：localhost + 5000 + 多线程）
    write_log("Flask 启动：http://127.0.0.1:5000 (threaded=True)")
    try:
        app.run(host="127.0.0.1", port=5000, threaded=True, use_reloader=False)
    except Exception as e:
        _write_error_log(f"[main] Flask 启动异常: {str(e)}")
        raise


if __name__ == "__main__":
    main()
