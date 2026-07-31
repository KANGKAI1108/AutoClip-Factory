# -*- coding: utf-8 -*-
"""
AutoClip Factory - 阶段2：yt-dlp 视频下载封装类
严格约束：单线程、--buffer-size 512K、2GB上限、失败自动跳过写入error.log
           禁止 pkill Ollama / 禁止全局 kill，仅操作本类 subprocess.Popen 启动的子进程
"""

import os
import re
import sys
import time
import json
import signal
import shutil
import subprocess
import threading
from pathlib import Path
from datetime import datetime
from typing import Callable, Optional, Dict, Any


class YtDlpDownloader:
    """yt-dlp 下载器（macOS Apple Silicon 8GB 机型专用，优先稳定牺牲速度）"""

    # ---------- 常量 ----------
    # 单视频文件大小上限 2GB（严格与 Flask MAX_CONTENT_LENGTH 对齐）
    MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024 * 1024
    # yt-dlp 强制参数（单线程 + 512K 分片缓存，减少内存峰值）
    BASE_ARGS = [
        "--no-playlist",           # 禁止下载播放列表（单链接单视频）
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/mp4",  # 优先后缀MP4
        "--merge-output-format", "mp4",
        "--buffer-size", "512K",   # 分片缓存 512K，避免内存暴涨
        "--concurrent-fragments", "1",  # 强制单线程下载（牺牲速度，降内存）
        "--no-check-certificate",  # 容错：本地网络证书问题
        "--retries", "5",
        "--fragment-retries", "5",
        "--socket-timeout", "30",
        "--newline",               # 进度换行输出，便于解析
        # ---- 代理配置（国内访问YouTube SSL链路中断，必须开代理）----
        # 请替换为你的本地代理端口（常见：7890/1087/8080）
        "--proxy", "http://127.0.0.1:7890",
        # ---- extractor_args：YouTube解析强制使用web客户端，消除JS缺失警告 ----
        # 需配合 brew install deno 安装Deno运行环境，解决JS解析器缺失问题
        "--extractor-args", "youtube:player_client=web",
    ]
    # 代理检测目标站（3个常见视频站，任意1个可达即认为代理可用）
    PROBE_URLS = [
        "https://www.youtube.com",
        "https://www.bilibili.com",
        "https://vimeo.com",
    ]
    PROBE_TIMEOUT = 8  # 秒

    def __init__(
        self,
        output_dir: Path,
        log_fn: Optional[Callable[[str, str], None]] = None,
        register_pid_fn: Optional[Callable[[int], None]] = None,
        unregister_pid_fn: Optional[Callable[[int], None]] = None,
        error_log_fn: Optional[Callable[[str], None]] = None,
    ):
        """
        :param output_dir: 下载保存目录（按约束使用 ~/AutoClip_Factory/Source_Videos）
        :param log_fn: 写主日志回调  fn(msg, level='INFO')
        :param register_pid_fn: 登记子进程PID  fn(pid)  -> 对接 app.register_child_pid
        :param unregister_pid_fn: 移除PID登记  fn(pid)   -> 对接 app.unregister_child_pid
        :param error_log_fn: 写 error.log 回调  fn(msg)
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._log = log_fn or (lambda m, l="INFO": None)
        self._register_pid = register_pid_fn or (lambda pid: None)
        self._unregister_pid = unregister_pid_fn or (lambda pid: None)
        self._errlog = error_log_fn or (lambda m: None)

        # 内部状态
        self._proc: Optional[subprocess.Popen] = None
        self._cancel_event = threading.Event()
        self._pid_registered: Optional[int] = None
        self._lock = threading.Lock()  # 保护 _proc / cancel 操作

    # ============================================================
    #  公共工具 1: 启动时自动执行 yt-dlp -U 更新（失败写日志不抛异常）
    # ============================================================
    def self_update(self) -> bool:
        """
        执行 yt-dlp -U 自动更新
        返回 True=更新成功或无需更新，False=更新失败（不影响继续使用旧版）
        """
        self._log("[yt-dlp] 执行自更新 yt-dlp -U", "INFO")
        try:
            # 不登记 PID（更新是瞬态子进程，结束立即退出）
            proc = subprocess.Popen(
                [sys.executable, "-m", "yt_dlp", "-U"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            try:
                stdout, _ = proc.communicate(timeout=120)  # 最多等 2 分钟
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, _ = proc.communicate()
                self._log("[yt-dlp] 自更新超时（>120s），继续使用当前版本", "WARN")
                self._errlog(f"[YtDlpDownloader.self_update] timeout, stdout tail: {(stdout or '')[-300:]}")
                return False
            # 写日志最后 3 行
            lines = [ln for ln in (stdout or "").splitlines() if ln.strip()]
            for ln in lines[-3:]:
                self._log(f"[yt-dlp -U] {ln[:200]}", "INFO")
            if proc.returncode == 0:
                self._log("[yt-dlp] 自更新完成", "INFO")
                return True
            else:
                self._log(f"[yt-dlp] 自更新返回码 {proc.returncode}，继续使用当前版本（非致命）", "WARN")
                self._errlog(f"[YtDlpDownloader.self_update] rc={proc.returncode}, tail={(stdout or '')[-500:]}")
                return False
        except FileNotFoundError:
            self._log("[yt-dlp] 未安装 yt-dlp 模块，请先 pip install yt-dlp", "ERROR")
            self._errlog("[YtDlpDownloader.self_update] yt-dlp module not found (FileNotFoundError)")
            return False
        except Exception as e:
            self._log(f"[yt-dlp] 自更新异常: {e}", "ERROR")
            self._errlog(f"[YtDlpDownloader.self_update] exception: {type(e).__name__}: {e}")
            return False

    # ============================================================
    #  公共工具 2: 代理连通性检测（不触碰 Ollama）
    # ============================================================
    def check_proxy_connectivity(self) -> Dict[str, Any]:
        """
        前置代理连通检测
        返回: {
            "ok": bool,                  # True=至少一个目标站可达
            "reachable": [url,...],      # 可达的站点
            "unreachable": [url,...],    # 不可达的站点
            "detail": str                # 给前端的提示文字
        }
        """
        import urllib.request
        import urllib.error

        reachable, unreachable = [], []
        for url in self.PROBE_URLS:
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=self.PROBE_TIMEOUT) as r:
                    if 200 <= r.status < 500:
                        reachable.append(url)
                        continue
            except (urllib.error.URLError, TimeoutError, Exception):
                pass
            unreachable.append(url)
            # 只要有一个能通就 break 省时间
            if reachable:
                break

        ok = len(reachable) > 0
        detail = (
            "网络/代理正常" if ok
            else f"无法访问视频站点（已检测 {len(self.PROBE_URLS)} 个全部失败）。"
                 "请检查网络或代理；也可切换为【上传本地视频文件】模式继续使用。"
        )
        return {
            "ok": ok,
            "reachable": reachable,
            "unreachable": unreachable,
            "detail": detail,
        }

    # ============================================================
    #  内部辅助：输出模板文件名（纯 ASCII，避免 TCC 路径中文乱码）
    # ============================================================
    @staticmethod
    def _safe_stem(title: str) -> str:
        s = (title or "video").strip()
        s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s)
        s = re.sub(r"[^A-Za-z0-9_\-]", "", s)
        s = s.strip("_-")
        return s[:80] or "video"

    # ============================================================
    #  公共 API: 查询视频元信息（标题、预计大小、时长等），用于 2GB 拦截
    # ============================================================
    def probe_metadata(self, url: str) -> Dict[str, Any]:
        """
        查询视频元数据，不下载。
        返回 dict: {ok, title, filesize_bytes_est, duration_sec, error}
        """
        result = {"ok": False, "title": "", "filesize_bytes_est": 0, "duration_sec": 0, "error": ""}
        cmd = [
            sys.executable, "-m", "yt_dlp",
            "-j",                 # JSON dump
            "--no-playlist",
            "--skip-download",
            url,
        ]
        self._log(f"[yt-dlp] 查询元信息: {url[:80]}", "INFO")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, check=False
            )
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or "")[:500].strip().splitlines()
                result["error"] = msg[-1] if msg else f"returncode={proc.returncode}"
                self._log(f"[yt-dlp] 元信息查询失败: {result['error']}", "WARN")
                return result
            raw = (proc.stdout or "").strip()
            if not raw:
                result["error"] = "元信息为空"
                return result
            # 有些 yt-dlp 版本会输出多个 JSON 行，取第一条
            first_line = raw.splitlines()[0]
            info = json.loads(first_line)
            result["title"] = info.get("title", "") or ""
            result["duration_sec"] = int(info.get("duration") or 0)
            # 文件大小预估：filesize / filesize_approx / bitrate 兜底
            fs = info.get("filesize") or info.get("filesize_approx")
            if isinstance(fs, (int, float)) and fs > 0:
                result["filesize_bytes_est"] = int(fs)
            else:
                br = info.get("abr") or info.get("vbr") or 0
                if br and result["duration_sec"]:
                    # bitrate 单位通常 kbps -> 字节
                    result["filesize_bytes_est"] = int(float(br) * 1024 / 8 * result["duration_sec"])
            result["ok"] = True
            return result
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            self._log(f"[yt-dlp] 元信息查询异常: {result['error']}", "WARN")
            self._errlog(f"[YtDlpDownloader.probe_metadata] {result['error']} url={url[:100]}")
            return result

    # ============================================================
    #  公共 API: 取消当前下载（只杀本类 subprocess.Popen 启动的，不碰 Ollama）
    # ============================================================
    def cancel(self):
        """用户请求取消 / 任务释放时调用"""
        self._cancel_event.set()
        with self._lock:
            p = self._proc
            if p and p.poll() is None:
                try:
                    self._log(f"[yt-dlp] 用户取消：SIGTERM PID={p.pid}", "WARN")
                    os.kill(p.pid, signal.SIGTERM)
                    time.sleep(0.5)
                    if p.poll() is None:
                        self._log(f"[yt-dlp] 进程未响应 SIGTERM，SIGKILL PID={p.pid}", "WARN")
                        os.kill(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    self._errlog(f"[YtDlpDownloader.cancel] pid={p.pid if p else None} err={e}")

    # ============================================================
    #  进度解析：yt-dlp --newline 输出 [download]  12.3% ...
    # ============================================================
    _DL_PROGRESS_RE = re.compile(
        r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+(?:~?\s*([\d.]+)\s*([KMG]i?B))?"
    )

    def _parse_progress_line(self, line: str) -> Optional[int]:
        """返回 0-100 int 或 None"""
        m = self._DL_PROGRESS_RE.search(line or "")
        if not m:
            return None
        try:
            pct = float(m.group(1))
            return max(0, min(100, int(round(pct))))
        except ValueError:
            return None

    # ============================================================
    #  公共 API: 执行单链接下载（主入口）
    # ============================================================
    def download(
        self,
        url: str,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        下载单个视频
        :param url: 视频链接（http/https）
        :param progress_cb: fn(progress_pct_int: 0-100, stage_message: str)
        :return: {
            "ok": bool,
            "file_path": str or None,
            "file_size_bytes": int,
            "title": str,
            "duration_sec": int,
            "skipped": bool,   # True=失败后自动跳过（非致命错误）
            "error": str,
        }
        """
        ret = {
            "ok": False,
            "file_path": None,
            "file_size_bytes": 0,
            "title": "",
            "duration_sec": 0,
            "skipped": False,
            "error": "",
        }
        progress_cb = progress_cb or (lambda p, s: None)

        # ---- Step 1. 元信息 + 2GB 拦截 ----
        progress_cb(1, "正在查询视频元信息")
        meta = self.probe_metadata(url)
        if meta["ok"]:
            ret["title"] = meta["title"]
            ret["duration_sec"] = meta["duration_sec"]
            est_mb = round(meta["filesize_bytes_est"] / (1024 * 1024.0), 1)
            self._log(
                f"[yt-dlp] 预估：标题='{meta['title'][:60]}' "
                f"大小≈{est_mb}MB 时长≈{meta['duration_sec']}s", "INFO"
            )
            if meta["filesize_bytes_est"] > self.MAX_FILE_SIZE_BYTES:
                msg = (
                    f"文件预估 {est_mb}MB 超过上限 2GB。"
                    f"请使用 yt-dlp 手动分段下载（如 --download-sections），或上传本地分段文件。"
                )
                ret["error"] = msg
                ret["skipped"] = True
                self._log(f"[yt-dlp] {msg}", "ERROR")
                self._errlog(f"[YtDlpDownloader.download] oversized url={url[:100]} est={est_mb}MB")
                return ret
        else:
            # 元信息查询失败也允许继续尝试下载（有些站点拿不到预估值），大小限制在下载完成后再校验
            self._log("[yt-dlp] 元信息查询失败，直接尝试下载（下载完成后再校验2GB上限）", "WARN")

        # ---- Step 2. 组装 yt-dlp 命令 ----
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = self._safe_stem(ret["title"]) or "clip"
        outtmpl = str(self.output_dir / f"{safe}_{ts}.%(ext)s")
        cmd = [
            sys.executable, "-m", "yt_dlp",
            *self.BASE_ARGS,
            "-o", outtmpl,
            url,
        ]

        # ---- Step 3. 启动子进程（登记 PID，由 atexit / release_task 兜底回收）----
        progress_cb(3, "正在启动 yt-dlp 下载（单线程+512K缓存，优先稳定）")
        self._log(f"[yt-dlp] 启动下载，输出模板: {outtmpl}", "INFO")
        final_path: Optional[Path] = None
        try:
            with self._lock:
                if self._cancel_event.is_set():
                    ret["error"] = "下载已被取消"
                    return ret
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self._pid_registered = self._proc.pid
                self._register_pid(self._proc.pid)
                update_task_pid = None  # 由 app 层在外层通过 update_task_state 同步

            # 逐行读取 stdout，解析进度
            assert self._proc.stdout is not None
            last_pct = -1
            for raw_line in self._proc.stdout:
                if self._cancel_event.is_set():
                    break
                line = raw_line.strip()
                if not line:
                    continue
                # 写主日志（前 200 字符，避免爆炸）
                self._log(f"[yt-dlp] {line[:200]}", "DEBUG" if "ETA" in line or "%" in line else "INFO")
                pct = self._parse_progress_line(line)
                if pct is not None and pct != last_pct:
                    last_pct = pct
                    # 进度映射：下载阶段占总任务的 3% ~ 75%
                    mapped = 3 + int(pct * 0.72)
                    progress_cb(mapped, f"yt-dlp 下载中 {pct}%（单线程，内存占用低）")
                # 下载完成后 yt-dlp 输出的最终文件名行： [download] Destination: xxx.mp4 / Merging formats into...
                if "Merging formats into" in line or "Destination:" in line:
                    m = re.search(r'"([^"]+)"', line)
                    if m:
                        final_path = Path(m.group(1))
            # 等待子进程结束
            rc = self._proc.wait(timeout=60 * 60 * 2)  # 最多 2 小时兜底（8GB机型 大文件可能慢）
            self._log(f"[yt-dlp] 子进程退出 returncode={rc}", "INFO")
        except subprocess.TimeoutExpired:
            ret["error"] = "下载超过 2 小时上限，已终止"
            self._log(f"[yt-dlp] {ret['error']}", "ERROR")
            self._errlog(f"[YtDlpDownloader.download] timeout url={url[:100]}")
            self.cancel()
            ret["skipped"] = True
            return ret
        except Exception as e:
            ret["error"] = f"{type(e).__name__}: {e}"
            self._log(f"[yt-dlp] 下载异常: {ret['error']}", "ERROR")
            self._errlog(f"[YtDlpDownloader.download] exception url={url[:100]} err={ret['error']}")
            ret["skipped"] = True
            return ret
        finally:
            # 注销 PID 登记
            if self._pid_registered is not None:
                self._unregister_pid(self._pid_registered)
                self._pid_registered = None
            with self._lock:
                self._proc = None

        # ---- Step 4. 解析最终输出文件（yt-dlp 打印的目标文件 + 目录扫描兜底）----
        if (final_path is None or not final_path.exists()) and ret["title"]:
            # 兜底：按 output_dir 里最新的 mp4 文件
            candidates = sorted(
                [p for p in self.output_dir.glob(f"{safe}_{ts}*.mp4")],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if candidates:
                final_path = candidates[0]
        if final_path is None or not final_path.exists():
            # 再兜底：目录内近 5 分钟新建的 mp4
            now = time.time()
            cands2 = sorted(
                [p for p in self.output_dir.glob("*.mp4")
                 if now - p.stat().st_mtime < 5 * 60],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if cands2:
                final_path = cands2[0]

        if final_path is None or not final_path.exists():
            ret["error"] = "下载失败：未找到输出文件（可能站点不支持或链接失效，已自动跳过）"
            ret["skipped"] = True
            self._log(f"[yt-dlp] {ret['error']}", "ERROR")
            self._errlog(f"[YtDlpDownloader.download] no output file url={url[:100]} rc={rc if 'rc' in locals() else 'N/A'}")
            return ret

        # ---- Step 5. 2GB 硬校验（下载后的实际大小）----
        real_size = final_path.stat().st_size
        ret["file_size_bytes"] = real_size
        if real_size > self.MAX_FILE_SIZE_BYTES:
            msg = (
                f"实际文件大小 {round(real_size/1024/1024,1)}MB > 2GB 上限，按约束删除并自动跳过。"
                f"请手动分段后上传本地文件。"
            )
            ret["error"] = msg
            ret["skipped"] = True
            try:
                final_path.unlink()
                self._log(f"[yt-dlp] 已删除超大文件 {final_path.name}", "INFO")
            except Exception as e:
                self._errlog(f"[YtDlpDownloader.download] 删除超大文件失败: {e}")
            self._log(f"[yt-dlp] {msg}", "ERROR")
            self._errlog(f"[YtDlpDownloader.download] real_oversized size={real_size} file={final_path}")
            return ret

        # ---- Step 6. 成功 ----
        progress_cb(78, "yt-dlp 下载完成，正在销毁子进程缓存")
        # 销毁缓存：清理 yt-dlp 临时文件（.part / .ytdl 等）
        for pattern in ("*.part", "*.part-Frag*", "*.ytdl", "*.tmp"):
            for p in self.output_dir.glob(pattern):
                try:
                    p.unlink()
                except Exception:
                    pass
        progress_cb(80, "下载成功")
        ret["ok"] = True
        ret["file_path"] = str(final_path)
        ret["file_size_bytes"] = real_size
        ret["title"] = ret["title"] or final_path.stem
        self._log(
            f"[yt-dlp] ✅ 下载完成 {final_path.name} "
            f"({round(real_size/1024/1024,2)}MB)",
            "INFO"
        )
        return ret
