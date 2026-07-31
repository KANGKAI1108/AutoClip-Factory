# -*- coding: utf-8 -*-
"""
AutoClip Factory - 阶段2：faster-whisper 字幕提取类
适配 macOS Apple Silicon 8GB 机型：
  * 固定模型 base.en（~140MB 内存占用，8GB 机型友好）
  * device="cpu" 显式指定 —— M 芯片走 CTranslate2 CPU float16 路径（无需 CUDA）
  * compute_type="float16"，禁用 int8（Apple Silicon NEON+AMX 对 fp16 友好）
  * vad_filter=True 过滤静音（减少转录时间，降内存峰值）
  * 分段冷却：每处理 10 分钟音频，自动 sleep 30s（减少 swap）
  * 完整释放：model.unload_model() + del 实例 + gc.collect()
  * 输出：带毫秒时间戳英文字幕 SRT；按 .?! 标点分割完整台词；筛选 30~60 秒有效切片时间戳
禁止编写任何 ffmpeg 切片 / Ollama AI 推理代码（阶段3/4实现）
"""

import os
import re
import gc
import sys
import time
import threading
import subprocess
from pathlib import Path
from datetime import timedelta
from typing import Callable, Optional, Dict, Any, List, Tuple


# ============================================================
#  辅助：秒 -> SRT 时间戳 (HH:MM:SS,mmm)
# ============================================================
def _sec_to_srt_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    td = timedelta(seconds=sec)
    total_ms = int(td.total_seconds() * 1000)
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


