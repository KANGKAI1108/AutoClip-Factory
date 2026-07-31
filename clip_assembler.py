# -*- coding: utf-8 -*-
"""
AutoClip Factory - 阶段5：FFmpeg 高光片段裁剪拼接导出
功能：
  1. 读取 Temp_Clips 下 slice_candidates.json / slice_results_latest.json 内全部高光时间段
  2. 循环调用 FFmpeg，基于原视频裁剪每一段高光，输出临时分片至 Temp_Clips
  3. 生成 FFmpeg concat 拼接列表 txt，无损合并所有高光分片
  4. 成品 MP4 输出至 Finished_Clips 文件夹，命名：原视频名称_高光成片.mp4
  5. 合成结束校验文件，成功返回路径，失败捕获异常
  6. 自动清理临时分片视频（保留字幕、json 清单）

严格红线1：仅回收本类 subprocess.Popen 启动的 ffmpeg，严禁 pkill/killall/Ollama
禁止修改下载、转录、切片打分等已有逻辑
"""

import os
import re
import json
import time
import shutil
import signal
import threading
import subprocess
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List, Tuple


class ClipAssembler:
    """高光片段裁剪 + 拼接导出封装（纯新增模块，不改动已有代码）"""

    # ---------- 常量 ----------
    SINGLE_CUT_TIMEOUT = 300        # 单段裁剪最大时长（秒）
    CONCAT_TIMEOUT = 600            # 拼接最大时长（秒）
    COOL_DOWN_BETWEEN_CUTS = 0.5    # 每段裁剪间隔（秒，降 swap）

    def __init__(
        self,
        temp_clips_dir: Path,
        finished_clips_dir: Path,
        log_fn: Optional[Callable[[str, str], None]] = None,
        error_log_fn: Optional[Callable[[str], None]] = None,
        ffmpeg_path: str = "ffmpeg",
    ):
        """
        :param temp_clips_dir: 临时目录（Temp_Clips，用于读取 json + 存放临时分片）
        :param finished_clips_dir: 成品输出目录（Finished_Clips）
        :param log_fn: 主日志回调 fn(msg, level='INFO')
        :param error_log_fn: error.log 回调 fn(msg)
        :param ffmpeg_path: ffmpeg 可执行路径
        """
        self.temp_clips_dir = Path(temp_clips_dir)
        self.finished_clips_dir = Path(finished_clips_dir)
        self.finished_clips_dir.mkdir(parents=True, exist_ok=True)
        self._log = log_fn or (lambda m, l="INFO": None)
        self._errlog = error_log_fn or (lambda m: None)
        self._ffmpeg = ffmpeg_path
        self._cancel_event = threading.Event()

    def cancel(self):
        """外部取消信号"""
        self._cancel_event.set()

    # ============================================================
    #  内部：Popen 安全封装（仅登记本类子进程，遵守红线1）
    # ============================================================
    def _run_ffmpeg(self, cmd: List[str], timeout: float) -> Tuple[int, str]:
        """
        执行 ffmpeg 命令，返回 (returncode, stderr_tail)。
        超时自动 terminate→kill。仅操作本类 Popen 启动的进程。
        """
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            self._errlog(f"[ClipAssembler._run_ffmpeg] Popen fail: {type(e).__name__}: {e}")
            return -1, str(e)

        try:
            _, errs = proc.communicate(timeout=timeout)
            stderr_tail = (errs.decode("utf-8", errors="replace") or "")[-2000:]
            return proc.returncode, stderr_tail
        except subprocess.TimeoutExpired:
            self._log(f"[assembler] ffmpeg 超时({timeout}s)，强制终止（仅本类 Popen）", "WARN")
            try:
                proc.terminate()
                proc.communicate(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.communicate(timeout=5)
                except Exception as e:
                    self._errlog(f"[ClipAssembler._run_ffmpeg] kill fail: {e}")
            return -1, "timeout"
        except Exception as e:
            self._errlog(f"[ClipAssembler._run_ffmpeg] exception: {type(e).__name__}: {e}")
            try:
                proc.kill()
            except Exception:
                pass
            return -1, str(e)

    # ============================================================
    #  内部：从 Temp_Clips 读取高光时间段列表
    # ============================================================
    def _load_highlights(self) -> List[Dict[str, Any]]:
        """
        读取 Temp_Clips 下的切片清单 JSON，返回 [{start, end, text, index}, ...]
        优先读取 slice_results_latest.json（阶段3 写的汇总），其次 slice_candidates.json
        兼容多种结构：clips 数组 / candidates 数组 / 顶层数组
        """
        candidates: List[Dict[str, Any]] = []

        # 按优先级查找 JSON 文件
        json_files = [
            self.temp_clips_dir / "slice_results_latest.json",
            self.temp_clips_dir / "slice_candidates.json",
        ]
        # 也扫描子目录下的 score.json
        for sub in self.temp_clips_dir.iterdir():
            if sub.is_dir():
                sj = sub / "score.json"
                if sj.exists():
                    json_files.append(sj)

        for jf in json_files:
            if not jf.exists():
                continue
            try:
                with open(str(jf), "r", encoding="utf-8") as f:
                    obj = json.load(f)
                # 提取候选列表（兼容多种 key）
                raw_list = []
                if isinstance(obj, list):
                    raw_list = obj
                elif isinstance(obj, dict):
                    for key in ("clips", "candidates", "slice_candidates"):
                        v = obj.get(key)
                        if isinstance(v, list) and v:
                            raw_list = v
                            break

                if not raw_list:
                    continue

                # 标准化：确保有 start / end
                for i, item in enumerate(raw_list, start=1):
                    if not isinstance(item, dict):
                        continue
                    s = float(item.get("start") or 0.0)
                    e = float(item.get("end") or (s + 30.0))
                    if e <= s:
                        e = s + 30.0
                    candidates.append({
                        "index": int(item.get("index") or i),
                        "start": s,
                        "end": e,
                        "text": str(item.get("text") or ""),
                    })
                if candidates:
                    self._log(f"[assembler] 从 {jf.name} 读取 {len(candidates)} 条高光时间段", "INFO")
                    return candidates
            except Exception as e:
                self._errlog(f"[ClipAssembler._load_highlights] 读取 {jf} 失败: {e}")
                continue

        return candidates

    # ============================================================
    #  公共 API：主入口 —— 裁剪 + 拼接 + 导出
    # ============================================================
    def assemble(
        self,
        source_video: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        读取高光时间段 → 裁剪 → 拼接 → 输出成品到 Finished_Clips
        返回:
          {
            "ok": bool,
            "video_full_name": str,      # 成片完整文件名
            "video_save_dir": str,       # 成品统一存储目录（Finished_Clips）
            "video_absolute_path": str,  # 文件本地完整绝对路径
            "clip_count": int,           # 拼接段数
            "error": str,
          }
        """
        source_video = Path(source_video)
        progress_cb = progress_cb or (lambda p, s: None)

        ret: Dict[str, Any] = {
            "ok": False,
            "video_full_name": "",
            "video_save_dir": "",
            "video_absolute_path": "",
            "clip_count": 0,
            "error": "",
        }

        # 1. 校验源视频
        if not source_video.exists():
            ret["error"] = f"源视频不存在: {source_video}"
            self._log(f"[assembler] {ret['error']}", "ERROR")
            return ret

        # 2. 读取高光时间段
        progress_cb(1, "正在读取高光时间段清单")
        highlights = self._load_highlights()
        if not highlights:
            ret["error"] = "未找到高光切片清单（slice_results_latest.json / slice_candidates.json），请先完成转录与切片"
            self._log(f"[assembler] {ret['error']}", "ERROR")
            return ret

        total = len(highlights)
        self._log(f"[assembler] 开始裁剪拼接：源={source_video.name}，高光段数={total}", "INFO")

        # 3. 创建临时分片子目录
        safe_stem = "".join(c for c in source_video.stem if c.isalnum() or c in "-_ ").strip() or "video"
        cut_dir = self.temp_clips_dir / f"assemble_{safe_stem}_{int(time.time())}"
        cut_dir.mkdir(parents=True, exist_ok=True)

        cut_files: List[Path] = []
        temp_cut_paths: List[Path] = []  # 记录所有临时分片（用于清理）

        try:
            # 4. 循环裁剪每段高光
            for idx, hl in enumerate(highlights, start=1):
                if self._cancel_event.is_set():
                    ret["error"] = "用户取消"
                    break

                s = hl["start"]
                e = hl["end"]
                dur = e - s
                cut_name = f"cut_{idx:03d}.mp4"
                cut_path = cut_dir / cut_name

                pct = int(5 + 70 * (idx / total))
                msg = f"阶段5：裁剪高光 {idx}/{total} (s={s:.1f} e={e:.1f} dur={dur:.1f}s)"
                progress_cb(pct, msg)
                self._log(f"[assembler] {msg}", "INFO")

                # FFmpeg 裁剪：-ss 快速 seek + -t 时长 + -c copy 无损（速度优先）
                # 若 copy 失败则回退重编码
                ok = self._cut_segment(source_video, s, dur, cut_path)
                if not ok:
                    self._log(f"[assembler] 裁剪段 {idx} 失败，跳过（不中断）", "WARN")
                    if cut_path.exists():
                        try:
                            cut_path.unlink()
                        except Exception:
                            pass
                    continue

                cut_files.append(cut_path)
                temp_cut_paths.append(cut_path)

                # 分段冷却
                if self._cancel_event.is_set():
                    break
                time.sleep(self.COOL_DOWN_BETWEEN_CUTS)

            if self._cancel_event.is_set():
                ret["error"] = "用户取消"
                return ret

            if not cut_files:
                ret["error"] = "所有高光段裁剪均失败，无法拼接"
                self._log(f"[assembler] {ret['error']}", "ERROR")
                return ret

            # 5. 生成 concat 拼接列表
            progress_cb(78, "正在生成拼接列表并合并高光片段")
            concat_txt = cut_dir / "concat_list.txt"
            with open(concat_txt, "w", encoding="utf-8") as f:
                for cf in cut_files:
                    # FFmpeg concat demuxer 要求路径使用 / 且无特殊字符
                    safe_path = str(cf).replace("\\", "/")
                    f.write(f"file '{safe_path}'\n")

            # 6. FFmpeg concat 无损拼接
            # 命名规则：原视频名称_高光成片_时间戳.mp4（统一带时间戳，避免重名覆盖）
            ts_str = time.strftime("%Y%m%d_%H%M%S")
            output_name = f"{safe_stem}_高光成片_{ts_str}.mp4"
            output_path = self.finished_clips_dir / output_name
            # 极端情况同秒提交：若已存在则追加随机后缀
            if output_path.exists():
                import uuid as _uuid
                output_name = f"{safe_stem}_高光成片_{ts_str}_{_uuid.uuid4().hex[:4]}.mp4"
                output_path = self.finished_clips_dir / output_name

            self._log(f"[assembler] 拼接 {len(cut_files)} 段 → {output_path.name}", "INFO")
            concat_ok = self._concat_videos(concat_txt, output_path)
            if not concat_ok or not output_path.exists() or output_path.stat().st_size == 0:
                ret["error"] = f"FFmpeg 拼接失败，成品文件未生成"
                self._log(f"[assembler] {ret['error']}", "ERROR")
                return ret

            file_size_mb = round(output_path.stat().st_size / (1024 * 1024.0), 2)
            progress_cb(95, f"阶段5：成片导出完成 ({file_size_mb}MB)")
            self._log(
                f"[assembler] ✅ 高光成片导出完成: {output_path} ({file_size_mb}MB, {len(cut_files)}段)",
                "INFO"
            )

            ret["ok"] = True
            ret["video_full_name"] = output_name
            ret["video_save_dir"] = str(self.finished_clips_dir.resolve())
            ret["video_absolute_path"] = str(output_path.resolve())
            ret["clip_count"] = len(cut_files)
            progress_cb(100, "阶段5：高光成片导出完成")
            return ret

        except Exception as e:
            ret["error"] = f"{type(e).__name__}: {e}"
            self._log(f"[assembler] 异常: {ret['error']}", "ERROR")
            self._errlog(f"[ClipAssembler.assemble] exception: {ret['error']}")
            return ret
        finally:
            # 7. 自动清理临时分片视频（保留字幕、json 清单）
            try:
                if cut_dir.exists():
                    shutil.rmtree(cut_dir, ignore_errors=True)
                    self._log(f"[assembler] 临时分片目录已清理: {cut_dir.name}", "INFO")
            except Exception as e:
                self._errlog(f"[ClipAssembler] cleanup fail: {e}")

    # ============================================================
    #  内部：单段裁剪（先 copy，失败回退重编码）
    # ============================================================
    def _cut_segment(self, source: Path, start: float, duration: float, output: Path) -> bool:
        """裁剪单段高光，返回 True=成功"""
        if output.exists():
            try:
                output.unlink()
            except Exception:
                pass

        # 方案1：-c copy 无损（快速）
        cmd_copy = [
            self._ffmpeg, "-y", "-hide_banner", "-nostdin",
            "-ss", f"{start:.3f}",
            "-i", str(source),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
        rc, err = self._run_ffmpeg(cmd_copy, self.SINGLE_CUT_TIMEOUT)
        if rc == 0 and output.exists() and output.stat().st_size > 0:
            return True

        # 方案2：回退重编码（copy 失败时，可能关键帧不对齐）
        self._log(f"[assembler] copy 失败 rc={rc}，回退重编码", "DEBUG")
        if output.exists():
            try:
                output.unlink()
            except Exception:
                pass
        cmd_reenc = [
            self._ffmpeg, "-y", "-hide_banner", "-nostdin",
            "-ss", f"{start:.3f}",
            "-i", str(source),
            "-t", f"{duration:.3f}",
            "-pix_fmt", "yuv420p",
            "-c:v", "h264_videotoolbox",
            "-b:v", "5M",
            "-maxrate", "5M",
            "-bufsize", "10M",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output),
        ]
        rc2, err2 = self._run_ffmpeg(cmd_reenc, self.SINGLE_CUT_TIMEOUT)
        if rc2 == 0 and output.exists() and output.stat().st_size > 0:
            return True

        self._errlog(
            f"[ClipAssembler._cut_segment] cut fail s={start:.2f} dur={duration:.2f} "
            f"rc={rc2} stderr_tail:\n{err2[-1000:]}"
        )
        return False

    # ============================================================
    #  内部：concat 拼接（无损合并）
    # ============================================================
    def _concat_videos(self, concat_txt: Path, output: Path) -> bool:
        """使用 FFmpeg concat demuxer 无损拼接，返回 True=成功"""
        if output.exists():
            try:
                output.unlink()
            except Exception:
                pass
        cmd = [
            self._ffmpeg, "-y", "-hide_banner", "-nostdin",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_txt),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ]
        rc, err = self._run_ffmpeg(cmd, self.CONCAT_TIMEOUT)
        if rc != 0:
            # concat copy 可能因编码参数不一致失败，回退重编码
            self._log(f"[assembler] concat copy 失败 rc={rc}，回退重编码拼接", "WARN")
            if output.exists():
                try:
                    output.unlink()
                except Exception:
                    pass
            cmd_reenc = [
                self._ffmpeg, "-y", "-hide_banner", "-nostdin",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_txt),
                "-pix_fmt", "yuv420p",
                "-c:v", "h264_videotoolbox",
                "-b:v", "5M",
                "-maxrate", "5M",
                "-bufsize", "10M",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(output),
            ]
            rc, err = self._run_ffmpeg(cmd_reenc, self.CONCAT_TIMEOUT)
            if rc != 0:
                self._errlog(f"[ClipAssembler._concat_videos] concat fail rc={rc} stderr:\n{err[-1500:]}")
                return False
        return output.exists() and output.stat().st_size > 0
