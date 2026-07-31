# -*- coding: utf-8 -*-
"""
AutoClip Factory - 阶段4：Ollama llama3:8b Q4_K_M 本地推理封装
严格 v4.5 约束：
  1.1 OLLAMA_LOW_VRAM='1' 环境变量由 app.py 最开头设置（这里不重复设，只做校验）
      API 请求体标准固定格式：
          {"model":"llama3:8b","prompt":"...",
           "options":{"num_ctx":1024},  # num_ctx 必须嵌套在 options 内，禁止顶层！
           "keep_alive":"0",           # 字符串 "0"；模型内存释放仅靠它（红线1：不杀ollama serve）
           "format":"json",            # 强制输出 JSON
           "stream":false}             # 非流式，便于解析
  1.2 Token 零内存粗算：estimated_tokens = len(text)//4；700token 预警，自动≤2组分组
  1.3 内存分区校验：
        - 模型加载阶段跳过 3.2GB 拦截（这里只负责推理，加载是 ollama 内部做，调用前 wait_until_available_ge 做前置校验）
        - 推理每 2 秒轮询内存，<1GB 休眠 45 秒；持续 <0.5GB 中断，保存已生成缓存
        - 卸载模型：通过 keep_alive:"0" 请求体自动触发；卸载后 15 秒校验内存，不足 3.2GB 全局休眠 120 秒（由调用方调 memory_watcher.after_unload_cooldown）
  1.4 输出固定 JSON 数组：每条切片返回 id, ≤50 字符 title, 20 词 hook, 3 个英文 SEO 标签
        返回结构：[{"id": int, "title": str, "hook": str, "tags": [str,str,str]}, ...]
  5. 禁用 LLaVA 视觉模型；只做纯文本推理（scene 分数来自阶段3）

严格红线1：本类不 kill/pkill 任何 ollama 进程，不通过 subprocess 调用 ollama CLI，
仅使用 127.0.0.1:11434 HTTP API + keep_alive:"0" body 字段释放。
"""

import os
import re
import gc
import sys
import time
import json
import threading
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List, Tuple

try:
    # 纯标准库实现 HTTP 请求，不引入 requests（禁止新增依赖）
    from urllib import request as _urllib_request
    from urllib.error import URLError as _URLError, HTTPError as _HTTPError
except Exception:
    _urllib_request = None  # type: ignore


# ---------- 硬约束常量 ----------
OLLAMA_DEFAULT_ENDPOINT = "http://127.0.0.1:11434"
OLLAMA_MODEL = "llama3:8b"
OLLAMA_NUM_CTX = 1024
TOKEN_WARN_THRESHOLD = 700
MAX_GROUPS = 2
TOKEN_ESTIMATE_DIVISOR = 4  # estimated_tokens = len(text) // 4

# 输出字段限制
TITLE_MAX_LEN = 50
HOOK_MAX_WORDS = 20
TAGS_EXPECTED = 3
TAG_MAX_LEN = 30

_CLEAN_TAG_RE = re.compile(r"[^A-Za-z0-9_\- ]+")
_WORD_SPLIT_RE = re.compile(r"\s+")


def _estimate_tokens(text: str) -> int:
    """需求1.2：len(text)//4 粗算 token，禁用 HuggingFace tokenizer"""
    if not text:
        return 0
    return len(text) // TOKEN_ESTIMATE_DIVISOR


