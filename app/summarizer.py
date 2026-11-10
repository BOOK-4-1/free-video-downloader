"""AI 视频总结：OpenAI 兼容协议 + 章节锁定/内容追加 + 独立内存任务表。

设计要点：
- 不引入 openai SDK，直接用项目已有的 httpx 打 /chat/completions，
  所以 AI_BASE_URL 可以指向任意兼容供应商（DeepSeek / 通义 / OpenAI / Ollama …）。
- 主端点失败自动切本地兜底端点，两者都失败给中文错误。
- 长视频用「章节锁定 + 内容追加」：首段生成并锁定章节大纲，后续每段只输出
  「要追加到哪一章的新要点」，由代码拼接，避免传统 map-reduce 越往后越简略。
- 任务表独立于下载任务表，避免和 TTL 文件清理互相干扰；不落库、不落盘。
- 生成过程通过 SSE 事件流增量下发（阶段 / 大纲 / 要点 / 完成），
  页面边生成边显示，全部完成后才解锁导出。
"""
import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from app import config, subtitle


class SummarizerError(Exception):
    """给用户看的错误（已经翻译成人话）"""


_tasks = {}
_lock = threading.RLock()   # acquire() 会在锁内调 create()，需要可重入
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ai")
MAX_KEPT = 50

# ---------------------------------------------------------------------------
# 结果复用：同一视频 + 同一登录态 + 同一风格，只生成一次
# ---------------------------------------------------------------------------
# 注意：不能去掉 p（B 站分 P 是不同内容）；t 是本项目笔记里跳回原片的时间戳。
TRACK_PARAMS = {
    "t", "spm_id_from", "vd_source", "share_source", "share_medium",
    "unique_k", "bbid", "ts", "_t", "seid", "timestamp",
}
LOGIN_KEYS = ("SESSDATA", "sessionid", "sessionid_ss", "sid_tt",
              "web_session", "sid", "sso_uid")


def normalize_url(url) -> str:
    """去掉跟踪参数 / 时间戳跳转 / 末尾斜杠，让同一视频的不同写法命中同一份笔记。"""
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        p = urlsplit(s)
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k.lower() not in TRACK_PARAMS and not k.lower().startswith("utm_")]
        path = p.path.rstrip("/") or "/"
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), path,
                           "&".join(f"{k}={v}" for k, v in query), ""))
    except ValueError:
        return s.rstrip("/")


def cookie_fingerprint(cookies) -> str:
    """只把登录态字段做指纹：换 Cookie 会换 key，但原文绝不落盘 / 下发浏览器。"""
    text = str(cookies or "")
    hits = []
    for name in LOGIN_KEYS:
        m = re.search(r"(?:^|[;\s])" + re.escape(name) + r"\s*=\s*([^;\s]+)", text, flags=re.I)
        if m:
            hits.append(name.lower() + "=" + m.group(1))
    return hashlib.md5("|".join(hits).encode("utf-8")).hexdigest()[:12] if hits else ""


def cache_key(url, cookies="", style="detailed") -> str:
    raw = f"{normalize_url(url)}|{cookie_fingerprint(cookies)}|{(style or 'detailed').strip()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


_index = {}   # cache_key -> task_id，与 _tasks 同生命周期，只存内存

STYLES = {
    "detailed": "详细笔记：要点写完整，保留关键细节、数据与例子。",
    "concise": "简明要点：只保留最重要的结论，每条不超过一句话。",
    "outline": "大纲为主：侧重结构，每章只给 1 到 2 条核心要点。",
}


# ---------------------------------------------------------------------------
# OpenAI 兼容客户端
# ---------------------------------------------------------------------------
def _endpoints() -> list:
    out = []
    if config.AI_BASE_URL and config.AI_MODEL:
        out.append((config.AI_BASE_URL, config.AI_API_KEY, config.AI_MODEL, "cloud"))
    if config.AI_FALLBACK_BASE_URL and config.AI_FALLBACK_MODEL:
        out.append((config.AI_FALLBACK_BASE_URL, config.AI_API_KEY, config.AI_FALLBACK_MODEL, "local"))
    return out


def is_ready() -> bool:
    return bool(_endpoints())


class StreamUnsupported(Exception):
    """供应商不支持 stream=True / 返回了非 SSE 内容 → 自动降级为一次性请求"""


def _post_once(base, key, model, messages) -> str:
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    # 不传 temperature / response_format：部分推理模型与国产兼容端点不支持，
    # 少一个参数就少一类 400；JSON 靠下面的 _extract_json 容错解析。
    payload = {"model": model, "messages": messages, "stream": False}
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=config.AI_TIMEOUT)
    except httpx.TimeoutException as e:
        raise SummarizerError(f"AI 调用超时（{config.AI_TIMEOUT} 秒），可调大 .env 里的 AI_TIMEOUT") from e
    except httpx.RequestError as e:
        raise SummarizerError(f"连不上 AI 服务（{type(e).__name__}），检查 AI_BASE_URL 与网络/代理设置") from e
    if r.status_code in (401, 403):
        raise SummarizerError("AI 密钥无效或没有权限（401/403），检查 .env 里的 AI_API_KEY")
    if r.status_code == 404:
        raise SummarizerError("AI 接口地址或模型名不对（404），检查 AI_BASE_URL / AI_MODEL")
    if r.status_code >= 400:
        raise SummarizerError(f"AI 服务返回 {r.status_code}：{r.text[:120]}")
    try:
        return r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise SummarizerError("AI 返回结构不符合 OpenAI 兼容协议（缺 choices[0].message.content）") from e


