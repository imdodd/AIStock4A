#!/usr/bin/env python3
"""真实 LLM / 联网检索连通性实测（走本地 HTTP，真调一次，不落盘、不回显密钥）。

服务端 ``POST /api/config/test`` 本身不写任何业务数据，短超时，只回掩码与摘要；
本脚本同样不读密钥文件，只把它当作黑盒检查，因此切流前后都能用。

用法：
    python tools/verify_keys_live.py                              # 默认 8100（影子实例）
    python tools/verify_keys_live.py --base http://127.0.0.1:8000 # 切流后打生产端口
    python tools/verify_keys_live.py --json

退出码：LLM 与检索两项都 ok → 0；任一项失败 → 1。
配置页地址随实例端口走：<base>/static/config.html，填完保存即时生效，不用重启。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def post(base: str, path: str, payload: dict | None = None, timeout: int = 40) -> dict:
    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + path, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out: dict = {"base": a.base, "llm": {}, "search": {}, "ready": False}
    try:
        state = post(a.base, "/api/config/state", timeout=10).get("data") or {}
    except Exception as e:
        out["error"] = f"配置接口不可达：{type(e).__name__}: {e}"
        print(json.dumps(out, ensure_ascii=False, indent=1) if a.json else out["error"])
        return 1

    for name in ("llm", "search"):
        cfg = state.get(name) or {}
        out[name]["configured"] = bool(cfg.get("configured"))
        out[name]["masked"] = cfg.get("masked") or "(未配)"
        out[name]["source"] = cfg.get("source") or "none"
        try:
            r = post(a.base, "/api/config/test", {"target": name})["data"]
            out[name]["ok"] = bool(r.get("ok"))
            out[name]["reason"] = r.get("reason") or ""
            if name == "llm":
                out[name]["model"] = r.get("model") or ""
                out[name]["reply"] = r.get("reply") or ""
            else:
                out[name]["provider"] = r.get("provider") or ""
                out[name]["count"] = int(r.get("count") or 0)
        except Exception as e:
            out[name]["ok"] = False
            out[name]["reason"] = f"{type(e).__name__}: {e}"

    out["ready"] = bool(out["llm"]["ok"] and out["search"]["ok"])
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"[{a.base}] Key 实测（真调一次，不落盘、不回显明文）")
        for name, label in (("llm", "LLM 解读"), ("search", "联网检索")):
            d = out[name]
            flag = "OK  " if d["ok"] else "FAIL"
            extra = d.get("reply") or d.get("reason") or ""
            if name == "search" and d.get("ok"):
                extra = f"{d.get('provider')} 返回 {d.get('count')} 条"
            print(f"  {flag} {label}: configured={d['configured']} source={d['source']} "
                  f"masked={d['masked']} {extra}")
        print("  结论：" + ("两项通过，前置项 3 可打勾" if out["ready"]
                            else "未通过，切流前须补齐；到配置页填 Key 并保存后重跑"))
    return 0 if out["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
