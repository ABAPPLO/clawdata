"""视频文库主流程：订阅 -> 最新视频元数据/简介/字幕 -> 结构化提取 -> AI 总结 -> 文档落盘入库。

与「订阅博主」下载线共用 subscriptions 表但互不干扰：本模块只读订阅，
不下载视频、不改动订阅的检查时间。判重使用 digests 表自身的视频 ID。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any, Callable

from clawdata.collection import bilibili as bili_mod
from clawdata.collection.account_videos import fetch_account_videos, resolve_sec_uid
from clawdata.core.paths import DEFAULT_DIGEST_DIR, PROJECT_ROOT
from clawdata.core.session import load_cookies
from clawdata.digest import extract, llm
from clawdata.storage import store


ProgressFn = Callable[[str], None]

_PLATFORM_LABEL = {"bilibili": "B站", "douyin": "抖音"}


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def _fmt_ts(ts: Any) -> str:
    try:
        ts = int(ts or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ AI part
def _parse_summary_text(text: str) -> dict[str, Any]:
    """稳健解析模型输出的 JSON（summary/key_points/resources），失败则降级存原文。"""
    txt = (text or "").strip()
    match = re.search(r"\{.*\}", txt, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                points = obj.get("key_points") or []
                if isinstance(points, str):
                    points = [p.strip() for p in re.split(r"[\n、;；]+", points) if p.strip()]
                resources = obj.get("resources") or []
                if not isinstance(resources, list):
                    resources = []
                return {
                    "summary": str(obj.get("summary", "")).strip(),
                    "key_points": [str(p).strip() for p in points if str(p).strip()][:12],
                    "resources": [
                        {"type": str(r.get("type", "")), "value": str(r.get("value", "")),
                         "note": str(r.get("note", ""))}
                        for r in resources if isinstance(r, dict) and r.get("value")
                    ][:20],
                }
        except (ValueError, TypeError):
            pass
    return {"summary": _clip(txt, 500), "key_points": [], "resources": []}


def summarize_video(meta: dict[str, Any], links: list[dict[str, str]], cfg: dict[str, Any]) -> dict[str, Any]:
    """调用 ComfyUI 文本通道整理单个视频，返回 {summary, key_points, resources, raw}。"""
    link_lines = "\n".join(f"- [{l['type']}] {l['value']} {l['note']}".rstrip() for l in links) or "（无）"
    prompt = str(cfg.get("summary_prompt", "")).format(
        focus="；".join(cfg.get("focus_points") or []),
        title=meta.get("title", ""),
        author=meta.get("author", ""),
        pubdate=meta.get("pubdate", "") or "未知",
        desc=_clip(meta.get("desc", ""), int(cfg.get("desc_max_chars", 4000))),
        subtitle=_clip(meta.get("subtitle", ""), int(cfg.get("subtitle_max_chars", 12000))) or "（无字幕）",
        links=link_lines,
    )
    raw = llm.generate_text(prompt, cfg)
    result = _parse_summary_text(raw)
    result["raw"] = raw
    return result


# ---------------------------------------------------------------- doc render
def _merge_links(regex_links: list[dict[str, str]], resources: list[dict[str, Any]]) -> list[dict[str, str]]:
    merged = [dict(l) for l in regex_links]
    known = {l["value"] for l in merged}
    for r in resources:
        value = str(r.get("value", "")).strip()
        if value and value not in known:
            known.add(value)
            merged.append({"type": str(r.get("type", "") or "AI提取"),
                           "value": value, "note": str(r.get("note", ""))})
    return merged


def render_doc(record: dict[str, Any]) -> str:
    lines = [
        f"# {record.get('title', '')}",
        "",
        f"> **博主**：{record.get('author', '')} | **平台**：{_PLATFORM_LABEL.get(record.get('platform', ''), record.get('platform', ''))}"
        f" | **发布**：{record.get('pubdate', '') or '未知'} | **视频**：{record.get('url', '')}",
        "",
    ]
    if record.get("summary"):
        lines += ["## AI 总结", "", str(record["summary"]), ""]
    if record.get("key_points"):
        lines += ["## 要点", ""]
        lines += [f"- {p}" for p in record["key_points"]]
        lines.append("")
    links = record.get("links") or []
    if links:
        lines += ["## 提取的链接", "", "| 类型 | 地址 | 说明 |", "| --- | --- | --- |"]
        lines += [f"| {l.get('type', '')} | {l.get('value', '')} | {l.get('note', '')} |" for l in links]
        lines.append("")
    if record.get("desc_text"):
        lines += ["## 简介原文", "", str(record["desc_text"]).strip(), ""]
    if record.get("subtitle_text"):
        lines += ["## 字幕全文", "", str(record["subtitle_text"]).strip(), ""]
    if record.get("status") == "no_llm":
        lines += ["## 状态", "", f"AI 总结未生成：{record.get('error', '')}（可在面板重新整理）", ""]
    return "\n".join(lines)


def _write_doc(record: dict[str, Any]) -> str:
    os.makedirs(DEFAULT_DIGEST_DIR, exist_ok=True)
    name = f"{record.get('platform', 'video')}_{record.get('aweme_id', 'unknown')}.md"
    abs_path = os.path.join(DEFAULT_DIGEST_DIR, name)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(render_doc(record))
    return os.path.relpath(abs_path, PROJECT_ROOT)


# -------------------------------------------------------------- single video
def _record_from(
    sub: dict[str, Any],
    platform: str,
    *,
    aweme_id: str,
    title: str,
    author: str,
    url: str,
    pubdate: str,
    desc: str,
    subtitle: str,
    links: list[dict[str, str]],
    ai: dict[str, Any],
    status: str,
    error: str,
) -> dict[str, Any]:
    merged = _merge_links(links, ai.get("resources", []))
    return {
        "platform": platform,
        "aweme_id": aweme_id,
        "subscription_id": int(sub.get("id") or 0),
        "author": author or str(sub.get("account") or sub.get("name") or ""),
        "title": title,
        "homepage": str(sub.get("homepage") or ""),
        "url": url,
        "pubdate": pubdate,
        "desc_text": desc,
        "subtitle_text": subtitle,
        "links": merged,
        "summary": ai.get("summary", ""),
        "key_points": ai.get("key_points", []),
        "status": status,
        "error": error,
    }


def _digest_with_llm(
    db_path: str,
    record: dict[str, Any],
    cfg: dict[str, Any],
    llm_ok: bool,
    on_progress: ProgressFn,
) -> dict[str, Any]:
    """对已取到元数据的视频执行 AI 总结并落盘入库，返回入库后的完整记录。"""
    ai: dict[str, Any] = {"summary": "", "key_points": [], "resources": []}
    status, error = "done", ""
    if llm_ok:
        try:
            meta = {
                "title": record["title"], "author": record["author"],
                "pubdate": record["pubdate"], "desc": record["desc_text"],
                "subtitle": record["subtitle_text"],
            }
            ai = summarize_video(meta, record["links"], cfg)
        except Exception as exc:  # noqa: BLE001
            status, error = "no_llm", str(exc)[:500]
            on_progress(f"AI 总结失败：{error[:120]}")
    else:
        status, error = "no_llm", "ComfyUI 不可用，已跳过 AI 总结"
    record.update(summary=ai.get("summary", ""), key_points=ai.get("key_points", []),
                  links=_merge_links(record["links"], ai.get("resources", [])),
                  status=status, error=error)
    record["doc_path"] = _write_doc(record)
    dig_id = store.record_digest(db_path, **_digest_kwargs(record))
    record["id"] = dig_id
    return record


def _digest_kwargs(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("platform", "aweme_id", "subscription_id", "author", "title", "homepage",
            "url", "pubdate", "desc_text", "subtitle_text", "links", "summary",
            "key_points", "doc_path", "status", "error")
    return {k: record.get(k, "" if k != "subscription_id" else 0) for k in keys}


# ----------------------------------------------------------------- run loop
def run_digest(
    db_path: str,
    subscription_id: int = 0,
    limit: int | None = None,
    on_progress: ProgressFn = lambda _m: None,
) -> dict[str, Any]:
    """对订阅博主执行一轮「拉取最新 -> 整理入库」。返回统计。"""
    cfg = llm.load_config()
    per_limit = limit or int(cfg.get("per_subscription_limit", 5))
    stats = {"subscriptions": 0, "created": 0, "no_llm": 0, "errors": 0}

    if subscription_id:
        subs = [s for s in [store.get_subscription(db_path, subscription_id)] if s]
    else:
        subs = [s for s in store.list_subscriptions(db_path) if s.get("enabled", 1)]
    stats["subscriptions"] = len(subs)
    if not subs:
        on_progress("没有可用的订阅")
        return stats

    llm_failed_streak = 0
    llm_ok = True
    try:
        dy_cookies = load_cookies()
    except Exception:  # noqa: BLE001
        dy_cookies = {}
    bili_cookies = bili_mod.load_bilibili_cookies()
    known = store.existing_digest_ids(db_path)

    for idx, sub in enumerate(subs, start=1):
        name = sub.get("name") or sub.get("homepage")
        homepage = str(sub.get("homepage") or "")
        on_progress(f"({idx}/{len(subs)}) 检查「{name}」…")
        try:
            if bili_mod.is_space_url(homepage):
                mid = sub.get("sec_uid") or bili_mod.extract_mid(homepage)
                vids = bili_mod.fetch_user_videos(mid, count=30, cookies=bili_cookies)
                pending = [v for v in vids if v.get("bvid") and v["bvid"] not in known]
                on_progress(f"「{name}」B站最新 {len(vids)} 条，新视频 {len(pending)} 条")
                for v in pending[:per_limit]:
                    bvid = v["bvid"]
                    on_progress(f"整理 {bvid} {(v.get('title') or '')[:30]}")
                    info = bili_mod.fetch_video_info(bvid, bili_cookies)
                    subtitle = bili_mod.fetch_subtitle(bvid, info["cid"], bili_cookies)
                    desc = str(info.get("desc", ""))
                    links = extract.extract_links(desc, cfg.get("link_rules"))
                    record = _record_from(
                        sub, "bilibili",
                        aweme_id=bvid, title=str(info.get("title", "")),
                        author=str(info.get("author", "") or v.get("author", "")),
                        url=f"https://www.bilibili.com/video/{bvid}",
                        pubdate=_fmt_ts(info.get("pubdate") or v.get("created")),
                        desc=desc, subtitle=subtitle, links=links,
                        ai={}, status="done", error="",
                    )
                    _digest_with_llm(db_path, record, cfg, llm_ok, on_progress)
                    known.add(bvid)
                    stats["created"] += 1
                    if record.get("status") == "no_llm":
                        stats["no_llm"] += 1
                        llm_failed_streak += 1
                    else:
                        llm_failed_streak = 0
                    if llm_failed_streak >= 2:
                        llm_ok = False
                        on_progress("AI 连续失败，本轮后续视频将跳过 AI 总结（结构化信息照常入库）")
                    time.sleep(1.0)
            else:
                sec_uid = sub.get("sec_uid") or resolve_sec_uid(homepage, dy_cookies)
                vids = fetch_account_videos(sec_uid, dy_cookies, count=50)
                pending = [v for v in vids if str(v.get("aweme_id") or "") and str(v["aweme_id"]) not in known]
                on_progress(f"「{name}」抖音最新 {len(vids)} 条，新视频 {len(pending)} 条")
                for v in pending[:per_limit]:
                    aweme_id = str(v["aweme_id"])
                    desc = str(v.get("title") or "")
                    title = _clip(desc.splitlines()[0] if desc else aweme_id, 60)
                    on_progress(f"整理 {aweme_id} {title[:30]}")
                    links = extract.extract_links(desc, cfg.get("link_rules"))
                    record = _record_from(
                        sub, "douyin",
                        aweme_id=aweme_id, title=title,
                        author=str(v.get("author") or v.get("account") or ""),
                        url=str(v.get("url", "")),
                        pubdate=_fmt_ts(v.get("create_time")),
                        desc=desc, subtitle="", links=links,
                        ai={}, status="done", error="",
                    )
                    _digest_with_llm(db_path, record, cfg, llm_ok, on_progress)
                    known.add(aweme_id)
                    stats["created"] += 1
                    if record.get("status") == "no_llm":
                        stats["no_llm"] += 1
                        llm_failed_streak += 1
                    else:
                        llm_failed_streak = 0
                    if llm_failed_streak >= 2:
                        llm_ok = False
                        on_progress("AI 连续失败，本轮后续视频将跳过 AI 总结（结构化信息照常入库）")
                    time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            on_progress(f"「{name}」整理失败：{exc}")
    on_progress(f"整理完成：订阅 {stats['subscriptions']}，入库 {stats['created']}，"
                f"跳过 AI {stats['no_llm']}，失败 {stats['errors']}")
    return stats


def redigest(db_path: str, dig_id: int, on_progress: ProgressFn = lambda _m: None) -> dict[str, Any] | None:
    """对已入库的记录重新执行 AI 总结并更新文档。"""
    old = store.get_digest(db_path, dig_id)
    if not old:
        return None
    cfg = llm.load_config()
    record = {
        "platform": old.get("platform"), "aweme_id": old.get("aweme_id"),
        "subscription_id": old.get("subscription_id") or 0,
        "author": old.get("author"), "title": old.get("title"),
        "homepage": old.get("homepage"), "url": old.get("url"),
        "pubdate": old.get("pubdate"), "desc_text": old.get("desc_text") or "",
        "subtitle_text": old.get("subtitle_text") or "",
        "links": old.get("links") or [],
        "status": "done", "error": "",
    }
    on_progress(f"重新整理 {old.get('aweme_id')} {str(old.get('title'))[:30]}")
    _digest_with_llm(db_path, record, cfg, True, on_progress)
    return store.get_digest(db_path, dig_id)


# --------------------------------------------------------------------- ask
_STOPWORDS = {"的", "了", "吗", "呢", "有", "是", "什么", "哪些", "这个", "那个",
              "视频", "里面", "里", "中", "和", "与", "或", "以及", "一个", "怎么",
              "如何", "谁", "在哪", "请问", "下", "个", "提供", "提到", "讲了",
              "哪个", "哪些", "我们", "他们", "还是", "没有", "可以"}
# 问询常见意图词：中文无天然分词，先扫词表保证「代码仓库」这类组合能拆出来
_INTEREST_WORDS = [
    "代码", "仓库", "github", "gitee", "链接", "网盘", "百度盘", "夸克", "阿里盘",
    "飞书", "notion", "语雀", "文档", "资料", "工具", "教程", "步骤", "总结",
    "安装", "下载", "配置", "命令", "插件", "模型", "部署", "提取码", "字幕",
    "简介", "要点", "结论", "清单", "地址", "项目", "密码", "账号", "素材",
]


def split_keywords(question: str, max_words: int = 10) -> list[str]:
    """把自然语言问题拆成可检索关键词：兴趣词表 + 标点短片段 + 长片段二元滑窗。"""
    low = question.lower()
    kws: list[str] = [w for w in _INTEREST_WORDS if w in low]

    parts = re.split(r"[\s，。？！,?!、；;：:·\-/\\()（）\[\]【】\"'“”‘’]+", question)
    for p in parts:
        p = p.strip()
        if not p or p in _STOPWORDS:
            continue
        if 2 <= len(p) <= 6:
            if p not in kws:
                kws.append(p)
        else:
            # 长片段滑窗取 2 字词（中文无空格，靠窗口覆盖专名）
            for i in range(len(p) - 1):
                gram = p[i:i + 2]
                if gram in _STOPWORDS or gram in kws:
                    continue
                kws.append(gram)
                if len(kws) >= max_words + 6:
                    break
    return kws[:max_words]


def ask(db_path: str, question: str, limit: int = 6) -> dict[str, Any]:
    """基于文库检索 + AI 生成回答。返回 {answer, refs}。"""
    question = (question or "").strip()
    if not question:
        return {"answer": "请输入问题。", "refs": []}
    hits = store.search_digests(db_path, split_keywords(question), limit=limit)
    refs = [{"id": h["id"], "title": h["title"], "author": h["author"],
             "pubdate": h["pubdate"], "url": h["url"]} for h in hits]
    if not hits:
        return {"answer": f"文库中没有找到与「{question}」相关的资料。可以先在「视频文库」页整理更多视频。",
                "refs": []}
    cfg = llm.load_config()
    blocks = []
    for i, h in enumerate(hits, start=1):
        parts = [f"[{i}] 标题：{h['title']}", f"博主：{h['author']} | 发布：{h['pubdate'] or '未知'}"]
        if h.get("summary"):
            parts.append(f"摘要：{h['summary']}")
        if h.get("desc_text"):
            parts.append(f"简介：{_clip(h['desc_text'], 1000)}")
        if h.get("subtitle_text"):
            parts.append(f"字幕节选：{_clip(h['subtitle_text'], 1500)}")
        blocks.append("\n".join(parts))
    prompt = str(cfg.get("ask_prompt", "")).format(
        question=question, context="\n\n".join(blocks)
    )
    try:
        answer = llm.generate_text(prompt, cfg, timeout=600)
    except Exception as exc:  # noqa: BLE001
        answer = f"AI 问答失败：{exc}\n\n以下是与「{question}」最相关的文库条目，可直接点击查看："
    return {"answer": answer, "refs": refs}
