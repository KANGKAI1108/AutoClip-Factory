# -*- coding: utf-8 -*-
"""
AutoClip Factory - 阶段4：内存+负载监控 + 全局进程类互斥锁 + 分层冷却
适配 8GB 统一内存 MacBook Air：
  * 可用内存轮询（macOS：vm_stat 解析 + sysctl hw.memsize；Linux/其他：/proc/meminfo 兜底）
  * CPU 负载监控：连续 3 秒 CPU > 95% → 强制休眠 60 秒（放弃温度，仅内存+负载双监控）
  * 三类进程互斥：Whisper / FFmpeg / Ollama 同一时刻仅允许其中一类占用内存（全局语义锁，非pid层）
  * 分层冷却接口：
      - cool_down_step(30s) → 单次转录/切片/单组AI推理后休眠
      - cool_down_full(180s) → 1条完整视频全部处理完后休眠
      - sleep_interruptible(total, tick=0.2, cancel_event=None) → 可被取消事件打断的休眠
严格红线1：本模块不启动任何子进程、不pkill不kill任何进程；仅作为监控/互斥/冷却层提供给上层调用。
"""

import os
import re
import gc
import sys
import time
import platform
import threading
import subprocess
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List


# ---------- 硬约束常量（v4.5）----------
# 可用内存阈值（字节）
MEM_1GB_BYTES = 1 * 1024 * 1024 * 1024
MEM_0_5GB_BYTES = int(0.5 * 1024 * 1024 * 1024)
MEM_3_2GB_BYTES = int(3.2 * 1024 * 1024 * 1024)
# CPU 过载阈值
CPU_OVERLOAD_PCT = 95.0
CPU_OVERLOAD_CONSECUTIVE_SEC = 3  # 连续 3 秒
CPU_OVERLOAD_SLEEP_SEC = 60
# 分层冷却常量
COOL_STEP_SEC = 30
COOL_FULL_SEC = 180
# 内存轮询间隔
MEM_POLL_INTERVAL_SEC = 2

# 进程类互斥锁的合法类型
PROC_CLASSES = ("whisper", "ffmpeg", "ollama")


def _mac_total_mem_bytes() -> int:
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout or ""
        v = int(out.strip())
        if v > 0:
            return v
    except Exception:
        pass
    return 0


def _mac_available_bytes_vm_stat() -> int:
    """
    macOS vm_stat 解析：
    Pages free + Pages inactive + (Pages speculative?) 作为近似可用内存。
    失败返回 0。
    """
    try:
        # 获取 page size
        ps_out = subprocess.run(
            ["sysctl", "-n", "hw.pagesize"],
            capture_output=True, text=True, timeout=3, check=False,
        ).stdout or ""
        page_size = int(ps_out.strip()) if ps_out.strip().isdigit() else 4096

        vm = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=3, check=False,
        ).stdout or ""

        def _page(key: str) -> int:
            m = re.search(rf"{re.escape(key)}:\s*(\d+)\.", vm)
            return int(m.group(1)) if m else 0

        free = _page("Pages free")
        inactive = _page("Pages inactive")
        speculative = _page("Pages speculative")
        # 可用 ≈ free + inactive + speculative
        return (free + inactive + speculative) * page_size
    except Exception:
        return 0


def _linux_available_bytes() -> int:
    try:
        p = Path("/proc/meminfo")
        if not p.exists():
            return 0
        txt = p.read_text()
        m = re.search(r"MemAvailable:\s*(\d+)\s*kB", txt)
        if not m:
            return 0
        return int(m.group(1)) * 1024
    except Exception:
        return 0


def _linux_cpu_load_pct_last1s(prev_stat: Optional[List[int]]) -> Optional[tuple]:
    """
    通过 /proc/stat 采样两次 CPU 总时间和空闲时间，返回 (pct, [new_total, new_idle])。
    Linux only；其他平台返回 None。
    """
    try:
        p = Path("/proc/stat")
        if not p.exists():
            return None
        line = p.read_text().splitlines()[0]
        # cpu  user nice system idle iowait irq softirq steal guest guest_nice
        parts = line.split()
        vals = [int(x) for x in parts[1:]]
        total = sum(vals)
        idle = vals[3] + vals[4]  # idle + iowait
        if prev_stat is None or len(prev_stat) < 2:
            return None, [total, idle]
        prev_total, prev_idle = prev_stat[0], prev_stat[1]
        dt = total - prev_total
        di = idle - prev_idle
        if dt <= 0:
            return 0.0, [total, idle]
        pct = 100.0 * (1.0 - (di / dt))
        return max(0.0, min(100.0, pct)), [total, idle]
    except Exception:
        return None


