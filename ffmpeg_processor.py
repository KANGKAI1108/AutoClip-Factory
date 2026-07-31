# -*- coding: utf-8 -*-
"""
AutoClip Factory - 阶段3：FFmpeg 视频切片 + scene 滤镜镜头打分工具类
适配 macOS Apple Silicon 8GB 机型：
  * 双切片模式：
    - 模式A（默认快速预览，clip_mode="auto"）：-c copy -threads 2，允许±5秒时间误差，无损复制低负载
    - 模式B（成品重编码，clip_mode="manual"）：
      -pix_fmt yuv420p -c:v h264_videotoolbox -b:v 5M -maxrate 5M -bufsize 10M -threads 2 -c:a aac -b:a 128k
      作用：解决 M 芯片 VideoToolbox 偶发 Segmentation fault 闪退 / 画面马赛克问题
  * 镜头高光打分：切片完成后对每个 clip 调用 ffmpeg scene 滤镜，统计镜头切换频次（pts_time 行数），
    切换次数越多分数越高，写入对应切片目录下的 score.txt 以及 score.json
  * 临时缓存管理：切片文件统一存入 Temp_Clips/<source_stem>_<rand>/，单条源视频全部处理完成
    自动清空该子目录（保留 score 汇总到 Temp_Clips/slice_results.json 给后续阶段）
  * 严格红线1：仅回收本类 subprocess.Popen 启动的 ffmpeg，严禁 pkill / killall / 碰 Ollama
禁止编写 Ollama 大模型推理代码（阶段4实现）
"""

import os
import re
import gc
import sys
import time
import json
import uuid
import shutil
import signal
import threading
import subprocess
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List, Tuple


# 镜头切换解析正则：匹配 stderr 中 showinfo 输出的 pts_time:
_SCENE_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
# ffmpeg 进度解析正则（Duration + out_time_ms）
_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_FFMPEG_OUTTIME_RE = re.compile(r"out_time_ms=(\d+)")


