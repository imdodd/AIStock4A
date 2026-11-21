"""校验 data/.secrets/llm.json 配置是否可用

用法：
    python -m tools.llm_check

输出三种结果之一：
    OK        配置有效，模型正常返回（会打印一行模型回话 + 耗时）
    NO_CONFIG api_key 为空或字段缺失，页内对话会退回规则文案
    ERROR     key 无效 / 余额不足 / 网络不通（会打印原始报错）
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = os.path.join(DATA_DIR, ".secrets", "llm.json")


def main() -> int:
    if not os.path.exists(SECRET):
        print(f"NO_CONFIG  {SECRET} 不存在")
        return 2
    try:
        cfg = json.load(open(SECRET, encoding="utf-8"))
    except Exception as e:
        print(f"NO_CONFIG  配置文件不是合法 JSON：{e}")
        return 2

    missing = [k for k in ("base_url", "api_key", "model") if not cfg.get(k)]
    if missing:
        print(f"NO_CONFIG  缺少字段：{', '.join(missing)}（api_key 为空即视为未配置）")
        return 2

    base = cfg["base_url"].rstrip("/")
    for tail in ("/chat/completions", "/v1/chat/completions"):
        if base.endswith(tail):
            base = base[: -len(tail)]
            break
    url = base + "/chat/completions"

    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "只回复两个字：可用"}],
        "stream": False,
        "max_tokens": 16,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['api_key']}"},
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
        used = d.get("usage") or {}
        print(f"OK  model={cfg['model']}  耗时 {time.time() - t0:.1f}s")
        print(f"    回话：{txt or '(空)'}")
        print(f"    token：prompt {used.get('prompt_tokens')} / completion {used.get('completion_tokens')}")
        return 0
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:400]
        print(f"ERROR  HTTP {e.code}  {url}")
        print(f"    {raw}")
        print("    401 → key 填错或被删；402/429 → 余额不足或限流；404 → base_url 或 model 名不对")
        return 1
    except Exception as e:
        print(f"ERROR  {type(e).__name__}: {e}  （{url}）")
        print("    检查网络 / 代理是否拦截 api.deepseek.com")
        return 1


if __name__ == "__main__":
    sys.exit(main())