class MemoryWatcher:
    """
    内存 + CPU 负载 + 进程类互斥 三合一监控。
    纯被动组件：不启动任何子进程（除了只读 sysctl/vm_stat/top 这种瞬态探测）；不杀任何进程。
    取消事件（cancel_event）注入后，所有可中断休眠在 cancel 时立即返回。
    """

    def __init__(
        self,
        log_fn: Optional[Callable[[str, str], None]] = None,
        error_log_fn: Optional[Callable[[str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self._log = log_fn or (lambda m, l="INFO": None)
        self._errlog = error_log_fn or (lambda m: None)
        self._cancel = cancel_event or threading.Event()

        # 进程类互斥（语义锁）：同一时刻只有一类 heavy consumer 在跑
        self._class_mutex = threading.Lock()
        self._held_class: Optional[str] = None
        self._holder_thread_id: Optional[int] = None

        # 上次可用内存快照（供外部查询）
        self._last_avail_bytes: int = 0
        self._avail_lock = threading.Lock()

    # ============================================================
    #  取消事件转发
    # ============================================================
    def set_cancel_event(self, ev: threading.Event):
        self._cancel = ev

    def is_canceled(self) -> bool:
        return bool(self._cancel.is_set())

    # ============================================================
    #  公共 API：查询可用内存（字节）
    # ============================================================
    def get_available_bytes(self) -> int:
        avail = 0
        sysn = platform.system()
        if sysn == "Darwin":
            avail = _mac_available_bytes_vm_stat()
        elif sysn == "Linux":
            avail = _linux_available_bytes()
        # 兜底：用 psutil 若可用（不强依赖；v4.5禁止新增额外依赖，所以仅try import）
        if avail <= 0:
            try:
                import psutil  # type: ignore
                mem = psutil.virtual_memory()
                avail = int(getattr(mem, "available", 0))
            except Exception:
                avail = 0
        if avail < 0:
            avail = 0
        with self._avail_lock:
            self._last_avail_bytes = avail
        return avail

    def get_last_available_mb(self) -> int:
        with self._avail_lock:
            v = self._last_avail_bytes
        return int(v / (1024 * 1024))

    def get_total_bytes(self) -> int:
        sysn = platform.system()
        if sysn == "Darwin":
            return _mac_total_mem_bytes()
        elif sysn == "Linux":
            try:
                txt = Path("/proc/meminfo").read_text()
                m = re.search(r"MemTotal:\s*(\d+)\s*kB", txt)
                if m:
                    return int(m.group(1)) * 1024
            except Exception:
                pass
        try:
            import psutil  # type: ignore
            mem = psutil.virtual_memory()
            return int(getattr(mem, "total", 0))
        except Exception:
            return 0

    # ============================================================
    #  公共 API：可中断休眠（分层冷却 / 内存等待共用）
    # ============================================================
    def sleep_interruptible(self, total_sec: float, tick: float = 0.2) -> bool:
        """
        可被 cancel_event 打断的休眠。
        返回 True=被取消提前唤醒；False=睡满了。
        """
        if total_sec <= 0:
            return False
        elapsed = 0.0
        while elapsed < total_sec:
            if self._cancel.is_set():
                return True
            step = min(tick, total_sec - elapsed)
            time.sleep(step)
            elapsed += step
        return bool(self._cancel.is_set())

    # ============================================================
    #  公共 API：内存分区校验（需求 1.3）
    #   - wait_available_ge(target_bytes, timeout_sec)：循环等待可用内存≥目标；被取消/超时返回False
    #   - poll_and_check_during_inference()：推理每2秒轮询；<1GB 休眠45s；持续<0.5GB 返回 (False, reason)
    # ============================================================
    def wait_until_available_ge(
        self,
        target_bytes: int,
        timeout_sec: float,
        poll_sec: float = MEM_POLL_INTERVAL_SEC,
        reason_msg: str = "",
    ) -> bool:
        """
        等待可用内存 >= target_bytes。超时/取消返回 False。
        """
        deadline = time.time() + timeout_sec
        while True:
            a = self.get_available_bytes()
            if a >= target_bytes:
                return True
            if self._cancel.is_set():
                self._log(f"[mem_watch] 等待内存取消：{reason_msg or ''}", "WARN")
                return False
            if time.time() >= deadline:
                self._log(
                    f"[mem_watch] 等待内存超时（≥{target_bytes/1024/1024:.0f}MB）。"
                    f"当前={a/1024/1024:.0f}MB。{reason_msg}",
                    "WARN"
                )
                return False
            self.sleep_interruptible(min(poll_sec, deadline - time.time()))

    def poll_during_inference_loop(
        self,
        is_inference_still_running_fn: Callable[[], bool],
        less_than_1gb_sleep_sec: int = 45,
        low_0_5gb_streak_to_abort: int = 3,  # 连续 N 次<0.5GB 才认定持续
    ) -> tuple:
        """
        推理过程监控：
          - 每 MEM_POLL_INTERVAL_SEC(2s) 轮询一次可用内存
          - 单次<1GB：打断式休眠 less_than_1gb_sleep_sec(45s)
          - 连续 low_0_5gb_streak_to_abort 次 <0.5GB：中止推理 → 返回 (False, reason)
          - 正常结束 → 返回 (True, "ok")
        调用方在自己的推理线程里：while running and watcher.poll_once(...) ...
        这里提供更简单的：只要 is_inference_still_running_fn() 为 True，就一直监控。
        """
        low_512mb_streak = 0
        try:
            while is_inference_still_running_fn() and not self._cancel.is_set():
                a = self.get_available_bytes()
                if a < MEM_0_5GB_BYTES:
                    low_512mb_streak += 1
                    self._log(
                        f"[mem_watch] 推理期间可用内存<512MB（连续{low_512mb_streak}次）：{a/1024/1024:.0f}MB",
                        "WARN"
                    )
                    if low_512mb_streak >= low_0_5gb_streak_to_abort:
                        return False, "available_memory_below_512mb_for_streak"
                else:
                    low_512mb_streak = 0  # reset
                    if a < MEM_1GB_BYTES:
                        self._log(
                            f"[mem_watch] 推理期间可用内存<1GB（{a/1024/1024:.0f}MB），"
                            f"休眠 {less_than_1gb_sleep_sec}s（swap保护）",
                            "WARN"
                        )
                        canceled = self.sleep_interruptible(less_than_1gb_sleep_sec)
                        if canceled:
                            return False, "canceled"
                        continue
                # 正常轮询间隔
                if self.sleep_interruptible(MEM_POLL_INTERVAL_SEC):
                    return False, "canceled"
            if self._cancel.is_set():
                return False, "canceled"
            return True, "ok"
        except Exception as e:
            self._errlog(f"[MemoryWatcher.poll_during_inference_loop] {type(e).__name__}: {e}")
            return False, f"exception:{type(e).__name__}"

    # ============================================================
    #  公共 API：卸载模型后等待 15s → 若仍不足 3.2GB → 全局休眠 120s
    # ============================================================
    def after_unload_cooldown(
        self,
        wait_1: float = 15.0,
        required_bytes: int = MEM_3_2GB_BYTES,
        global_sleep_if_short: float = 120.0,
    ) -> bool:
        """
        需求 1.3：卸载模型后等待15秒校验内存，不足3.2GB全局休眠120秒。
        返回 True=充足；False=进入120s全局休眠后返回（调用方自行决定是否继续）。
        """
        self._log("[mem_watch] 推理模型卸载完成，等待15s校验可用内存…", "INFO")
        gc.collect()
        self.sleep_interruptible(wait_1)
        if self._cancel.is_set():
            return False
        a = self.get_available_bytes()
        self._log(f"[mem_watch] 15s后校验可用内存：{a/1024/1024:.0f}MB（阈值 {required_bytes/1024/1024:.0f}MB）", "INFO")
        if a >= required_bytes:
            return True
        self._log(
            f"[mem_watch] 仍不足3.2GB，进入全局休眠 {global_sleep_if_short:.0f}s（降低swap压力）",
            "WARN"
        )
        self.sleep_interruptible(global_sleep_if_short)
        return False

    # ============================================================
    #  公共 API：CPU 负载监控（需求 3.2）连续3秒>95% → 60s休眠
    # ============================================================
    def cpu_overload_protect_if_needed(self) -> None:
        """
        采样 CPU_OVERLOAD_CONSECUTIVE_SEC(3) 次（间隔1s）：若每次都>95% → 60s interruptible sleep。
        macOS 无稳定 1s 粒度采样（避免依赖 psutil）；仅 Linux 平台基于 /proc/stat 生效。
        非Linux平台：退化使用 load average（若可用）简单判断（保守）。
        """
        sysn = platform.system()
        if self._cancel.is_set():
            return
        try:
            if sysn == "Linux" and Path("/proc/stat").exists():
                prev: Optional[List[int]] = None
                consecutive = 0
                for _ in range(CPU_OVERLOAD_CONSECUTIVE_SEC):
                    r = _linux_cpu_load_pct_last1s(prev)
                    if r is None:
                        return
                    pct, prev = r
                    self._log(f"[mem_watch] CPU load last 1s = {pct:.1f}%", "DEBUG" if pct <= CPU_OVERLOAD_PCT else "WARN")
                    if pct is not None and pct >= CPU_OVERLOAD_PCT:
                        consecutive += 1
                    else:
                        consecutive = 0
                        break
                    if self.sleep_interruptible(1.0):
                        return
                if consecutive >= CPU_OVERLOAD_CONSECUTIVE_SEC:
                    self._log(
                        f"[mem_watch] 连续{CPU_OVERLOAD_CONSECUTIVE_SEC}s CPU > {CPU_OVERLOAD_PCT:.0f}%，"
                        f"强制休眠 {CPU_OVERLOAD_SLEEP_SEC}s（整机保护）",
                        "WARN"
                    )
                    self.sleep_interruptible(CPU_OVERLOAD_SLEEP_SEC)
                    return
            else:
                # macOS / 其他：load average (1min) > ncpu*0.95 当作近似过载（一次命中就触发60s，保守）
                try:
                    import os as _os
                    load1, load5, load15 = _os.getloadavg()
                    ncpu = _os.cpu_count() or 1
                    thr = ncpu * (CPU_OVERLOAD_PCT / 100.0)
                    if load1 >= thr:
                        self._log(
                            f"[mem_watch] loadavg 1min={load1:.2f} ≥ 阈值 {thr:.2f} (ncpu={ncpu})，"
                            f"保守休眠 {CPU_OVERLOAD_SLEEP_SEC}s",
                            "WARN"
                        )
                        self.sleep_interruptible(CPU_OVERLOAD_SLEEP_SEC)
                except Exception:
                    return
        except Exception as e:
            self._errlog(f"[MemoryWatcher.cpu_overload_protect] {type(e).__name__}: {e}")

    # ============================================================
    #  公共 API：进程类互斥锁（Whisper / FFmpeg / Ollama 不同时占内存，需求3.1）
    #    acquire_proc_class(cls) / release_proc_class()
    #    语义上的全局互斥（同一时刻仅一类大内存占用者运行），非 pid 强制。
    # ============================================================
    def acquire_proc_class(self, cls: str, timeout_sec: float = 60.0) -> bool:
        if cls not in PROC_CLASSES:
            raise ValueError(f"cls must be in {PROC_CLASSES}, got {cls}")
        deadline = time.time() + timeout_sec
        while True:
            with self._class_mutex:
                if self._held_class is None:
                    self._held_class = cls
                    self._holder_thread_id = threading.get_ident()
                    self._log(f"[proc_class_lock] 持有 {cls}（当前线程）", "INFO")
                    return True
            if self._cancel.is_set():
                self._log(f"[proc_class_lock] 等待 {cls} 被取消", "WARN")
                return False
            if time.time() >= deadline:
                self._log(
                    f"[proc_class_lock] 等待 {cls} 锁超时（当前持有={self._held_class}）",
                    "WARN"
                )
                return False
            self.sleep_interruptible(0.5)

    def release_proc_class(self, cls: str) -> None:
        with self._class_mutex:
            if self._held_class == cls and self._holder_thread_id == threading.get_ident():
                self._held_class = None
                self._holder_thread_id = None
                self._log(f"[proc_class_lock] 释放 {cls}", "INFO")
            else:
                self._errlog(
                    f"[proc_class_lock] 释放不匹配：请求={cls}, 当前持有={self._held_class}, thread={self._holder_thread_id}"
                )

    # ============================================================
    #  公共 API：分层冷却（需求 3.3）
    # ============================================================
    def cool_down_step(self, sec: float = COOL_STEP_SEC) -> bool:
        """单次转录/切片/单组推理完成后休眠（默认30s）。返回True=被取消。"""
        self._log(f"[cool_down] 步骤冷却 {sec:.0f}s（swap保护）", "INFO")
        return self.sleep_interruptible(sec)

    def cool_down_full(self, sec: float = COOL_FULL_SEC) -> bool:
        """完整处理1条视频后全局休眠（默认180s）。返回True=被取消。"""
        self._log(f"[cool_down] 整条视频流程结束，全局冷却 {sec:.0f}s", "INFO")
        return self.sleep_interruptible(sec)