class FasterWhisperTranscriber:
    """faster-whisper 轻量封装（只做转录 + 切片候选筛选，不做切片/推理）"""

    # ---------- 硬约束常量（M1 8GB 机型友好）----------
    MODEL_SIZE = "base.en"             # 固定 base.en（英文，~140MB）
    DEVICE = "cpu"                     # M芯片走 CTranslate2 CPU float16 路径
    COMPUTE_TYPE = "float16"           # 显式 fp16，禁用 int8
    VAD_FILTER = True                  # 过滤静音，减少推理量
    BEAM_SIZE = 3                      # beam=3（比默认5更省内存）
    CHUNK_LENGTH = 20                  # 20s 一 chunk（控制内存峰值）
    BATCH_SIZE = 1                     # 强制 batch_size=1，稳定优先
    SEG_COOL_DOWN_EVERY_SEC = 600      # 每累积处理 600 秒（10分钟）音频 => 休眠
    SEG_COOL_DOWN_SLEEP_SEC = 30       # 休眠 30 秒
    SLICE_MIN_SEC = 30                 # 有效切片最短 30s
    SLICE_MAX_SEC = 60                 # 有效切片最长 60s
    PUNCT_SPLIT_RE = re.compile(r"(?<=[.?!])\s+")  # .?! 后跟空白 => 分句

    def __init__(
        self,
        work_dir: Path,
        log_fn: Optional[Callable[[str, str], None]] = None,
        error_log_fn: Optional[Callable[[str], None]] = None,
        ffmpeg_path: str = "ffmpeg",   # 允许外部注入（阶段3 ffmpeg 工具类可复用）
    ):
        """
        :param work_dir: 输出字幕 / 切片 JSON 的目录（建议 Temp_Clips 或 Source_Videos 同级）
        :param log_fn: 主日志回调 fn(msg, level='INFO')
        :param error_log_fn: error.log 回调 fn(msg)
        :param ffmpeg_path: ffmpeg 可执行文件路径（默认系统 ffmpeg）
        """
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._log = log_fn or (lambda m, l="INFO": None)
        self._errlog = error_log_fn or (lambda m: None)
        self._ffmpeg_path = ffmpeg_path

        # 内部状态
        self._model = None
        self._cancel_event = threading.Event()

    # ============================================================
    #  公共 API: 取消（中断当前转录，释放模型）
    # ============================================================
    def cancel(self):
        self._cancel_event.set()

    # ============================================================
    #  内部：faster-whisper 延迟加载（只有真正调用 transcribe 才占内存）
    # ============================================================
    def _ensure_model(self):
        """加载 base.en，失败写日志抛异常"""
        if self._model is not None:
            return
        self._log(
            f"[whisper] 加载模型 {self.MODEL_SIZE} "
            f"(device={self.DEVICE}, compute_type={self.COMPUTE_TYPE}, "
            f"vad_filter={self.VAD_FILTER}) —— 首次加载可能需联网下载（~140MB）",
            "INFO"
        )
        try:
            # 延迟导入：模块未安装时 import 顶部不会崩
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.MODEL_SIZE,
                device=self.DEVICE,
                compute_type=self.COMPUTE_TYPE,
            )
            self._log("[whisper] 模型加载完成", "INFO")
        except Exception as e:
            self._log(
                f"[whisper] 模型加载失败: {type(e).__name__}: {e}",
                "ERROR"
            )
            self._errlog(
                f"[FasterWhisperTranscriber._ensure_model] {type(e).__name__}: {e}"
            )
            raise

    # ============================================================
    #  内部：ffmpeg 提取音频（转为 16kHz mono wav，让 whisper 更稳定）
    #  注意：红线1 —— 仅回收本类 Popen 启动的 ffmpeg；本类不使用全局 pkill
    # ============================================================
    def _extract_audio(
        self,
        video_path: Path,
        progress_cb: Callable[[int, str], None],
    ) -> Tuple[Optional[Path], Optional[subprocess.Popen]]:
        """
        用 ffmpeg 提取 16kHz mono wav 到 Temp_Clips。
        返回 (wav_path, ffmpeg_proc)；调用者必须负责 wait+回收。
        失败返回 (None, None)。
        """
        stem = video_path.stem
        wav_path = self.work_dir / f"{stem}.16k_mono.wav"
        cmd = [
            self._ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000",
            "-f", "wav", str(wav_path),
        ]
        self._log(f"[whisper] ffmpeg 提取音频 -> {wav_path.name}", "INFO")
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return wav_path, proc
        except FileNotFoundError:
            self._log(
                "[whisper] 未找到 ffmpeg。请安装：brew install ffmpeg",
                "ERROR"
            )
            self._errlog("[FasterWhisperTranscriber._extract_audio] ffmpeg not found")
            return None, None
        except Exception as e:
            self._log(f"[whisper] ffmpeg 启动失败: {e}", "ERROR")
            self._errlog(f"[FasterWhisperTranscriber._extract_audio] {type(e).__name__}: {e}")
            return None, None

    # ============================================================
    #  内部：按 .?! 标点合并 segments 成完整台词 + 筛选 30~60s 切片候选
    # ============================================================
    @classmethod
    def _build_sentences_and_candidates(
        cls,
        segments: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        :param segments: faster-whisper 原始片段（dict: start/end/text）
        :return: (sentences, candidates)
          - sentences: 按 .?! 合并后的完整台词 {start, end, text}
          - candidates: 满足 30~60 秒的切片时间戳列表 [{start, end, text, duration}]
        """
        sentences: List[Dict[str, Any]] = []
        if not segments:
            return sentences, []

        buf_text_parts: List[str] = []
        buf_start: Optional[float] = None
        buf_end: float = 0.0

        def flush_buf():
            nonlocal buf_start, buf_end
            if buf_start is None or not buf_text_parts:
                return
            text = " ".join(p.strip() for p in buf_text_parts if p.strip())
            if text:
                sentences.append({
                    "start": float(buf_start),
                    "end": float(buf_end),
                    "text": text,
                })
            buf_text_parts.clear()
            buf_start = None
            buf_end = 0.0

        for seg in segments:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", 0.0))
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            if buf_start is None:
                buf_start = start
            buf_end = end
            buf_text_parts.append(text)
            # 以 .?! 结尾 => 整句结束
            if len(text) >= 1 and text[-1] in ".?!":
                flush_buf()
        # 尾部 flush
        flush_buf()

        # 筛选 30~60s 连续句子合并的切片候选（贪心滑窗）
        candidates: List[Dict[str, Any]] = []
        n = len(sentences)
        i = 0
        while i < n:
            c_start = sentences[i]["start"]
            merged_parts: List[str] = []
            j = i
            cur_end = sentences[i]["end"]
            while j < n:
                s = sentences[j]
                cur_end = s["end"]
                merged_parts.append(s["text"])
                dur = cur_end - c_start
                if dur >= cls.SLICE_MIN_SEC:
                    if dur <= cls.SLICE_MAX_SEC:
                        candidates.append({
                            "start": round(c_start, 3),
                            "end": round(cur_end, 3),
                            "duration": round(dur, 3),
                            "text": " ".join(merged_parts),
                        })
                    break  # 超过也 break，下一轮 i 从 j+1 开始
                j += 1
            # 移动 i；避免死循环：当单句就 > 60s 时 i+1 步进
            i = max(i + 1, j + 1 if j < n else n)

        return sentences, candidates

    # ============================================================
    #  内部：SRT 输出（带毫秒时间戳）
    # ============================================================
    @staticmethod
    def _write_srt(segments: List[Dict[str, Any]], srt_path: Path):
        with open(srt_path, "w", encoding="utf-8") as f:
            idx = 1
            for s in segments:
                start = float(s.get("start", 0.0))
                end = float(s.get("end", 0.0))
                text = (s.get("text") or "").strip()
                if not text:
                    continue
                f.write(f"{idx}\n")
                f.write(f"{_sec_to_srt_ts(start)} --> {_sec_to_srt_ts(end)}\n")
                f.write(text + "\n\n")
                idx += 1

    # ============================================================
    #  公共 API: 主入口 transcribe
    #  input video_path -> output 字幕 + 切片候选 JSON
    # ============================================================
    def transcribe(
        self,
        video_path: Path,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Dict[str, Any]:
        """
        返回 dict:
        {
          ok: bool,
          wav_path: str (temp),
          srt_path: str,
          segments_total: int,
          sentences_total: int,
          slice_candidates: [ {start, end, duration, text}, ... ],
          slice_candidates_total: int,
          duration_sec: float (音频总长),
          error: str,
          canceled: bool,
        }
        """
        progress_cb = progress_cb or (lambda p, s: None)
        ret: Dict[str, Any] = {
            "ok": False,
            "wav_path": None,
            "srt_path": None,
            "segments_total": 0,
            "sentences_total": 0,
            "slice_candidates": [],
            "slice_candidates_total": 0,
            "duration_sec": 0.0,
            "error": "",
            "canceled": False,
        }

        video_path = Path(video_path)
        if not video_path.exists():
            ret["error"] = f"视频文件不存在: {video_path}"
            self._log(f"[whisper] {ret['error']}", "ERROR")
            self._errlog(f"[FasterWhisperTranscriber.transcribe] {ret['error']}")
            return ret

        try:
            # ---------- 阶段 A：音频提取（占总进度 81% ~ 85%）----------
            progress_cb(81, "阶段A：ffmpeg 提取 16kHz 单声道 WAV")
            wav_path, ffmpeg_proc = self._extract_audio(video_path, progress_cb)
            if wav_path is None or ffmpeg_proc is None:
                ret["error"] = "ffmpeg 音频提取失败（详见日志）"
                return ret
            try:
                ffmpeg_stdout, ffmpeg_stderr = ffmpeg_proc.communicate(timeout=60 * 60 * 1)  # 1h 上限
                if ffmpeg_proc.returncode != 0:
                    err = (ffmpeg_stderr or b"").decode("utf-8", errors="replace")[-500:]
                    ret["error"] = f"ffmpeg 返回码 {ffmpeg_proc.returncode}: {err}"
                    self._log(f"[whisper] {ret['error']}", "ERROR")
                    self._errlog(
                        f"[FasterWhisperTranscriber.transcribe] ffmpeg rc={ffmpeg_proc.returncode} stderr_tail={err}"
                    )
                    return ret
            except subprocess.TimeoutExpired:
                ffmpeg_proc.kill()
                ret["error"] = "ffmpeg 音频提取超时（>1h）"
                self._log(f"[whisper] {ret['error']}", "ERROR")
                self._errlog(f"[FasterWhisperTranscriber.transcribe] ffmpeg timeout file={video_path.name}")
                return ret

            if not wav_path.exists() or wav_path.stat().st_size == 0:
                ret["error"] = "提取的音频文件为空或不存在"
                self._log(f"[whisper] {ret['error']}", "ERROR")
                return ret
            ret["wav_path"] = str(wav_path)
            size_mb = round(wav_path.stat().st_size / 1024 / 1024.0, 2)
            self._log(f"[whisper] 音频提取完成 {wav_path.name} ({size_mb}MB)", "INFO")

            # ---------- 阶段 B：加载模型 + 转录（占进度 85% ~ 97%）----------
            if self._cancel_event.is_set():
                ret["canceled"] = True
                ret["error"] = "转录被取消"
                return ret
            progress_cb(85, "阶段B：加载 faster-whisper base.en 模型（首次需联网下载约140MB）")
            try:
                self._ensure_model()
            except Exception as e:
                ret["error"] = f"模型加载失败: {e}"
                return ret

            self._log(
                "[whisper] 开始转录：vad_filter=True, beam=3, chunk=20s, batch=1，"
                "每处理10分钟音频自动冷却30s",
                "INFO"
            )
            progress_cb(87, "阶段B：正在转录（稳定优先，速度慢属正常）")

            # 调用 faster-whisper transcribe
            assert self._model is not None
            try:
                iter_segments, info = self._model.transcribe(
                    str(wav_path),
                    language="en",          # base.en 固定英文
                    beam_size=self.BEAM_SIZE,
                    vad_filter=self.VAD_FILTER,
                    chunk_length=self.CHUNK_LENGTH,
                    batch_size=self.BATCH_SIZE,
                    condition_on_previous_text=False,  # 省上下文缓存
                )
            except Exception as e:
                ret["error"] = f"whisper.transcribe 启动失败: {type(e).__name__}: {e}"
                self._log(f"[whisper] {ret['error']}", "ERROR")
                self._errlog(
                    f"[FasterWhisperTranscriber.transcribe] start transcribe err: {ret['error']}"
                )
                return ret

            audio_duration = float(getattr(info, "duration", 0.0) or 0.0)
            ret["duration_sec"] = round(audio_duration, 2)
            self._log(f"[whisper] 音频总长 ≈ {ret['duration_sec']} 秒", "INFO")

            # 逐段消费 segments 迭代器 —— 冷却计时 + 进度 + 取消检测
            raw_segments: List[Dict[str, Any]] = []
            processed_sec = 0.0            # 累计已处理音频秒（用于冷却）
            last_cooldown_mark_sec = 0.0   # 上次冷却时的累计处理秒
            last_logged_pct = -1

            for seg in iter_segments:
                if self._cancel_event.is_set():
                    ret["canceled"] = True
                    ret["error"] = "转录被用户取消"
                    self._log("[whisper] 转录已取消", "WARN")
                    return ret
                # faster-whisper 返回的 Segment namedtuple / dict 兼容
                try:
                    start = float(seg.start)
                    end = float(seg.end)
                    text = str(seg.text)
                except Exception:
                    try:
                        start = float(seg["start"])
                        end = float(seg["end"])
                        text = str(seg["text"])
                    except Exception:
                        continue
                raw_segments.append({"start": start, "end": end, "text": text})
                seg_dur = max(0.0, end - start)
                processed_sec += seg_dur

                # 分段冷却：跨过 SEG_COOL_DOWN_EVERY_SEC 边界就 sleep 30s
                if processed_sec - last_cooldown_mark_sec >= self.SEG_COOL_DOWN_EVERY_SEC:
                    self._log(
                        f"[whisper] 已处理 ≈ {int(processed_sec)}s 音频 —— "
                        f"按约束冷却 {self.SEG_COOL_DOWN_SLEEP_SEC}s，减少 swap",
                        "INFO"
                    )
                    slept = 0
                    while slept < self.SEG_COOL_DOWN_SLEEP_SEC and not self._cancel_event.is_set():
                        time.sleep(1.0)
                        slept += 1
                    last_cooldown_mark_sec = processed_sec

                # 进度（转录阶段映射到总任务的 87%~97%）
                if audio_duration > 0:
                    pct = min(1.0, processed_sec / audio_duration)
                    overall = 87 + int(pct * 10)  # 87..97
                    if overall != last_logged_pct:
                        last_logged_pct = overall
                        progress_cb(overall, f"阶段B：转录进行中 {int(pct*100)}%（stabe优先）")

            ret["segments_total"] = len(raw_segments)
            self._log(f"[whisper] 原始片段 {len(raw_segments)} 条，开始按标点合句并筛选30~60s切片", "INFO")
            progress_cb(97, "阶段C：按 .?! 合句 + 筛选30~60s切片候选")

            # ---------- 阶段 C：合句 + 切片候选 + 写 SRT（进度 97% ~ 99%）----------
            sentences, candidates = self._build_sentences_and_candidates(raw_segments)
            ret["sentences_total"] = len(sentences)
            ret["slice_candidates"] = candidates
            ret["slice_candidates_total"] = len(candidates)

            # 写 SRT（使用原始 segments 带毫秒）
            srt_path = self.work_dir / f"{video_path.stem}.en.srt"
            self._write_srt(raw_segments, srt_path)
            ret["srt_path"] = str(srt_path)
            self._log(
                f"[whisper] SRT 字幕已输出 {srt_path.name}，"
                f"候选切片 {len(candidates)} 条（{self.SLICE_MIN_SEC}~{self.SLICE_MAX_SEC}s）",
                "INFO"
            )

            # 写切片候选 JSON（给后续阶段4 AI 高光选择用）
            import json as _json
            cand_path = self.work_dir / f"{video_path.stem}.slice_candidates.json"
            with open(cand_path, "w", encoding="utf-8") as f:
                _json.dump(
                    {
                        "source_video": str(video_path),
                        "audio_duration_sec": ret["duration_sec"],
                        "slice_min_sec": self.SLICE_MIN_SEC,
                        "slice_max_sec": self.SLICE_MAX_SEC,
                        "candidates": candidates,
                    },
                    f, ensure_ascii=False, indent=2,
                )
            self._log(f"[whisper] 切片候选清单 -> {cand_path.name}", "INFO")

            progress_cb(99, "阶段D：释放 faster-whisper 内存（unload + del + gc）")

            ret["ok"] = True
            return ret

        finally:
            # ---------- 内存完整释放流程（红线级约束）----------
            # 1) unload_model
            if self._model is not None:
                try:
                    unload = getattr(self._model, "unload_model", None)
                    if callable(unload):
                        unload()
                        self._log("[whisper] model.unload_model() 已执行", "INFO")
                except Exception as e:
                    self._errlog(f"[FasterWhisperTranscriber.transcribe] unload_model err: {e}")
            # 2) del 实例引用
            if self._model is not None:
                try:
                    del self._model
                except Exception:
                    pass
                self._model = None
            # 3) gc.collect() x 2（更稳定清空 Metal / CTranslate2 缓存）
            gc.collect()
            time.sleep(0.2)
            gc.collect()
            self._log("[whisper] gc.collect() x2 已执行（清空 Metal / CTranslate2 推理缓存）", "INFO")

            # 4) 清理临时 wav（临时文件处理完成自动清空 —— 遵守目录规则4）
            wav_p = ret.get("wav_path")
            if wav_p:
                try:
                    wp = Path(wav_p)
                    if wp.exists() and wp.suffix in {".wav", ".mp3", ".m4a"}:
                        wp.unlink()
                        self._log(f"[whisper] 临时音频 {wp.name} 已清理", "INFO")
                except Exception as e:
                    self._errlog(f"[FasterWhisperTranscriber.transcribe] clean wav fail: {e}")

            progress_cb(100 if ret["ok"] else 100, "转录阶段结束")
