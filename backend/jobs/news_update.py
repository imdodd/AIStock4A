"""消息模块：30 分钟增量更新的落盘入口。

自动化任务只负责「检索最近 30~60 分钟的新资讯 + 做影响分析」，把结果写成一个
patch JSON 交给本脚本；本脚本负责去重、合并、重算统计、原子落盘、清理过期文件。
这样 AI 不必直接改当天文件，避免覆盖已有条目或把同一条新闻反复写进来。

用法（工作目录 server/）：
  python -m jobs.news_update --status                      # 看当天状态与近期标题（供增量判重）
  python -m jobs.news_update --patch ../.tmp/news_patch.json
  python -m jobs.news_update --patch ... --dry-run        # 只预览，不写不删
  python -m jobs.news_update --purge --days 30 --dry-run  # 只做保留期清理

patch 结构：
  {"summary": "...", "window": "...", "source_of_truth": "...", "items": [ {...} ]}
  items 每条字段与 news.py 的单日契约一致；title / url / source / time 必备。
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)                        # 允许 import news / env_loader

# CLI 子进程必须自己加载 .env；否则手工运行或调度拉起时只继承到部分环境变量。
import env_loader  # noqa: E402
env_loader.load_env()

# STORAGE_READ=db 时，写后同步钩子必须装在 news 模块上。
# 少了这一步，news.save 只落文件不回库，而 8000 服务因库读看不到新增资讯。
import storage_runtime  # noqa: E402
storage_runtime.install_from_env()

import news  # noqa: E402

TRACK_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                "from", "spm", "share_token", "ref", "src", "_", "timestamp"}
ID_PARAMS = {"id", "article_id", "articleid", "nid", "sid", "docid", "newsid", "postid"}


def norm_url(u: str) -> str:
    """规范化 URL 做判重：去协议/跟踪参数/尾部斜杠，保留文章 id 类参数。"""
    u = (u or "").strip()
    if not u:
        return ""
    try:
        p = urlsplit(u)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
             if k.lower() not in TRACK_PARAMS and (k.lower() in ID_PARAMS or len(v) < 40)]
        path = p.path.rstrip("/") or "/"
        return (p.netloc.lower().removeprefix("www.") + path
                + ("?" + urlencode(sorted(q)) if q else ""))
    except Exception:
        return u.lower().rstrip("/")


def norm_title(t: str) -> str:
    """只保留字母/数字/汉字，去掉空白与各种全半角标点，供模糊判重。"""
    return "".join(ch for ch in (t or "").lower() if ch.isalnum())


def keys_of(it: dict) -> tuple[str, str]:
    return norm_url(it.get("url")), norm_title(it.get("title"))


def similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.86


def is_dup(candidate: dict, existing: list[dict]) -> bool:
    cu, ct = keys_of(candidate)
    for old in existing:
        ou, ot = keys_of(old)
        if cu and ou and cu == ou:
            return True
        if similar(ct, ot):
            return True
    return False


def valid_item(it: dict) -> bool:
    """红线：每条必须有真实可核对的标题、来源链接、媒体名与时间。"""
    return (isinstance(it, dict)
            and bool(str(it.get("title") or "").strip())
            and str(it.get("url") or "").startswith(("http://", "https://"))
            and bool(str(it.get("source") or "").strip())
            and bool(str(it.get("time") or "").strip()))


def read_patch(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if not isinstance(d, dict):
        raise ValueError("patch 顶层必须是对象")
    if not isinstance(d.get("items") or [], list):
        raise ValueError("patch.items 必须是数组")
    return d


def apply(day: str, patch: dict, dry_run: bool, no_purge: bool) -> dict:
    old = news.load(day) or {}
    items_old = list(old.get("items") or [])
    raw = patch.get("items") or []

    fresh, dup, invalid = [], 0, 0
    for it in raw:
        if not valid_item(it):
            invalid += 1
            continue
        if is_dup(it, items_old) or is_dup(it, fresh):
            dup += 1
            continue
        fresh.append(dict(it))

    # 新增置顶；同一批内按 time 倒序，保证页面「最新在前」
    fresh.sort(key=lambda x: str(x.get("time") or ""), reverse=True)
    used_ids = {str(x.get("id")) for x in items_old}
    stamp = datetime.now().strftime("%H%M%S")
    for i, it in enumerate(fresh, 1):
        nid = f"inc-{day.replace('-', '')}-{stamp}-{i}"
        if not it.get("id") or str(it["id"]) in used_ids:
            it["id"] = nid
        used_ids.add(str(it["id"]))

    merged = fresh + items_old
    summary = (patch.get("summary") or "").strip()
    window = (patch.get("window") or "").strip()
    changed = (bool(fresh)
               or bool(summary and summary != (old.get("summary") or ""))
               or bool(window and window != (old.get("window") or "")))

    result = {
        "ok": True, "date": day, "dry_run": dry_run,
        "patch_items": len(raw), "added": len(fresh),
        "duplicates": dup, "invalid": invalid, "changed": changed,
        "total_before": len(items_old), "total_after": len(merged),
        "first_run": not bool(old), "file": None,
    }
    if changed and not dry_run:
        payload = {
            "date": day,
            "items": merged,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "window": window or old.get("window") or "当日滚动更新（每 30 分钟增量）",
            "summary": summary or old.get("summary") or "本期暂无总览，等待下一次增量更新。",
            "source_of_truth": patch.get("source_of_truth") or old.get("source_of_truth")
                               or "事实摘自所列公开报道；板块/个股影响为模型推断，不构成买卖建议。",
        }
        result["file"] = news.save(payload)
    if not no_purge:
        result["purge"] = news.purge(news.RETENTION_DAYS, dry_run=dry_run)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    ap.add_argument("--status", action="store_true", help="只输出当天状态")
    ap.add_argument("--patch", default=None, help="增量 patch JSON 路径")
    ap.add_argument("--purge", action="store_true", help="只做保留期清理")
    ap.add_argument("--days", type=int, default=news.RETENTION_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-purge", action="store_true", help="本次合并不顺带清理")
    a = ap.parse_args()
    day = a.date or datetime.now().strftime("%Y-%m-%d")

    if a.status:
        out = news.status(day)
    elif a.purge:
        out = news.purge(a.days, dry_run=a.dry_run)
    elif a.patch:
        out = apply(day, read_patch(a.patch), a.dry_run, a.no_purge)
    else:
        ap.print_help()
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