class FFmpegProcessor:
    """
    FFmpeg 切片 + scene 镜头打分封装。
    对接阶段2的 slice_candidates.json（来自 faster-whisper 转录输出），批量生成片段。
    严格遵守红线1：所有 Popen PID 通过 register_pid_fn 登记到 app.py 的 _our_child_pids 集合统一回收。
    """

    # ---------- 硬约束常量 ----------
    # 模式A：-c copy 无损复制（允许 ±5s 时间误差，keyframe 对齐允许）
    MODE_A_COPY_EXTRA_OFFSET = 5.0  # 允许的起止时间误差（copy 模式关键帧对齐）
    MODE_A_ARGS_PREFIX = ["-y", "-hide_banner", "-nostdin", "-threads", "2"]
    MODE_A_V_A_ARGS = ["-c", "copy"]  # 音视频统一 copy
    # 模式B：Apple Silicon h264_videotoolbox 防崩溃完整参数
    MODE_B_ARGS_PREFIX = ["-y", "-hide_banner", "-nostdin", "-threads", "2"]
    MODE_B_V_ARGS = [
        "-pix_fmt", "yuv420p",
        "-c:v", "h264_videotoolbox",
        "-b:v", "5M",
        "-maxrate", "5M",
        "-bufsize", "10M",
    ]
    MODE_B_A_ARGS = ["-c:a", "aac", "-b:a", "128k"]
    # scene 滤镜阈值（经验值：0.3 中等灵敏度，切换太多可调 0.4，太少可调 0.2）
    SCENE_THRESHOLD = 0.3
    # 切片间 0.5s 冷却 + 每完成 10 个切片 sleep 3s（减少内存/swap 抖动）
    SLICE_INTERVAL_SLEEP = 0.5
    SLICE_COOL_DOWN_EVERY = 10
    SLICE_COOL_DOWN_SLEEP = 3
    # 单个 ffmpeg 切片进程最大执行时长（秒）：保护 3~5 分钟素材 × 60s 切片
    SINGLE_SLICE_TIMEOUT = 300
    SCORE_TIMEOUT = 120

    def __init__(
        self,
        temp_clips_dir: Path,
        log_fn: Optional[Callable[[str, str], None]] = None,
        register_pid_fn: Optional[Callable[[int], None]] = None,
        unregister_pid_fn: Optional[Callable[[int], None]] = None,
        error_log_fn: Optional[Callable[[str], None]] = None,
        ffmpeg_path: str = "ffmpeg",
        ffprobe_path: str = "ffprobe",
    ):
        """
        :param temp_clips_dir: 临时切片根目录（一般为项目根 Temp_Clips）
        :param log_fn: 主日志回调 fn(msg, level='INFO')
        :param register_pid_fn: 登记子进程 PID fn(pid) → 对接 app.register_child_pid
        :param unregister_pid_fn: 移除 PID 登记 fn(pid) → 对接 app.unregister_child_pid
        :param error_log_fn: error.log 回调 fn(msg)
        :param ffmpeg_path: ffmpeg 可执行路径
        :param ffprobe_path: ffprobe 可执行路径
        """
        self.temp_clips_dir = Path(temp_clips_dir)
        self.temp_clips_dir.mkdir(parents=True, exist_ok=True)
        self._log = log_fn or (lambda m, l="INFO": None)
        self._register_pid = register_pid_fn or (lambda pid: None)
        self._unregister_pid = unregister_pid_fn or (lambda pid: None)
        self._errlog = error_log_fn or (lambda m: None)
        self._ffmpeg = ffmpeg_path
        self._ffprobe = ffprobe_path

        # 内部状态
        self._proc: Optional[subprocess.Popen] = None
        self._cancel_event = threading.Event()
        self._pid_registered: Optional[int] = None
        self._lock = threading.Lock()
        # 当前会话工作子目录（单源视频独立目录，结束统一清理）
        self._session_dir: Optional[Path] = None

    # ============================================================
    #  公共 API: 取消（中断当前 ffmpeg，不碰 Ollama）
    # ============================================================
    def cancel(self):
        """用户 cancel_task / atexit 调用：仅终止本类 Popen 启动的 ffmpeg 子进程"""
        self._cancel_event.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                self._log("[ffmpeg_processor] 取消当前 ffmpeg 子进程（红线1：只杀本类 Popen PID，不碰 Ollama）", "WARN")
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                        proc.wait(timeout=3)
                    except Exception:
                        pass
            except Exception as e:
                self._errlog(f"[FFmpegProcessor.cancel] exception: {type(e).__name__}: {e}")

    # ============================================================
    #  内部：Popen 安全封装 + PID 登记（严格遵守红线1）
    # ============================================================
    def _safe_popen(self, cmd: List[str], **kwargs) -> subprocess.Popen:
        """
        启动子进程并登记 PID。kwargs 可覆盖默认 stdout/stderr 设置。
        禁止对任何不相关进程发送信号。
        """
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)
        kwargs.setdefault("bufsize", 1)
        # 不通过 shell，避免命令注入
        proc = subprocess.Popen(cmd, **kwargs)
        self._register_pid(proc.pid)
        with self._lock:
            self._proc = proc
            self._pid_registered = proc.pid
        return proc

    def _wait_and_unregister(self, proc: subprocess.Popen, timeout: Optional[float] = None
                             ) -> Tuple[int, str, str]:
        """
        等待子进程结束，返回 (returncode, stdout_tail, stderr_tail)。
        超时自动 SIGTERM→SIGKILL。结束后从全局 PID 集合移除。
        """
        stdout_tail = ""
        stderr_tail = ""
        pid_was = proc.pid
        try:
            if self._cancel_event.is_set():
                raise RuntimeError("用户已取消")
            outs, errs = proc.communicate(timeout=timeout)
            stdout_tail = (outs.decode("utf-8", errors="replace") or "")[-1000:]
            stderr_tail = (errs.decode("utf-8", errors="replace") or "")[-2000:]
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            self._log(f"[ffmpeg_processor] 子进程 pid={pid_was} 超时({timeout}s)，强制回收（仅本类 Popen）", "WARN")
            try:
                proc.terminate()
                outs, errs = proc.communicate(timeout=5)
                stdout_tail = (outs.decode("utf-8", errors="replace") or "")[-1000:]
                stderr_tail = (errs.decode("utf-8", errors="replace") or "")[-2000:]
                rc = proc.returncode
            except Exception:
                try:
                    proc.kill()
                    outs, errs = proc.communicate(timeout=5)
                    stdout_tail = (outs.decode("utf-8", errors="replace") or "")[-1000:]
                    stderr_tail = (errs.decode("utf-8", errors="replace") or "")[-2000:]
                    rc = proc.returncode
                except Exception as e:
                    rc = -1
                    self._errlog(f"[FFmpegProcessor._wait_and_unregister] kill fail: {e}")
        except RuntimeError as cancel_e:
            # 用户取消：主动 terminate
            try:
                proc.terminate()
                outs, errs = proc.communicate(timeout=5)
                stdout_tail = (outs.decode("utf-8", errors="replace") or "")[-1000:]
                stderr_tail = (errs.decode("utf-8", errors="replace") or "")[-2000:]
                rc = proc.returncode
            except Exception:
                rc = -2
                self._errlog(f"[FFmpegProcessor._wait_and_unregister] cancel term fail: {cancel_e}")
            if rc is None:
                rc = -2
        finally:
            # 从集合移除；_proc 清理
            try:
                self._unregister_pid(pid_was)
            except Exception:
                pass
            with self._lock:
                if self._pid_registered == pid_was:
                    self._pid_registered = None
                if self._proc is proc:
                    self._proc = None
        return (rc or 0), stdout_tail, stderr_tail

    # ============================================================
    #  内部：ffprobe 获取源视频总时长（秒），用于 clamp 切片末尾
    # ============================================================
    def _probe_duration(self, video_path: Path) -> float:
        cmd = [
            self._ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        try:
            proc = self._safe_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rc, out, err = self._wait_and_unregister(proc, timeout=30)
            if rc == 0:
                d = float((out or "").strip())
                if d > 0:
                    self._log(f"[ffmpeg] ffprobe 探测源视频时长: {d:.2f}s", "INFO")
                    return d
        except Exception as e:
            self._errlog(f"[FFmpegProcessor._probe_duration] {type(e).__name__}: {e}")
        return 0.0

    # ============================================================
    #  公共 API 1：按 slice_candidates 批量切片 + 打分
    #  入参 candidates 结构兼容阶段2 transcribe_result:
    #      [{start: float, end: float, duration: float, text: str}, ...]
    #  返回：
    #    {
    #      "ok": bool,
    #      "canceled": bool,
    #      "session_dir": str,
    #      "clips": [ {index, clip_path, start, end, mode, scene_count, score, text} ... ],
    #      "score_txt_path": str,
    #      "score_json_path": str,
    #      "error": str,
    #    }
    # ============================================================
    def slice_and_score(
        self,
        source_video: Path,
        candidates: List[Dict[str, Any]],
        clip_mode: str = "auto",
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        阶段3主流程：
          1. 在 Temp_Clips 下新建会话子目录（源视频 stem + 短 uuid）
          2. 遍历 candidates，按 clip_mode 选择 -c copy(A) / h264_videotoolbox(B)
          3. 每个切片完成后立即调用 _score_single_clip(scene 滤镜) 打分
          4. 写 score.txt（每一行：序号 分数(=scene切换次数) 文件名）+ score.json（结构化）
          5. 全部完成（或取消/失败）：清理会话子目录（仅保留 JSON 到 Temp_Clips 根，供阶段4读取）
        """
        source_video = Path(source_video)
        result: Dict[str, Any] = {
            "ok": False,
            "canceled": False,
            "session_dir": "",
            "clips": [],
            "score_txt_path": "",
            "score_json_path": "",
            "error": "",
        }
        if not source_video.exists():
            result["error"] = f"源视频不存在: {source_video}"
            self._log(f"[slice] 源视频不存在: {source_video}", "ERROR")
            return result
        if not isinstance(candidates, list) or not candidates:
            result["error"] = "切片候选列表为空（请先完成阶段2转录）"
            self._log("[slice] 切片候选列表为空，跳过切片", "WARN")
            return result

        mode = clip_mode if clip_mode in ("auto", "manual") else "auto"
        mode_label = "A-无损复制(-c copy)" if mode == "auto" else "B-VideoToolbox重编码(h264_videotoolbox)"
        self._log(
            f"[slice] 启动批量切片：模式={mode_label} | 候选数={len(candidates)} | "
            f"允许±5s误差={'是' if mode == 'auto' else '否(重编码精确切割)'}",
            "INFO"
        )

        # 1) 会话子目录
        stem = source_video.stem
        safe_stem = "".join(c for c in stem if c.isalnum() or c in "-_ ").strip() or "video"
        session_dir = self.temp_clips_dir / f"{safe_stem}_{uuid.uuid4().hex[:8]}"
        try:
            session_dir.mkdir(parents=True, exist_ok=False)
        except Exception as e:
            result["error"] = f"创建切片会话目录失败: {e}"
            self._errlog(f"[FFmpegProcessor.slice_and_score] mkdir fail {session_dir}: {e}")
            return result
        self._session_dir = session_dir
        result["session_dir"] = str(session_dir)

        # 2) 源视频总时长（clamp 防止切片超尾）
        total_dur = self._probe_duration(source_video)

        total = len(candidates)
        produced_clips: List[Dict[str, Any]] = []

        try:
            for idx, cand in enumerate(candidates, start=1):
                if self._cancel_event.is_set():
                    result["canceled"] = True
                    result["error"] = "用户取消任务"
                    self._log(f"[slice] 用户取消（第 {idx}/{total}）", "WARN")
                    break

                s = float(cand.get("start") or 0.0)
                e = float(cand.get("end") or s + 30.0)
                text = str(cand.get("text") or "")

                # 30~60s 约束兜底（即使上游未过滤）
                if e <= s:
                    e = s + 30.0
                seg_dur = e - s
                if seg_dur < 25:
                    seg_dur = 30.0
                    e = s + seg_dur
                if seg_dur > 65:
                    seg_dur = 60.0
                    e = s + seg_dur
                # clamp to 总时长
                if total_dur > 0 and e > total_dur:
                    e = total_dur
                    s = max(0.0, e - seg_dur)
                real_dur = e - s
                if real_dur < 5:
                    self._log(f"[slice] 片段 {idx}/{total} 时长不足 5s，跳过（s={s:.2f} e={e:.2f}）", "WARN")
                    continue

                pct = int(5 + 85 * (idx / total))
                msg = f"阶段3：切片 {idx}/{total} (模式{mode_label}) s={s:.1f} e={e:.1f} dur={real_dur:.1f}s"
                if progress_cb:
                    try:
                        progress_cb(pct, msg)
                    except Exception:
                        pass
                self._log(f"[slice] {msg}", "INFO")

                # 切片输出路径
                clip_name = f"clip_{idx:03d}.mp4"
                clip_path = session_dir / clip_name

                ok_slice = self._do_single_slice(
                    source=source_video,
                    start=s,
                    duration=real_dur,
                    output=clip_path,
                    mode=mode,
                )
                if not ok_slice:
                    self._log(f"[slice] 片段 {idx}/{total} 切片失败，跳过；不中断整体任务", "ERROR")
                    self._errlog(
                        f"[FFmpegProcessor.slice_and_score] clip {idx} fail s={s:.2f} e={e:.2f} mode={mode}"
                    )
                    if clip_path.exists():
                        try:
                            clip_path.unlink()
                        except Exception:
                            pass
                    continue

                # 镜头打分
                pct2 = int(5 + 85 * ((idx - 0.5) / total))
                msg2 = f"阶段3：scene 打分 {idx}/{total} {clip_name}"
                if progress_cb:
                    try:
                        progress_cb(pct2, msg2)
                    except Exception:
                        pass
                scene_count = self._score_single_clip(clip_path)
                # 分数 = scene 切换次数（切换越多分数越高，按需求规则）
                clip_record = {
                    "index": idx,
                    "clip_path": str(clip_path),
                    "start": round(s, 3),
                    "end": round(e, 3),
                    "duration": round(real_dur, 3),
                    "mode": mode,
                    "scene_count": scene_count,
                    "score": int(scene_count),  # 切换次数即分数，语义直观
                    "text": text[:2000],
                }
                produced_clips.append(clip_record)
                self._log(
                    f"[slice/scene] {clip_name} 完成：scene切换={scene_count}，分数={clip_record['score']}",
                    "INFO"
                )

                # 分段冷却
                if idx % self.SLICE_COOL_DOWN_EVERY == 0 and idx != total:
                    self._log(
                        f"[slice] 已完成 {idx} 个切片，冷却 {self.SLICE_COOL_DOWN_SLEEP}s（降swap）",
                        "INFO"
                    )
                    slept = 0.0
                    while slept < self.SLICE_COOL_DOWN_SLEEP and not self._cancel_event.is_set():
                        time.sleep(0.2)
                        slept += 0.2
                elif self.SLICE_INTERVAL_SLEEP > 0 and not self._cancel_event.is_set():
                    time.sleep(self.SLICE_INTERVAL_SLEEP)

            # 循环结束
            if not self._cancel_event.is_set():
                # 写 score.txt / score.json
                if produced_clips:
                    score_txt = session_dir / "score.txt"
                    score_json = session_dir / "score.json"
                    try:
                        with open(score_txt, "w", encoding="utf-8") as ft:
                            ft.write("# 序号  分数(scene切换次数)  文件  时长  模式\n")
                            for c in produced_clips:
                                ft.write(
                                    f"{c['index']:>3d}  {c['score']:>4d}  "
                                    f"{Path(c['clip_path']).name:<16s}  "
                                    f"{c['duration']:>6.2f}s  {c['mode']}\n"
                                )
                        with open(score_json, "w", encoding="utf-8") as fj:
                            json.dump(
                                {
                                    "source_video": str(source_video),
                                    "clip_mode": mode,
                                    "generated_at": int(time.time()),
                                    "clips": produced_clips,
                                },
                                fj,
                                ensure_ascii=False,
                                indent=2,
                            )
                        result["score_txt_path"] = str(score_txt)
                        result["score_json_path"] = str(score_json)
                        # 再把一份总汇总写到 Temp_Clips 根（阶段4读取，session_dir 清理后不丢）
                        root_summary = self.temp_clips_dir / "slice_results_latest.json"
                        try:
                            with open(root_summary, "w", encoding="utf-8") as fr:
                                json.dump(
                                    {
                                        "source_video": str(source_video),
                                        "clip_mode": mode,
                                        "session_dir": str(session_dir),
                                        "score_txt_path": str(score_txt),
                                        "score_json_path": str(score_json),
                                        "clips": produced_clips,
                                    },
                                    fr,
                                    ensure_ascii=False,
                                    indent=2,
                                )
                        except Exception:
                            pass
                        self._log(
                            f"[slice] ✅ 切片+打分完成。成功 {len(produced_clips)}/{total} 个。"
                            f"score.txt={score_txt}  score.json={score_json}",
                            "INFO"
                        )
                        result["ok"] = True
                    except Exception as e:
                        self._errlog(f"[FFmpegProcessor] 写出score文件失败: {e}")
                        result["error"] = f"写出score文件失败: {e}"
                else:
                    result["error"] = "所有候选切片都失败（请检查源视频或ffmpeg环境）"
                    self._log("[slice] 所有候选切片均失败", "ERROR")
            else:
                result["canceled"] = True
                if not result["error"]:
                    result["error"] = "用户取消任务"

        except Exception as e:
            self._errlog(f"[FFmpegProcessor.slice_and_score] unhandled: {type(e).__name__}: {e}")
            self._log(f"[slice] 未捕获异常: {e}", "ERROR")
            result["error"] = str(e)[:500]
        finally:
            # 最终结果 clips 字段
            result["clips"] = produced_clips
            # 清理：单条源视频全部处理完成 → 自动清空 Temp_Clips 会话子目录（约束3）
            # 注意：仅清空当前 _session_dir，且已经把结果写到 Temp_Clips/slice_results_latest.json 供阶段4
            try:
                if result["ok"] and (self._session_dir is not None) and self._session_dir.exists():
                    # 保留 score.txt / score.json 两份（把它们 copy 到 Temp_Clips 根再删会话子目录，避免阶段4读不到）
                    keep = []
                    for name in ("score.txt", "score.json"):
                        p = self._session_dir / name
                        if p.exists():
                            target = self.temp_clips_dir / f"{safe_stem}_{Path(self._session_dir).name}_{name}"
                            try:
                                shutil.copy2(p, target)
                                keep.append(str(target))
                            except Exception:
                                pass
                    shutil.rmtree(self._session_dir, ignore_errors=True)
                    self._log(
                        f"[slice] Temp_Clips 会话子目录已清理（约束3）。保留的 score 汇总: {keep}",
                        "INFO"
                    )
            except Exception as e:
                self._errlog(f"[FFmpegProcessor] clean session dir fail: {e}")
            gc.collect()
        return result

    # ============================================================
    #  内部：单次切片（模式A / 模式B）
    # ============================================================
    def _do_single_slice(
        self,
        source: Path,
        start: float,
        duration: float,
        output: Path,
        mode: str,
    ) -> bool:
        """
        按模式执行单个 ffmpeg 切片：
          - auto → 模式A：-ss 放 -i 前（快速 seek）+ -t duration + -c copy
            允许±5s误差（关键帧对齐自然出现，符合需求1）。
          - manual → 模式B：-ss 放 -i 后（精确）+ -t duration + VideoToolbox 参数
        返回 True=切片成功且文件>0
        """
        src_str = str(source)
        out_str = str(output)
        # 避免冲突残留
        if output.exists():
            try:
                output.unlink()
            except Exception:
                pass

        if mode == "auto":
            # 模式A：-c copy，允许±5s关键帧对齐
            ss = max(0.0, start - self.MODE_A_COPY_EXTRA_OFFSET)  # 早5s（关键帧对齐）
            plus_d = duration + self.MODE_A_COPY_EXTRA_OFFSET * 2  # 晚5s（总时长 = 原时长 + 10s？）
            # 不，需求写："允许 ±5 秒时间误差" —— 即输出结果起止点偏差最多5秒即可（copy 模式特性）。
            # 实际实现用标准 -ss -i -t copy 流程。
            cmd = (
                [self._ffmpeg]
                + self.MODE_A_ARGS_PREFIX
                + ["-ss", f"{start:.3f}", "-i", src_str, "-t", f"{duration:.3f}"]
                + self.MODE_A_V_A_ARGS
                + ["-movflags", "+faststart", out_str]
            )
        else:
            # 模式B：VideoToolbox 重编码（精确切割 + 防崩溃完整参数）
            cmd = (
                [self._ffmpeg]
                + self.MODE_B_ARGS_PREFIX
                + ["-i", src_str, "-ss", f"{start:.3f}", "-t", f"{duration:.3f}"]
                + self.MODE_B_V_ARGS
                + self.MODE_B_A_ARGS
                + ["-movflags", "+faststart", out_str]
            )

        self._log(f"[slice/ffmpeg] {mode} cmd: {' '.join(cmd[:10])} ... (略，完整已debug记录)", "DEBUG")
        try:
            proc = self._safe_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rc, out_tail, err_tail = self._wait_and_unregister(proc, timeout=self.SINGLE_SLICE_TIMEOUT)
            if self._cancel_event.is_set():
                return False
            if rc != 0 or not output.exists() or output.stat().st_size == 0:
                self._log(
                    f"[slice] ffmpeg 切片失败 rc={rc} size={output.stat().st_size if output.exists() else 0}",
                    "ERROR"
                )
                if err_tail:
                    self._errlog(
                        f"[FFmpegProcessor._do_single_slice] rc={rc} stderr tail:\n{err_tail[-1500:]}"
                    )
                return False
            return True
        except Exception as e:
            self._errlog(f"[FFmpegProcessor._do_single_slice] {type(e).__name__}: {e}")
            return False

    # ============================================================
    #  内部：scene 滤镜打分 —— 返回镜头切换次数（整数）
    # ============================================================
    def _score_single_clip(self, clip_path: Path) -> int:
        """
        调用 ffmpeg scene 滤镜：
          ffmpeg -i clip -vf "select='gt(scene,THRESH)',showinfo" -f null -
        解析 stderr 中所有 pts_time: 行数 => scene 切换次数。
        失败返回 0（不中断主流程）。
        """
        if not clip_path.exists():
            return 0
        vf = f"select='gt(scene,{self.SCENE_THRESHOLD})',showinfo"
        cmd = [
            self._ffmpeg, "-hide_banner", "-nostdin",
            "-i", str(clip_path),
            "-vf", vf,
            "-an",
            "-f", "null", "-",
        ]
        try:
            proc = self._safe_popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            rc, out_tail, err_tail = self._wait_and_unregister(proc, timeout=self.SCORE_TIMEOUT)
            if self._cancel_event.is_set():
                return 0
            # 解析所有 pts_time
            times = _SCENE_PTS_RE.findall(err_tail or "")
            n = len(times)
            if rc != 0 and n == 0:
                self._log(
                    f"[scene] ffmpeg scene 失败 rc={rc}，使用0分（不中断）",
                    "WARN"
                )
                return 0
            return n
        except Exception as e:
            self._errlog(f"[FFmpegProcessor._score_single_clip] {type(e).__name__}: {e}")
            return 0

    # ============================================================
    #  公共工具：从文件 / dict 加载 slice_candidates 列表
    #  兼容阶段2 transcriber 输出路径（{video_stem}.slice_candidates.json）
    # ============================================================
    @staticmethod
    def load_candidates_from_json(path_like) -> List[Dict[str, Any]]:
        """
        从 JSON 文件或 dict 读取切片候选。
        文件 JSON 结构需含 "candidates" 字段（数组），或直接为数组。
        """
        try:
            if isinstance(path_like, (str, Path)):
                with open(str(path_like), "r", encoding="utf-8") as f:
                    obj = json.load(f)
            else:
                obj = path_like
            if isinstance(obj, list):
                return [o for o in obj if isinstance(o, dict)]
            if isinstance(obj, dict):
                cands = obj.get("candidates") or obj.get("slice_candidates") or []
                if isinstance(cands, list):
                    return [o for o in cands if isinstance(o, dict)]
        except Exception:
            return []
        return []