def _post_stream(base, key, model, messages, on_delta) -> str:
    """流式读取 OpenAI 兼容的 SSE delta，边读边上报累计字数（用于活体指示）。"""
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model, "messages": messages, "stream": True}
    parts, chars = [], 0
    try:
        with httpx.Client(timeout=config.AI_TIMEOUT) as client:
            with client.stream("POST", url, json=payload, headers=headers) as r:
                if r.status_code != 200:
                    body = r.read().decode("utf-8", errors="replace")[:160]
                    # 中转站常见「不支持 stream」的响应，降级而非直接判失败
                    if r.status_code in (400, 404, 415, 501) or "stream" in body.lower():
                        raise StreamUnsupported(body)
                    if r.status_code in (401, 403):
                        raise SummarizerError("AI 密钥无效或没有权限（401/403），检查 .env 里的 AI_API_KEY")
                    raise SummarizerError(f"AI 服务返回 {r.status_code}：{body[:120]}")
                for line in r.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == "[DONE]":
                        break
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        parts.append(delta)
                        chars += len(delta)
                        on_delta(chars)
    except httpx.TimeoutException as e:
        raise SummarizerError(f"AI 调用超时（{config.AI_TIMEOUT} 秒），可调大 .env 里的 AI_TIMEOUT") from e
    except httpx.RequestError as e:
        raise SummarizerError(f"连不上 AI 服务（{type(e).__name__}），检查 AI_BASE_URL 与网络/代理设置") from e
    text = "".join(parts).strip()
    if not text:
        raise StreamUnsupported("流式响应里没有正文")
    return text


def _post(base, key, model, messages, on_delta=None) -> str:
    if on_delta is None:
        return _post_once(base, key, model, messages)
    try:
        return _post_stream(base, key, model, messages, on_delta)
    except StreamUnsupported:
        return _post_once(base, key, model, messages)


def _chat(messages, on_delta=None):
    """返回 (正文, 供应商标记)。主端点失败自动切本地兜底。"""
    eps = _endpoints()
    if not eps:
        raise SummarizerError(
            "尚未配置 AI 模型：把 .env.example 复制为 .env，填写 AI_BASE_URL / AI_API_KEY / AI_MODEL；"
            "或只填 AI_FALLBACK_BASE_URL / AI_FALLBACK_MODEL 用本地模型"
        )
    errors = []
    for base, key, model, tag in eps:
        try:
            return _post(base, key, model, messages, on_delta), tag
        except SummarizerError as e:
            errors.append(f"{tag}：{e}")
    raise SummarizerError("AI 调用失败 → " + "；".join(errors))