def _split_into_groups(clips_meta: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """
    根据累计 estimated_tokens 自动分成 ≤ MAX_GROUPS(2) 组；
    若单组≤700token，就1组。超过就按 TOKEN_WARN_THRESHOLD 切，最多 2 组，剩余合并进第2组。
    """
    if not clips_meta:
        return []
    token_per_clip = []
    for c in clips_meta:
        snippet = _build_clip_snippet(c)
        token_per_clip.append(_estimate_tokens(snippet))

    groups: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_tok = 0
    for c, t in zip(clips_meta, token_per_clip):
        projected = cur_tok + t + 150  # +prompt overhead (~150 tokens 粗略)
        if cur and projected >= TOKEN_WARN_THRESHOLD and len(groups) < MAX_GROUPS - 1:
            groups.append(cur)
            cur = [c]
            cur_tok = t
        else:
            cur.append(c)
            cur_tok += t
    if cur:
        groups.append(cur)
    if len(groups) > MAX_GROUPS:
        # 兜底：2组之后所有 clip 塞第2组（防止组数超限制）
        merged = groups[MAX_GROUPS - 1]
        for extra in groups[MAX_GROUPS:]:
            merged.extend(extra)
        groups = groups[:MAX_GROUPS - 1] + [merged]
    return groups


def _build_clip_snippet(c: Dict[str, Any]) -> str:
    """把阶段3 clip record 转成 prompt 里的一行摘要"""
    idx = int(c.get("index") or 0)
    start = float(c.get("start") or 0.0)
    end = float(c.get("end") or start + 30.0)
    score = int(c.get("score") or 0)
    scene = int(c.get("scene_count") or score)
    text = (str(c.get("text") or "").replace("\n", " ").strip())[:400]
    return (
        f"[CLIP {idx}] start={start:.1f}s end={end:.1f}s scene_changes={scene} score={score} transcript=\"{text}\""
    )


def _build_prompt(group: List[Dict[str, Any]]) -> str:
    """
    构建 prompt：明确要求返回严格 JSON 数组，每条含 id(=clip.index)/title≤50char/hook≤20words/tags[3英文]
    禁止额外字段。
    """
    snippets = "\n".join(_build_clip_snippet(c) for c in group)
    clip_ids = [int(c.get("index") or 0) for c in group]
    prompt = f"""You are an English short-form video highlight editor. For each clip listed below, generate ONLY a JSON array (top-level list) with EXACTLY these keys per item:
- "id": integer, MUST equal one of the clip index values {clip_ids} in given order, one item per clip.
- "title": string, ENGLISH ONLY, max {TITLE_MAX_LEN} characters. Catchy but precise.
- "hook": string, ENGLISH ONLY. Exactly {HOOK_MAX_WORDS} words or fewer (do NOT exceed). First sentence / thumbnail hook for a TikTok/Shorts clip.
- "tags": array of EXACTLY {TAGS_EXPECTED} strings, ENGLISH ONLY, each tag short (max {TAG_MAX_LEN} chars), SEO-friendly (e.g. ["travel","vlog","cinematic"]). No emoji. No punctuation inside tags.

Do NOT include any key outside the four above. Do NOT include any extra text outside the JSON array. Do NOT wrap the JSON in markdown code fences. Output ONLY the JSON array.

Clips (score is higher = more scene changes = more visually interesting):
{snippets}
"""
    return prompt


def _sanitize_tag(raw: str) -> str:
    cleaned = _CLEAN_TAG_RE.sub("", raw or "").strip().lower()
    cleaned = cleaned.replace(" ", "_")
    return cleaned[:TAG_MAX_LEN]


def _normalize_output(raw_obj: Any, expected_ids: List[int]) -> List[Dict[str, Any]]:
    """
    把模型返回的任意 JSON 形态强制归一化成：
      [{"id":int, "title":str(≤50), "hook":str(≤20词), "tags":[s,s,s](每个英文短tag)}, ...]
    单条失败 → 生成一条兜底占位（确保批量容错，不中断整组）。
    """
    out_list: List[Any] = []
    if isinstance(raw_obj, list):
        out_list = raw_obj
    elif isinstance(raw_obj, dict):
        for k in ("clips", "items", "results", "highlights"):
            v = raw_obj.get(k)
            if isinstance(v, list):
                out_list = v
                break
        if not out_list:
            out_list = [raw_obj]

    by_id: Dict[int, Dict[str, Any]] = {}
    for item in out_list:
        if not isinstance(item, dict):
            continue
        try:
            rid = int(item.get("id") or 0)
        except Exception:
            continue
        if rid <= 0:
            continue
        # title ≤ 50
        title_raw = str(item.get("title") or "").strip()
        if len(title_raw) > TITLE_MAX_LEN:
            title_raw = title_raw[:TITLE_MAX_LEN].rsplit(" ", 1)[0] if " " in title_raw[:TITLE_MAX_LEN] else title_raw[:TITLE_MAX_LEN]
        title = title_raw or f"Clip {rid}"
        # hook 20 words
        hook_raw = str(item.get("hook") or "").strip()
        words = [w for w in _WORD_SPLIT_RE.split(hook_raw) if w]
        if len(words) > HOOK_MAX_WORDS:
            words = words[:HOOK_MAX_WORDS]
        hook = " ".join(words) or title
        # tags 3个英文
        tags_raw = item.get("tags") or []
        if isinstance(tags_raw, str):
            tags_raw = [t for t in re.split(r"[,\s#]+", tags_raw) if t]
        tags_list = [_sanitize_tag(t) for t in tags_raw if t]
        # 补满/截断到3个
        base_tags = tags_list[:TAGS_EXPECTED]
        while len(base_tags) < TAGS_EXPECTED:
            base_tags.append(f"clip{rid}")
        by_id[rid] = {
            "id": rid,
            "title": title,
            "hook": hook,
            "tags": base_tags,
        }

    # 按 expected_ids 顺序输出，缺失的用兜底填充（批量容错：单条失败不中断整批）
    final: List[Dict[str, Any]] = []
    for eid in expected_ids:
        if eid in by_id:
            final.append(by_id[eid])
        else:
            final.append({
                "id": eid,
                "title": f"Clip {eid}",
                "hook": f"Watch this highlight clip {eid} now.",
                "tags": [f"clip{eid}", "highlight", "shorts"],
            })
    return final


class OllamaAgent:
    """
    Ollama llama3:8b 本地文本推理封装。
    红线1：绝对不启动 subprocess 调用 ollama kill/stop；仅通过 HTTP API 请求体 keep_alive:"0" 释放模型内存。
    """

    def __init__(
        self,
        endpoint: str = OLLAMA_DEFAULT_ENDPOINT,
        model: str = OLLAMA_MODEL,
        log_fn: Optional[Callable[[str, str], None]] = None,
        error_log_fn: Optional[Callable[[str], None]] = None,
        memory_watcher=None,  # MemoryWatcher | None
        cancel_event: Optional[threading.Event] = None,
        cache_dir: Optional[Path] = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self._log = log_fn or (lambda m, l="INFO": None)
        self._errlog = error_log_fn or (lambda m: None)
        self._watcher = memory_watcher  # 可选：提供则在推理中轮询内存
        self._cancel = cancel_event or threading.Event()
        self._cache_dir = Path(cache_dir) if cache_dir else None

        # 推理中标记（供内存监控轮询函数使用）
        self._infer_in_progress = threading.Event()
        # 已生成缓存（持续<0.5GB中断时返回）
        self._partial_results_lock = threading.Lock()
        self._partial_results: Dict[int, Dict[str, Any]] = {}

    # ============================================================
    #  外部取消钩子（由 pipeline cancel 调用）
    # ============================================================
    def cancel(self):
        """红线1：只置 cancel_event → 推理循环下一个 tick 退出，不杀任何进程"""
        self._log("[ollama_agent] 用户取消推理（仅置取消标记，禁止碰ollama serve进程）", "WARN")
        self._cancel.set()

    # ============================================================
    #  内部：标准 HTTP POST (urllib 标准库，零额外依赖)
    # ============================================================
    def _http_post_json(self, path: str, payload: Dict[str, Any], timeout: int = 600) -> Optional[Dict[str, Any]]:
        if _urllib_request is None:
            self._errlog("[OllamaAgent._http_post_json] urllib unavailable")
            return None
        url = f"{self.endpoint}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = _urllib_request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with _urllib_request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            txt = body.decode("utf-8", errors="replace")
            try:
                return json.loads(txt) if txt else None
            except Exception as je:
                self._log(f"[ollama] 响应不是JSON：{str(je)[:80]}；raw tail={txt[-400:]}", "WARN")
                # 尝试从文本里提取首段 [...] JSON 数组
                m = re.search(r"\[[\s\S]*\]", txt)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except Exception:
                        pass
                return None
        except _HTTPError as he:
            try:
                errtxt = he.read().decode("utf-8", errors="replace")
            except Exception:
                errtxt = ""
            self._errlog(f"[OllamaAgent] HTTP {he.code}: {errtxt[:600]}")
            return None
        except _URLError as ue:
            self._errlog(f"[OllamaAgent] URLError {type(ue).__name__}: {ue}")
            return None
        except Exception as e:
            self._errlog(f"[OllamaAgent] _http_post_json {type(e).__name__}: {e}")
            return None

    # ============================================================
    #  公共 API: 连接 / 模型可用性探测（调用推理前使用）
    # ============================================================
    def probe(self) -> Dict[str, Any]:
        """
        探测 ollama /api/tags 是否包含 llama3:8b。
        红线1：本探测只发 HTTP，不启动/不杀进程。
        """
        try:
            if _urllib_request is None:
                return {"ok": False, "reason": "urllib_unavailable"}
            url = f"{self.endpoint}/api/tags"
            req = _urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
            with _urllib_request.urlopen(req, timeout=5) as resp:
                tags_obj = json.loads(resp.read().decode("utf-8", errors="replace"))
            models = tags_obj.get("models") or []
            names = [m.get("name") for m in models if isinstance(m, dict)]
            matched = self.model in names
            return {
                "ok": matched,
                "models": names,
                "target_model": self.model,
                "reason": "" if matched else f"model '{self.model}' not found; available={names}",
            }
        except Exception as e:
            self._errlog(f"[OllamaAgent.probe] {type(e).__name__}: {e}")
            return {"ok": False, "reason": f"{type(e).__name__}: {e}", "models": [], "target_model": self.model}

    # ============================================================
    #  公共 API: 批量高光文案推理（主入口）
    #  clips_meta：阶段3 score.json 里的 clips 列表（每条含 index/start/end/score/text）
    #  返回 {ok, canceled, groups_total, results_normalized:[{id,title,hook,tags}...], partial_saved_path, error}
    # ============================================================
    def highlight_batch(
        self,
        clips_meta: List[Dict[str, Any]],
        cache_key: Optional[str] = None,
        progress_cb: Optional[Callable[[int, str], None]] = None,
        request_timeout: int = 600,
    ) -> Dict[str, Any]:
        result = {
            "ok": False,
            "canceled": False,
            "groups_total": 0,
            "results_normalized": [],
            "partial_saved_path": "",
            "error": "",
        }
        if not isinstance(clips_meta, list) or not clips_meta:
            result["error"] = "clips_meta 空"
            self._log("[ollama] 空 clips_meta，跳过推理", "WARN")
            return result

        # 1) 先读缓存（需求2：断点续存；命中则直接返回）
        if cache_key and self._cache_dir:
            cached = self._read_cache(cache_key)
            if cached is not None:
                self._log(f"[ollama] 命中缓存 key={cache_key}", "INFO")
                result["ok"] = True
                result["results_normalized"] = cached
                result["groups_total"] = 1  # 缓存已合并
                return result

        # 2) OLLAMA_LOW_VRAM 校验（app.py 开头必须已设置；这里仅日志提示）
        low_vram = os.environ.get("OLLAMA_LOW_VRAM")
        self._log(f"[ollama] OLLAMA_LOW_VRAM env = {low_vram!r}", "INFO")

        # 3) 分组（≤2组，按700token阈值）
        groups = _split_into_groups(clips_meta)
        result["groups_total"] = len(groups)
        # 粗略估算
        prompt_tokens_sum = 0
        for g in groups:
            prompt_tokens_sum += _estimate_tokens(_build_prompt(g))
        self._log(
            f"[ollama] 将 {len(clips_meta)} clips 拆成 {len(groups)} 组，预计总prompt tokens≈{prompt_tokens_sum} "
            f"(单组分界 {TOKEN_WARN_THRESHOLD})",
            "INFO"
        )
        if prompt_tokens_sum // max(1, len(groups)) >= TOKEN_WARN_THRESHOLD:
            self._log(f"[ollama] ⚠️ 单组预估tokens≥{TOKEN_WARN_THRESHOLD}，已分组为最多{MAX_GROUPS}组", "WARN")

        all_normalized: List[Dict[str, Any]] = []

        try:
            self._infer_in_progress.set()

            # 可选：推理期间启动内存守护线程（每2秒轮询）
            watcher_thread = None
            if self._watcher is not None:
                watcher_thread = threading.Thread(
                    target=self._watcher.poll_during_inference_loop,
                    args=(lambda: self._infer_in_progress.is_set() and not self._cancel.is_set(),),
                    name="ollama-mem-watcher",
                    daemon=True,
                )
                watcher_thread.start()

            abort_reason = None
            for gi, group in enumerate(groups, start=1):
                if self._cancel.is_set():
                    abort_reason = "canceled"
                    result["canceled"] = True
                    break
                pct = int(5 + 85 * ((gi - 0.9) / len(groups)))
                msg = f"阶段4：Ollama 推理 第{gi}/{len(groups)}组（keep_alive:0释放模型）"
                if progress_cb:
                    try:
                        progress_cb(pct, msg)
                    except Exception:
                        pass

                prompt = _build_prompt(group)
                etokens = _estimate_tokens(prompt)
                self._log(f"[ollama] 组{gi} clips={len(group)} est_tokens={etokens} model={self.model}", "INFO")

                # 标准固定请求体（必须与需求1.1逐字段一致）
                payload = {
                    "model": self.model,
                    "prompt": prompt,
                    "options": {
                        "num_ctx": OLLAMA_NUM_CTX,
                    },
                    "keep_alive": "0",    # 字符串 "0"；请求体字段释放模型内存（红线1）
                    "format": "json",     # 强制 JSON 输出
                    "stream": False,      # 非流式
                }
                resp = self._http_post_json("/api/generate", payload, timeout=request_timeout)

                if self._cancel.is_set():
                    abort_reason = "canceled"
                    result["canceled"] = True
                    break

                # 解析 model_response → JSON → normalize
                model_raw_text = ""
                if isinstance(resp, dict):
                    model_raw_text = str(resp.get("response") or resp.get("content") or "")
                parsed_obj: Any = None
                if model_raw_text:
                    try:
                        parsed_obj = json.loads(model_raw_text)
                    except Exception:
                        # 容错：提取第一个 [...]
                        m = re.search(r"\[[\s\S]*?\]", model_raw_text)
                        if m:
                            try:
                                parsed_obj = json.loads(m.group(0))
                            except Exception:
                                parsed_obj = None
                elif isinstance(resp, list) or isinstance(resp, dict):
                    parsed_obj = resp

                expected_ids = [int(c.get("index") or 0) for c in group]
                normalized = _normalize_output(parsed_obj, expected_ids)
                # 存部分结果（持续<0.5GB中断时也有产出）
                with self._partial_results_lock:
                    for n in normalized:
                        self._partial_results[int(n["id"])] = n
                all_normalized.extend(normalized)

                # 完成单组 → 步骤冷却30s（需求3.3）
                self._log(f"[ollama] 组{gi}完成，步骤冷却30s（分层冷却/swap保护）", "INFO")
                if self._watcher is not None:
                    if self._watcher.cool_down_step(30):
                        abort_reason = "canceled"
                        result["canceled"] = True
                        break
                else:
                    slept = 0.0
                    while slept < 30 and not self._cancel.is_set():
                        time.sleep(0.2)
                        slept += 0.2
                    if self._cancel.is_set():
                        abort_reason = "canceled"
                        result["canceled"] = True
                        break

            # 循环结束
            self._infer_in_progress.clear()
            if watcher_thread is not None:
                try:
                    watcher_thread.join(timeout=8)
                except Exception:
                    pass

            if abort_reason and abort_reason != "canceled" and not all_normalized:
                # 持续<0.5GB 中断 → 保存部分缓存（需求1.3）
                partials = list(self._partial_results.values())
                if partials:
                    pth = self._save_partial_results(cache_key, partials)
                    result["partial_saved_path"] = str(pth) if pth else ""
                    self._log(
                        f"[ollama] 推理因内存不足中断，已保存部分缓存 {len(partials)} 条: {result['partial_saved_path']}",
                        "WARN"
                    )
                    result["results_normalized"] = partials
                    result["error"] = f"aborted due to: {abort_reason}"
                else:
                    result["error"] = f"aborted due to: {abort_reason}"
                return result

            # 去重合并（跨组时 id 唯一）
            dedup: Dict[int, Dict[str, Any]] = {}
            for n in all_normalized:
                dedup[int(n["id"])] = n
            final_list = [v for _, v in sorted(dedup.items(), key=lambda kv: kv[0])]
            result["results_normalized"] = final_list

            if result["canceled"] and not final_list:
                result["error"] = "canceled"
                return result

            # 写缓存（需求2断点续存）
            if cache_key and self._cache_dir and not result["canceled"]:
                self._write_cache(cache_key, final_list)

            result["ok"] = True
            return result

        except Exception as e:
            self._errlog(f"[OllamaAgent.highlight_batch] unhandled {type(e).__name__}: {e}")
            self._log(f"[ollama] 未捕获异常: {e}", "ERROR")
            result["error"] = str(e)[:500]
            # 存部分
            with self._partial_results_lock:
                partials = list(self._partial_results.values())
            if partials:
                pth = self._save_partial_results(cache_key, partials)
                if pth:
                    result["partial_saved_path"] = str(pth)
            return result
        finally:
            self._infer_in_progress.clear()
            try:
                # 强制一次 keep_alive:"0" 空请求兜底（红线1：只通过API释放，不杀进程）
                self._release_model_fallback(timeout=20)
            except Exception:
                pass
            gc.collect()

    # ============================================================
    #  内部：缓存读写（断点续存）+ 部分结果落盘
    #  cache_key 生成由 app.py 负责：{文件名}_{字节大小}_{mtime}（需求2.1）
    # ============================================================
    def _cache_file(self, key: str) -> Optional[Path]:
        if not (key and self._cache_dir):
            return None
        safe = "".join(c for c in key if c.isalnum() or c in "-_.").strip() or "cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir / f"ollama_cache_{safe}.json"

    def _write_cache(self, key: str, normalized: List[Dict[str, Any]]):
        p = self._cache_file(key)
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump(
                    {"cache_key": key, "saved_at": int(time.time()),
                     "model": self.model, "results_normalized": normalized},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            self._errlog(f"[OllamaAgent._write_cache] {type(e).__name__}: {e}")

    def _read_cache(self, key: str) -> Optional[List[Dict[str, Any]]]:
        p = self._cache_file(key)
        if not p or not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if isinstance(obj, dict) and obj.get("cache_key") == key:
                res = obj.get("results_normalized")
                if isinstance(res, list):
                    return res
        except Exception as e:
            self._errlog(f"[OllamaAgent._read_cache] {type(e).__name__}: {e}")
        return None

    def _save_partial_results(self, key: Optional[str], partials: List[Dict[str, Any]]):
        if not self._cache_dir:
            return None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            suffix = (key or "no_key")
            safe_suffix = "".join(c for c in suffix if c.isalnum() or c in "-_.").strip() or "cache"
            p = self._cache_dir / f"partial_{ts}_{safe_suffix}.json"
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"partial": True, "saved_at": ts, "cache_key": key, "results": partials},
                          f, ensure_ascii=False, indent=2)
            return p
        except Exception as e:
            self._errlog(f"[OllamaAgent._save_partial_results] {type(e).__name__}: {e}")
            return None

    # ============================================================
    #  内部：兜底发一次 keep_alive:"0"，释放模型内存（红线1）
    # ============================================================
    def _release_model_fallback(self, timeout: int = 20) -> bool:
        try:
            payload = {
                "model": self.model,
                "prompt": "",
                "options": {"num_ctx": 1024},
                "keep_alive": "0",
                "stream": False,
                "format": "json",
            }
            # 没读取响应也没关系，发送到 API 让 ollama 收到 keep_alive:0 即可
            if _urllib_request is None:
                return False
            url = f"{self.endpoint}/api/generate"
            data = json.dumps(payload).encode("utf-8")
            req = _urllib_request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with _urllib_request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            self._log("[ollama] keep_alive:0 兜底请求已发送（红线1：仅靠API释放，不碰ollama serve进程）", "INFO")
            return True
        except Exception as e:
            self._errlog(f"[OllamaAgent._release_model_fallback] {type(e).__name__}: {e}")
            return False