def _extract_json(text) -> dict:
    """LLM 常把 JSON 包在 ```json 代码块里，或前后带解释，这里统一容错"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            obj = json.loads(t[a:b + 1])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    raise SummarizerError("AI 返回的不是有效 JSON，换个模型或把 AI_MODEL 换成指令遵循更好的模型再试")


# ---------------------------------------------------------------------------
# 时间码 / 分段
# ---------------------------------------------------------------------------
def _mmss(sec) -> str:
    s = int(sec or 0)
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    return f"{h}:{m:02d}:{ss:02d}" if h else f"{m:02d}:{ss:02d}"


def _parse_time(s) -> int:
    bits = str(s or "0").strip().split(":")
    try:
        nums = [float(x) for x in bits]
    except ValueError:
        return 0
    if len(nums) == 3:
        return int(nums[0] * 3600 + nums[1] * 60 + nums[2])
    if len(nums) == 2:
        return int(nums[0] * 60 + nums[1])
    return int(nums[0]) if nums else 0


def _jump(url, sec) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={max(0, int(sec))}"


def _line(seg) -> str:
    return f"[{_mmss(seg['start'])}] {seg['text']}"


def _split_chunks(segments, max_chars, max_segments):
    """按字符预算切段；超过上限时均匀合并，保证首尾都不丢。返回 (chunks, truncated)"""
    chunks, cur, size = [], [], 0
    for seg in segments:
        n = len(_line(seg)) + 1
        if cur and size + n > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(seg)
        size += n
    if cur:
        chunks.append(cur)
    # 单段但内容偏长时强制对半切：保证「大纲 + 至少一次追加」链路能跑起来。
    # 否则模型要在一次调用里同时产出大纲和全片要点，后半段章节容易整片空着。
    if len(chunks) == 1:
        total = sum(len(_line(s)) + 1 for s in segments)
        half = len(segments) // 2
        if total > 3000 and half > 0:
            chunks = [segments[:half], segments[half:]]
    if len(chunks) > max_segments:
        step = len(chunks) / max_segments
        merged = []
        for i in range(max_segments):
            a, b = int(i * step), int((i + 1) * step)
            merged.append([s for c in chunks[a:b] for s in c])
        return merged, True
    return chunks, False


def _skeleton(segments, max_chars=1500, max_lines=40) -> str:
    """全片等距采样骨架：让首次大纲调用看到整条时间轴，而不是只看到开头。

    只用于规划章节，不参与要点提取；控制字符数，不额外增加 LLM 调用次数。
    """
    if not segments:
        return ""
    step = max(1, len(segments) // max_lines)
    picked = segments[::step]
    if picked[-1] is not segments[-1]:
        picked = picked + [segments[-1]]
    lines, size = [], 0
    for seg in picked:
        line = _line(seg)
        if size + len(line) > max_chars:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
def _style_hint(style) -> str:
    return STYLES.get((style or "").strip(), STYLES["detailed"])


def _outline_messages(meta, chunk, style, skeleton="", single=False):
    body = "\n".join(_line(s) for s in chunk)
    system = (
        "你是一个专业的视频学习笔记助手。只依据给定的字幕内容做整理，不要编造字幕里没有的信息。"
        "必须严格返回 JSON，不要输出任何解释文字。"
    )
    # 单段时「字幕片段」就是全片，措辞必须让模型覆盖所有章节，不能只写开头
    if single:
        task2 = "2) 为「字幕片段」里的全部内容提取要点，按时间码分配到对应章节；"
        task2 += "骨架里出现的后半段主题也要有要点，不能只写开头几章。\n\n"
        block_title = "【完整字幕片段】"
    else:
        task2 = "2) 只为「开头字幕片段」提取值得记入笔记的要点。\n\n"
        block_title = "【开头字幕片段】"
    skeleton_block = (
        "\n【全片骨架】下面是整条视频的等距采样（只用来把握主题走向与时间跨度，"
        "不要为骨架条目生成要点，也不要照抄骨架文字）：\n"
        f"{skeleton}\n"
    ) if skeleton else ""
    user = (
        f"视频标题：{meta['title']}\n视频时长：{meta['duration_text']}\n"
        f"笔记风格：{_style_hint(style)}\n\n"
        "你会拿到两份材料：「全片骨架」和「字幕片段」。请完成两件事：\n"
        "1) 依据全片骨架规划整份笔记的章节大纲，之后会被锁定、不再改写；\n"
        f"{task2}"
        "【大纲硬性要求】章节必须覆盖从 00:00 到片尾的完整时间轴，按时间先后排列；"
        "骨架若显示后半段出现新主题，必须为它单独设章节，不要把所有内容都归到开头几章。\n"
        f"{skeleton_block}\n"
        '严格返回如下 JSON（chapters 3-10 章；time 用 mm:ss 或 h:mm:ss；'
        "chapter 是 chapters 的索引，从 0 开始）：\n"
        '{"one_line":"一句话概览","chapters":[{"time":"00:00","title":"章节标题"}],'
        '"items":[{"chapter":0,"time":"00:12","points":["要点一","要点二"]}]}\n\n'
        f"{block_title}\n{body}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _append_messages(chapters, chunk, index, total, style):
    body = "\n".join(_line(s) for s in chunk)
    locked = json.dumps(
        [{"index": i, "time": c["time"], "title": c["title"]} for i, c in enumerate(chapters)],
        ensure_ascii=False,
    )
    system = (
        "你是一个专业的视频学习笔记助手。章节大纲已经锁定，你只能往已有章节里追加新要点，"
        "绝对不能改写、重排或新增章节。只依据给定字幕，不要编造。必须严格返回 JSON。"
    )
    user = (
        f"笔记风格：{_style_hint(style)}\n"
        f"已锁定的章节大纲：{locked}\n\n"
        f"下面是第 {index}/{total} 段字幕。请只输出这一段里值得追加的新要点，"
        "已经在大纲里体现过的内容不要重复；没有新内容就返回 {\"items\":[]}。\n"
        "按每条要点的时间码，把它归到时间区间覆盖该时刻的章节（参考大纲里各章的 time）；"
        "不要因为拿不准就把整段要点全塞进同一章。\n"
        '严格返回 JSON：{"items":[{"chapter":1,"time":"05:12","points":["要点一"]}]}\n\n'
        f"字幕片段：\n{body}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _normalize_chapters(raw) -> list:
    if not isinstance(raw, list) or not raw:
        raise SummarizerError("AI 没有生成章节大纲，换个模型再试")
    out = []
    for c in raw[:10]:
        if not isinstance(c, dict):
            continue
        title = str(c.get("title") or "").strip()[:40]
        if not title:
            continue
        out.append({"time": str(c.get("time") or "00:00").strip()[:12], "title": title})
    if not out:
        raise SummarizerError("AI 生成的章节大纲是空的，换个模型再试")
    return out


def _collect(points_map, raw, n_chapters) -> int:
    """把 LLM 返回的要点收进 points_map；章节索引越界直接丢弃，保证大纲不被改写"""
    if not isinstance(raw, list):
        return 0
    added = 0
    for it in raw:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("chapter", -1))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= n_chapters:
            continue
        t = str(it.get("time") or "00:00").strip()[:12]
        sec = _parse_time(t)
        points = it.get("points") or []
        if not isinstance(points, list):
            points = [points]
        for p in points:
            text = str(p).strip().replace("\n", " ")[:180]
            if text:
                points_map.setdefault(idx, []).append({"time": t, "sec": sec, "text": text})
                added += 1
    return added


def _redistribute_by_time(chapters, points_map) -> int:
    """按要点自带时间码把归错章的要点搬回正确章节（章节标题不变）。

    只在模型给的时间码「有区分度」时才做：若大半要点都写成同一个时间
    （典型是整章 00:00），这些值不可信，搬动只会把内容全挤进第 1 章，
    交给 _snap_times() 的等距重排处理更合适。
    """
    if not chapters:
        return 0
    all_secs = [p.get("sec") or 0 for pts in points_map.values() for p in pts]
    if len(set(all_secs)) < max(2, len(all_secs) // 4):
        return 0
    bounds = []
    for i, ch in enumerate(chapters):
        lo = _parse_time(ch["time"])
        hi = _parse_time(chapters[i + 1]["time"]) if i + 1 < len(chapters) else float("inf")
        bounds.append((lo, hi))
    rebuilt = {i: [] for i in range(len(chapters))}
    moved = 0
    for idx, pts in points_map.items():
        for p in pts:
            sec = p.get("sec") or 0
            target = idx
            for j, (lo, hi) in enumerate(bounds):
                if lo <= sec < hi:
                    target = j
                    break
            else:
                if sec >= bounds[-1][0]:
                    target = len(bounds) - 1
            moved += int(target != idx)
            rebuilt.setdefault(target, []).append(p)
    for i in range(len(chapters)):
        points_map[i] = rebuilt.get(i, [])
    return moved


def _snap_times(chapters, points_map, segments) -> None:
    """把要点时间码吸附到真实字幕时间点，杜绝「一整章全是 00:00」。

    LLM 常把同一章里所有要点的时间码都写成章节起始时间（prompt 里
    {"chapter":0,"time":"00:00","points":[...]} 的多个要点共享一个 time）。
    这里按要点在所属章节区间内的顺序，从真实字幕时间点里分配：
    若模型给的时间码大量重复，整章改为等距分配；否则吸附到最近的真实时间点。
    结果保证每条要点时间码真实存在、单调递增、互不重复。
    """
    times = sorted({int(s.get("start") or 0) for s in segments if (s.get("start") or 0) >= 0})
    if not times:
        return
    end = times[-1]
    bounds = []
    for i, ch in enumerate(chapters):
        lo = _parse_time(ch.get("time"))
        hi = _parse_time(chapters[i + 1]["time"]) if i + 1 < len(chapters) else end + 1
        bounds.append((lo, max(lo, hi)))

    for i, (lo, hi) in enumerate(bounds):
        pts = points_map.get(i) or []
        if not pts:
            continue
        pool = [t for t in times if lo <= t < hi]
        if not pool:
            pool = [t for t in times if lo <= t <= hi + 60] or [lo]
        given = [p.get("sec") for p in pts if isinstance(p.get("sec"), (int, float))]
        # 时间码高度重复（含全 00:00）时，模型给的值不可信，整章等距重排
        uniform = len(set(given)) < max(2, len(pts) // 2)
        n = len(pts)
        used = set()
        for j, p in enumerate(pts):
            if uniform:
                target = lo + (hi - lo) * (j / max(1, n))
            else:
                target = p.get("sec")
                if not isinstance(target, (int, float)) or not (lo <= target < hi):
                    target = lo + (hi - lo) * (j / max(1, n))
            nearest = min(pool, key=lambda t: abs(t - target))
            if nearest in used:
                rest = [t for t in pool if t not in used]
                nearest = min(rest, key=lambda t: abs(t - target)) if rest else nearest + 1
            used.add(nearest)
            p["sec"] = nearest
            p["time"] = _mmss(nearest)
        pts.sort(key=lambda p: p["sec"])


def _chapter_title_at(segments, sec) -> str:
    """补章时从该时间点的字幕里取一小段做标题，比「后续内容」更有信息量"""
    for s in segments:
        if (s.get("start") or 0) >= sec:
            txt = re.sub(r"[\s，。！？、,.!?]", "", str(s.get("text") or ""))[:12]
            return ("后续 · " + txt) if txt else "后续内容"
    return "后续内容"


def _ensure_coverage(chapters, segments) -> int:
    """大纲没覆盖到片尾时自动补章，避免后半段要点全部挤进前几章。

    返回补了几章，写进 stats 便于在笔记里说明。
    """
    if not chapters or not segments:
        return 0
    end = float(segments[-1].get("start") or 0)
    last = _parse_time(chapters[-1]["time"])
    if end <= 60 or last >= end * 0.8:
        return 0
    cands = sorted({int(s["start"]) for s in segments if last + 30 < s["start"] <= end})
    if not cands:
        return 0
    k = min(3, max(1, int((end - last) // 180)))
    step = len(cands) / (k + 1)
    picks = sorted({cands[min(len(cands) - 1, int((i + 1) * step))] for i in range(k)})
    for t in picks:
        chapters.append({"time": _mmss(t), "title": _chapter_title_at(segments, t)})
    return len(picks)


def _fill_empty_messages(chapters, points_map, segments, style):
    """为空章节生成一次「定向补要点」调用，避免笔记里出现一片空章。

    章节锁定后，后段章节常常只是被大纲列出来、却没分到要点（尤其是
    追加调用返回内容偏少时）。把所有空章连同各自区间内的字幕一次性交给
    模型补 1-3 条，只多 1 次调用，不改写大纲。
    """
    if not chapters or not segments:
        return None
    end = float(segments[-1].get("start") or 0)
    blocks = []
    for i, ch in enumerate(chapters):
        if points_map.get(i):
            continue
        lo = _parse_time(ch["time"])
        hi = _parse_time(chapters[i + 1]["time"]) if i + 1 < len(chapters) else end + 1
        body = "\n".join(_line(s) for s in segments if lo <= (s.get("start") or 0) < hi)[:1200]
        if not body.strip():
            continue
        blocks.append(f"【章节 {i}｜{ch['time']}｜{ch['title']}】\n{body}")
    if not blocks:
        return None
    system = (
        "你是一个专业的视频学习笔记助手。只依据给定的字幕内容，为指定章节补充要点，"
        "不要编造。必须严格返回 JSON，不要输出任何解释文字。"
    )
    user = (
        f"笔记风格：{_style_hint(style)}\n\n"
        "下面几个章节目前还没有任何要点，请分别为它们补充 1 到 3 条要点；"
        "时间码必须落在该章节的时间范围内，没有内容可写的章节可以跳过。\n"
        '严格返回 JSON：{"items":[{"chapter":1,"time":"05:12","points":["要点一"]}]}\n\n'
        + "\n\n".join(blocks)
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# 思维导图：单独一次调用，按「知识逻辑」重组成多层树，而不是复述笔记章节
# ---------------------------------------------------------------------------
MAP_MAX_BRANCHES = 5
MAP_MAX_CHILDREN = 3
MAP_MAX_LEAVES = 3
MAP_MAX_NODES = 48


def _point_material(chapters, points_map, max_chars=1800) -> str:
    """给导图模型看的「已提炼要点」素材，按章节摊平，控制长度。"""
    lines, size = [], 0
    for i, ch in enumerate(chapters):
        pts = (points_map.get(i) or [])[:3]
        if not pts:
            continue
        head = f"[{ch.get('time', '00:00')}] {ch.get('title', '')}"
        body = "；".join(str(p.get("text") or "") for p in pts)
        line = f"{head}：{body}"
        if size + len(line) > max_chars:
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def _mindmap_messages(meta, segments, chapters, points_map, one_line, style):
    """让模型独立产出一张「深入浅出」的知识树，输入是内容素材而非笔记结构。"""
    skeleton = _skeleton(segments, max_chars=3200, max_lines=70)
    material = _point_material(chapters, points_map)
    system = (
        "你是一位擅长把复杂知识讲给初学者听的学科教师，正在为一段教学视频绘制思维导图。"
        "只依据给定的字幕与要点素材，不要编造素材里没有的事实、数字或案例。"
        "必须严格返回 JSON，不要输出任何解释文字或代码块标记。"
    )
    user = (
        f"视频标题：{meta['title']}\n视频时长：{meta['duration_text']}\n"
        f"一句话概览：{one_line or '（无）'}\n"
        f"笔记风格：{_style_hint(style)}\n\n"
        "请绘制一张**独立于笔记章节结构**的思维导图。最重要的要求：\n"
        "1) 【不要照搬】禁止把笔记的章节标题按顺序改成主干，也不要把要点原文复制成节点；"
        "要按知识本身的内在逻辑重新归类、合并、抽象。\n"
        "2) 【层次递进】从中心主题出发，建议按这些认知维度组织主干（挑与内容最匹配的 3~5 个，"
        "不要生硬套用）：核心概念是什么 / 背后的因果机制 / 关键证据与案例 / 常见误区与边界 / "
        "怎么应用与延伸。每个主干下要有 2~3 个具体知识点，其中至少一半知识点还要再往下拆出"
        " 1~3 个例证、数据、反例或操作细节，整张图至少 3 层、尽量达到 4 层。\n"
        "3) 【深入浅出】label 是短标题（不超过 16 字），brief 用大白话解释这个节点"
        "（不超过 40 字），优先用类比、对比、具体例子，避免堆术语；越往深层越具体。\n"
        "4) 【可回看】叶子或子节点可选填 at（mm:ss 或 h:mm:ss），必须来自下面素材里"
        "真实出现的时间码，用于点击跳回原片；不确定就留空字符串。\n"
        "5) 主干 3~5 个，总节点不超过 45 个；所有文字用中文。\n\n"
        '严格返回如下 JSON（children 可嵌套 2 层，root/branches 为必填）：\n'
        '{"root":"中心主题（不超过 18 字）","summary":"整张图的核心结论（不超过 40 字）",'
        '"branches":[{"label":"主干","brief":"通俗解释",'
        '"children":[{"label":"知识点","brief":"解释","at":"02:15",'
        '"children":[{"label":"例证或细节","brief":"解释","at":"03:02"}]}]}]}\n\n'
        f"【字幕等距采样】\n{skeleton}\n\n"
        f"【已提炼要点（仅供理解内容，不要照抄成节点）】\n{material}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _nearest_time(sec, times, tol=90):
    """把模型给的时间码吸附到真实字幕时间点，防止编造造成跳错片。"""
    if not times or sec is None:
        return None
    t = min(times, key=lambda x: abs(x - sec))
    return t if abs(t - sec) <= tol else None


def _normalize_mindmap(obj, segments, title="") -> dict:
    """校验并裁剪模型返回的树：限制层数/节点数、吸附时间码、保证至少 3 层。"""
    if not isinstance(obj, dict):
        raise ValueError("导图不是 JSON 对象")
    branches = obj.get("branches")
    if not isinstance(branches, list) or not branches:
        raise ValueError("导图没有主干")
    times = sorted({int(s.get("start") or 0) for s in (segments or [])})
    budget = [MAP_MAX_NODES]

    def node(raw, depth):
        if not isinstance(raw, dict) or budget[0] <= 0:
            return None
        label = re.sub(r"\s+", " ", str(raw.get("label") or "").strip())[:24]
        if not label:
            return None
        budget[0] -= 1
        brief = re.sub(r"\s+", " ", str(raw.get("brief") or "").strip())[:60]
        at = str(raw.get("at") or "").strip()[:12]
        sec = _nearest_time(_parse_time(at), times) if at else None
        limit = {1: MAP_MAX_CHILDREN, 2: MAP_MAX_LEAVES}.get(depth, 0)
        kids = []
        if limit:
            src = raw.get("children")
            if isinstance(src, list):
                for item in src[:limit]:
                    kid = node(item, depth + 1)
                    if kid:
                        kids.append(kid)
        return {"label": label, "brief": brief, "sec": sec, "children": kids}

    # 每个主干独立配额：否则前两个主干会把全局预算吃光，后面的主干整支消失，
    # 导图看起来「层次很深」但知识覆盖面残缺。
    valid = [b for b in branches[:MAP_MAX_BRANCHES]
             if isinstance(b, dict) and str(b.get("label") or "").strip()]
    per_branch = max(6, (MAP_MAX_NODES - 1) // max(1, len(valid)))
    out = []
    for b in valid:
        budget[0] = per_branch
        n = node(b, 1)
        if n:
            out.append(n)
    if len(out) < 2:
        raise ValueError("导图主干不足")

    def stats(nodes, depth=1):
        # 空 children 不能算一层，否则叶子会被多计一层深度
        if not nodes:
            return depth - 1, 0
        deepest, count = depth, 0
        for n in nodes:
            count += 1
            d, c = stats(n["children"], depth + 1)
            deepest, count = max(deepest, d), count + c
        return deepest, count

    depth, count = stats(out)
    if depth < 3 or count < 8:
        raise ValueError(f"导图层次不足（{depth} 层 / {count} 节点）")
    return {
        "root": str(obj.get("root") or title or "视频主题").strip()[:28],
        "summary": str(obj.get("summary") or "").strip()[:60],
        "branches": out,
        "meta": {"depth": depth + 1, "nodes": count + 1, "fallback": False},
    }


def _fallback_mindmap(meta, chapters, points_map) -> dict:
    """导图调用失败时的兜底：至少 3 层的笔记结构示意，绝不返回空图。"""
    branches = []
    for i, ch in enumerate(chapters[:MAP_MAX_BRANCHES]):
        kids = []
        for p in (points_map.get(i) or [])[:MAP_MAX_CHILDREN]:
            kids.append({
                "label": str(p.get("text") or "")[:24],
                "brief": "", "sec": p.get("sec"), "children": [],
            })
        branches.append({
            "label": ch.get("title") or f"第 {i + 1} 章",
            "brief": ch.get("time") or "",
            "sec": _parse_time(ch.get("time")),
            "children": kids,
        })
    return {
        "root": meta.get("title") or "视频主题",
        "summary": "导图模型未能生成，当前按笔记结构展示",
        "branches": branches,
        "meta": {"depth": 3, "nodes": sum(1 + len(b["children"]) for b in branches) + 1,
                 "fallback": True},
    }


def _render(meta, chapters, points_map, one_line, stats, draft=False) -> str:
    total_points = sum(len(v) for v in points_map.values())
    lines = [f"# {meta['title']} · AI 学习笔记", ""]
    lines.append(f"> 来源：{meta['url']}")
    lines.append(
        f"> 时长：{meta['duration_text']} ｜ 字幕覆盖：{meta.get('subtitle_end_text') or '未知'}"
        f" ｜ 字幕：{meta['lang']}{'（自动）' if meta['auto'] else ''}"
        f" ｜ 共 {len(chapters)} 章 / {total_points} 条要点"
    )
    lines.append("")
    if one_line:
        lines += ["## 一句话概览", "", one_line, ""]
    # 核心要点按整条时间轴均匀采样，不能只取每章第一条（否则全挤在开头几章）
    ranked = sorted((p for pts in points_map.values() for p in pts), key=lambda p: p.get("sec") or 0)
    if len(ranked) <= 6:
        takeaways = ranked
    else:
        idxs = sorted({round(k * (len(ranked) - 1) / 5) for k in range(6)})
        takeaways = [ranked[i] for i in idxs]
    if takeaways:
        lines += ["## 核心知识要点", ""]
        for p in takeaways:
            lines.append(f"- **[{p['time']}]({_jump(meta['url'], p['sec'])})** {p['text']}")
        lines.append("")
    lines += ["## 大纲与章节要点", ""]
    for i, ch in enumerate(chapters):
        sec = _parse_time(ch["time"])
        lines.append(f"### {i + 1}. [{ch['time']}]({_jump(meta['url'], sec)}) {ch['title']}")
        lines.append("")
        pts = points_map.get(i, [])
        if pts:
            for p in pts:
                lines.append(f"- **[{p['time']}]({_jump(meta['url'], p['sec'])})** {p['text']}")
        elif draft:
            lines.append("- *正在整理这一章的要点…*")
        else:
            lines.append("- （这一段没有提取到新增要点）")
        lines.append("")
    if stats.get("auto_chapters"):
        lines += [f"> 注意：章节大纲没有覆盖到片尾，已按字幕时间轴自动补充 {stats['auto_chapters']} 章。", ""]
    if stats.get("truncated"):
        lines += [f"> 注意：内容较长，已按 {stats['chunks']} 段等距抽样处理，细节可能有省略。", ""]
    if draft:
        lines += ["---", f"*生成中：{stats.get('stage_text') or '请稍候'}。全部完成后才能复制或导出。*"]
    else:
        lines += ["---", "*本笔记由 AI 依据平台字幕生成，仅供学习速览；关键结论请点击时间戳回原片核对。*"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 任务表
# ---------------------------------------------------------------------------
def _now() -> float:
    return time.time()


_HIDDEN_FIELDS = ("cookies", "cookie_file", "events", "aqueue", "loop")
_FINAL_EVENTS = ("done", "error")


def create(url, cookies="", cookie_file="", style="detailed", loop=None, ck="") -> dict:
    t = {
        "id": uuid.uuid4().hex[:12],
        "url": url,
        "cookies": cookies or "",
        "cookie_file": cookie_file or "",
        "style": (style or "detailed").strip(),
        "cache_key": ck or cache_key(url, cookies, style),
        "status": "queued",       # queued / running / done / error
        "stage": "queued",        # subtitles / outlining / expanding / done / error
        "percent": 0,
        "message": "排队中…",
        "result": None,
        "partial": None,          # 当前草稿快照，供 SSE 断线回退的轮询使用
        "error": "",
        "seq": 0,
        "events": [],             # 有限的事件历史，SSE 断线重连时补发
        "aqueue": asyncio.Queue() if loop is not None else None,
        "loop": loop,
        "created_at": _now(),
        "finished_at": 0,
    }
    with _lock:
        _tasks[t["id"]] = t
        _trim()
    return t


def _drop_index(t):
    k = t.get("cache_key")
    if k and _index.get(k) == t["id"]:
        _index.pop(k, None)


def _trim():
    if len(_tasks) <= MAX_KEPT:
        return
    done = sorted((t for t in _tasks.values() if t["status"] in ("done", "error")), key=lambda x: x["created_at"])
    for t in done[: len(_tasks) - MAX_KEPT]:
        _tasks.pop(t["id"], None)
        _drop_index(t)


def find_by_key(key):
    """按缓存键找仍可复用的任务；索引指向已清理任务时顺手擦掉。"""
    with _lock:
        tid = _index.get(key)
        t = _tasks.get(tid) if tid else None
        if t is None and tid:
            _index.pop(key, None)
        return t


def acquire(url, cookies="", cookie_file="", style="detailed", loop=None, force=False):
    """锁内完成「查 + 建」，保证并发重复提交也只生成一个任务。

    返回 (task, reused)。命中 queued/running/done 直接复用；error 视为未命中
    （网络抖动后用户可以立刻重试）；force=True 强制新建并让索引指向新任务。
    """
    key = cache_key(url, cookies, style)
    with _lock:
        old = find_by_key(key)
        if not force and old and old["status"] in ("queued", "running", "done"):
            return old, True
        t = create(url, cookies, cookie_file, style, loop, ck=key)
        _index[key] = t["id"]
        return t, False


def get(task_id):
    return _tasks.get(task_id)


def all_tasks() -> list:
    with _lock:
        return sorted(_tasks.values(), key=lambda t: t["created_at"], reverse=True)


def _update(task_id, **kw):
    with _lock:
        t = _tasks.get(task_id)
        if t:
            t.update(kw)


def serialize(task) -> dict:
    """下发给浏览器的结构：绝不把 Cookie / 临时路径 / 事件内部对象带出去"""
    return {k: v for k, v in task.items() if k not in _HIDDEN_FIELDS}


def _emit(task_id, event, data) -> None:
    """发一条 SSE 事件。_run() 在线程池里跑，用 call_soon_threadsafe 回到事件循环。"""
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t["seq"] += 1
        payload = {"id": t["seq"], "event": event, "data": data}
        t["events"].append(payload)
        if len(t["events"]) > 300:
            del t["events"][:-300]
        if event == "partial":
            t["partial"] = data
        q, loop = t.get("aqueue"), t.get("loop")
    if q is not None and loop is not None:
        try:
            loop.call_soon_threadsafe(q.put_nowait, payload)
        except RuntimeError:  # 事件循环已关闭
            pass


async def stream_events(task_id, after=0):
    """SSE 事件生成器：先补发 after 之后的历史，再等新事件；超时发心跳保活。"""
    t = get(task_id)
    if not t:
        return
    with _lock:
        backlog = [e for e in t["events"] if e["id"] > after]
        q, status = t.get("aqueue"), t["status"]
    for e in backlog:
        yield e
        if e["event"] in _FINAL_EVENTS:
            return
    if status in ("done", "error") or q is None:
        return
    while True:
        try:
            e = await asyncio.wait_for(q.get(), timeout=15)
        except asyncio.TimeoutError:
            yield {"id": 0, "event": "ping", "data": {}}
            continue
        if e["id"] <= after:
            continue
        yield e
        if e["event"] in _FINAL_EVENTS:
            return


def _draft(task_id, meta, chapters, points_map, one_line, stage_text, percent,
           segments_count=0, chunks=0) -> None:
    """把当前已生成的内容渲染成「草稿笔记」并作为 partial 事件下发。"""
    stats = {"stage_text": stage_text, "chunks": chunks, "truncated": False}
    md = _render(meta, chapters, points_map, one_line, stats, draft=True)
    data = {
        "markdown": md,
        "title": meta["title"],
        "url": meta["url"],
        "chapters": [{"time": c["time"], "title": c["title"]} for c in chapters],
        "total_points": sum(len(v) for v in points_map.values()),
        "stage": stage_text,
        "percent": percent,
        "segments": segments_count,
        "chunks": chunks,
    }
    _update(task_id, percent=percent, message=stage_text)
    _emit(task_id, "partial", data)


def _delta_reporter(task_id, label, call_index, total_calls):
    """流式 delta 回调，节流后发 token 事件做活体指示（不展示原始 JSON）。"""
    last = [0.0, 0]

    def report(chars):
        now = time.time()
        if now - last[0] < 0.35 and chars - last[1] < 120:
            return
        last[0], last[1] = now, chars
        _emit(task_id, "token", {"chars": chars, "call": call_index,
                                 "calls": total_calls, "label": label})

    return report


def start(task_id):
    _pool.submit(_run, task_id)


def cleanup(max_age=None):
    """清理过期结果并同步擦掉索引；TTL 由 .env 的 AI_NOTE_TTL 控制，默认 1 小时。"""
    ttl = max_age or config.AI_NOTE_TTL
    with _lock:
        for tid, t in list(_tasks.items()):
            if t["finished_at"] and _now() - t["finished_at"] > ttl:
                _tasks.pop(tid, None)
                _drop_index(t)


def _run(task_id):
    t = get(task_id)
    if not t:
        return
    try:
        _update(task_id, status="running", stage="subtitles", percent=5, message="正在读取字幕…")
        _emit(task_id, "stage", {"stage": "subtitles", "percent": 5, "message": "正在读取字幕…"})

        def on_sub_progress(cur, total, hint):
            _emit(task_id, "stage", {
                "stage": "subtitles", "percent": 6,
                "message": f"正在核对字幕（第 {cur}/{total} 次）：{hint}",
            })

        sub = subtitle.resolve(
            t["url"],
            has_cookie=bool(t["cookie_file"]),
            cookies_text=t["cookies"],
            cookie_file=t["cookie_file"],
            on_progress=on_sub_progress,
        )
        if not sub.get("ok"):
            raise SummarizerError(sub.get("reason") or "这条视频没拿到可用字幕")
        segments = sub["segments"]
        if not segments:
            raise SummarizerError("字幕是空的，无法生成笔记")

        chunks, truncated = _split_chunks(segments, config.AI_MAX_CHARS, config.AI_MAX_SEGMENTS)
        meta = {
            "title": sub.get("title") or t["url"],
            "url": t["url"],
            "duration_text": _mmss(sub.get("duration") or 0),
            "subtitle_end_text": sub.get("subtitle_end_text") or _mmss(segments[-1]["start"]),
            "lang": sub.get("lang") or "",
            "auto": bool(sub.get("auto")),
        }
        # 预估调用数：大纲 1 + 追加 (n-1) + 可能的补空章 1 + 独立导图 1
        est_calls = len(chunks) + 2
        ready_msg = (
            f"字幕已就绪（{len(segments)} 段 / 覆盖到 {meta['subtitle_end_text']}），"
            "正在生成章节大纲…"
        )
        _emit(task_id, "meta", {
            "title": meta["title"], "url": meta["url"],
            "duration_text": meta["duration_text"],
            "subtitle_end_text": meta["subtitle_end_text"],
            "lang": meta["lang"], "auto": meta["auto"],
            "segments": len(segments), "chunks": len(chunks),
            "subtitle_chars": sub.get("chars") or 0,
        })
        _update(task_id, stage="outlining", percent=15, message=ready_msg)
        _emit(task_id, "stage", {"stage": "outlining", "percent": 15, "message": ready_msg})

        skeleton = _skeleton(segments)
        raw, provider = _chat(
            _outline_messages(meta, chunks[0], t["style"], skeleton, single=(len(chunks) == 1)),
            _delta_reporter(task_id, "章节大纲", 1, est_calls),
        )
        obj = _extract_json(raw)
        chapters = _normalize_chapters(obj.get("chapters"))
        auto_chapters = _ensure_coverage(chapters, segments)
        one_line = str(obj.get("one_line") or "").strip()[:200]
        points_map = {}
        _collect(points_map, obj.get("items"), len(chapters))
        calls = 1
        _draft(task_id, meta, chapters, points_map, one_line,
               f"章节大纲已锁定（{len(chapters)} 章），正在逐段整理要点…",
               25, len(segments), len(chunks))

        for i, chunk in enumerate(chunks[1:], start=2):
            percent = round(15 + 70 * (i - 1) / max(1, len(chunks)), 1)
            msg = f"正在整理第 {i}/{len(chunks)} 段字幕…"
            _update(task_id, stage="expanding", percent=percent, message=msg)
            _emit(task_id, "stage", {"stage": "expanding", "percent": percent, "message": msg})
            raw, provider = _chat(
                _append_messages(chapters, chunk, i, len(chunks), t["style"]),
                _delta_reporter(task_id, f"第 {i}/{len(chunks)} 段", i, est_calls),
            )
            _collect(points_map, _extract_json(raw).get("items"), len(chapters))
            calls += 1
            total = sum(len(v) for v in points_map.values())
            _draft(task_id, meta, chapters, points_map, one_line,
                   f"已整理 {i}/{len(chunks)} 段，累计 {total} 条要点…",
                   percent, len(segments), len(chunks))

        redistributed = _redistribute_by_time(chapters, points_map)
        fill_msgs = _fill_empty_messages(chapters, points_map, segments, t["style"])
        filled = 0
        if fill_msgs:
            msg = "正在补全空章节要点…"
            _update(task_id, stage="expanding", percent=88, message=msg)
            _emit(task_id, "stage", {"stage": "expanding", "percent": 88, "message": msg})
            raw, provider = _chat(
                fill_msgs,
                _delta_reporter(task_id, "补全空章节", calls + 1, est_calls),
            )
            filled = _collect(points_map, _extract_json(raw).get("items"), len(chapters))
            calls += 1
            _draft(task_id, meta, chapters, points_map, one_line,
                   "要点补全完成，正在校正时间戳…", 92, len(segments), len(chunks))
        _snap_times(chapters, points_map, segments)
        _draft(task_id, meta, chapters, points_map, one_line,
               "笔记要点已齐，正在独立绘制知识结构…", 90, len(segments), len(chunks))

        # 导图是独立的一次生成：不笔记章节做骨架，而是按知识逻辑重组。
        # 这一步失败绝不能让整份笔记一起失败，降级为结构示意图并明确标注。
        map_msg = "正在生成富有层次的知识导图…"
        _update(task_id, stage="mindmap", percent=92, message=map_msg)
        _emit(task_id, "stage", {"stage": "mindmap", "percent": 92, "message": map_msg})
        mindmap_fallback, mindmap_error = False, ""
        try:
            raw, provider = _chat(
                _mindmap_messages(meta, segments, chapters, points_map, one_line, t["style"]),
                _delta_reporter(task_id, "知识导图", calls + 1, est_calls),
            )
            mindmap = _normalize_mindmap(_extract_json(raw), segments, meta["title"])
            calls += 1
        except Exception as e:  # noqa: BLE001 - 导图是附加能力，失败只降级
            mindmap = _fallback_mindmap(meta, chapters, points_map)
            mindmap_fallback, mindmap_error = True, f"{type(e).__name__}: {str(e)[:80]}"
            _emit(task_id, "stage", {
                "stage": "mindmap", "percent": 94,
                "message": "导图模型没返回合格结构，已按笔记结构生成示意图…",
            })

        _emit(task_id, "stage", {"stage": "finishing", "percent": 97,
                                 "message": "正在排版并准备导出…"})
        stats = {
            "segments": len(segments),
            "chunks": len(chunks),
            "llm_calls": calls,
            "provider": provider,
            "truncated": truncated,
            "subtitle_chars": sub.get("chars") or 0,
            "auto_chapters": auto_chapters,
            "redistributed": redistributed,
            "filled_points": filled,
            "map_depth": mindmap["meta"]["depth"],
            "map_nodes": mindmap["meta"]["nodes"],
            "map_fallback": mindmap_fallback,
        }
        markdown = _render(meta, chapters, points_map, one_line, stats)
        result = {
            "title": meta["title"],
            "url": meta["url"],
            "duration_text": meta["duration_text"],
            "subtitle_end_text": meta["subtitle_end_text"],
            "coverage": sub.get("coverage"),
            "lang": meta["lang"],
            "auto": meta["auto"],
            "one_line": one_line,
            "markdown": markdown,
            "outline": [
                {"time": c["time"], "title": c["title"], "sec": _parse_time(c["time"]),
                 "points": points_map.get(i, [])}
                for i, c in enumerate(chapters)
            ],
            "mindmap": mindmap,
            "map_error": mindmap_error,
            "stats": stats,
        }
        _update(task_id, status="done", stage="done", percent=100,
                message="总结完成", result=result, finished_at=_now())
        _emit(task_id, "done", {"result": result})
    except SummarizerError as e:
        msg = str(e)
        _update(task_id, status="error", stage="error", message=msg, error=msg, finished_at=_now())
        _emit(task_id, "error", {"message": msg})
    except Exception as e:  # noqa: BLE001
        msg = f"AI 总结失败（{type(e).__name__}）：{str(e)[:160]}"
        _update(task_id, status="error", stage="error", message=msg, error=msg, finished_at=_now())
        _emit(task_id, "error", {"message": msg})
