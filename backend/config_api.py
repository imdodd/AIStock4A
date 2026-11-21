"""Key 自助配置（默认开启、仅本机、仅 POST 端点）。

为什么是 POST-only：在线查询能力全部走 **GET**、配置写入走 **POST**，
职责分离且配置端点不会扩张在线查询的路由面。

为什么写这两个文件而不是只写 .env：
  * ``ai.config()`` 每次调用重读 ``data/.secrets/llm.json``（**文件优先**）；
  * ``jobs.fetch_news._cfg()`` 每次调用重读 ``data/.secrets/search.json``（环境变量为空时）；
  两者都是运行时读，写入即生效，不需要重启。档位类（PRODUCER / SEARCH_PROVIDER）
  由 ``app_profile`` / ``get_provider()`` 从 ``os.environ`` 读，所以写 .env 持久化后
  还要同步当前进程的环境变量，才能「保存即生效」。

安全红线：
  1. 只接受本机请求（HOST 不是 127.0.0.1/localhost 时直接不挂载）；
  2. 只接受固定白名单键，值做长度 / 控制字符 / URL 协议 / 枚举校验；
  3. 状态与保存结果**只回掩码**（前 6 … 后 4），永不回显密钥明文；
  4. 原子写 + 尽力 chmod 600；``.env`` 保留原有注释与未涉及的行；
  5. 密钥只进 ``data/.secrets/*.json``，不写进 .env、不进日志、不进快照。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request

import env_loader
import ops_config

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SECRETS_DIR = os.path.join(DATA_DIR, ".secrets")
LLM_FILE = os.path.join(SECRETS_DIR, "llm.json")
SEARCH_FILE = os.path.join(SECRETS_DIR, "search.json")
ENV_FILE = os.path.join(ROOT, ".env")

router = APIRouter(prefix="/api/config", tags=["config"])

# 本机地址白名单：写密钥的接口绝不对局域网/公网开放
_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"}

LLM_DEFAULTS: dict[str, Any] = {
    "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat",
    "temperature": 0.4,
    "api_key": "",
}
SEARCH_DEFAULTS: dict[str, Any] = {
    "backend": "tavily",
    "api_key": "",
    "url": "",
    "results_path": "results",
    "max_queries": 4,
    "max_results": 5,
}

# 服务商预设：只含公开的接口地址与模型名，绝不含任何密钥
PRESETS: list[dict[str, str]] = [
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-chat"},
    {"id": "deepseek_reasoner", "name": "DeepSeek 推理版", "base_url": "https://api.deepseek.com/v1",
     "model": "deepseek-reasoner"},
    {"id": "qwen", "name": "通义千问（百炼兼容模式）",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"id": "custom", "name": "自定义（OpenAI 兼容）", "base_url": "", "model": ""},
]

# 允许从页面写的档位（非密钥）。其它环境变量一律拒绝，防止被用来改 HOST/PORT/DB 等。
ENV_FIELDS: dict[str, dict[str, Any]] = {
    "PRODUCER": {"choices": ("rule", "llm_direct", "agent", "manual"),
                 "note": "AI 解读通道；无 Key 时 llm_direct 自动退规则"},
    "SEARCH_PROVIDER": {"choices": ("none", "search_api", "llm_websearch"),
                        "note": "资讯检索档；llm_websearch=LLM服务端联网搜索（需 deepseek-flash，按次计费）"},
}
LLM_FIELDS = ("base_url", "model", "api_key", "temperature")
SEARCH_FIELDS = ("backend", "api_key", "url", "results_path",
                "max_queries", "max_results")
URL_FIELDS = {"base_url", "url"}
INT_FIELDS = {"max_queries", "max_results"}
SECRET_FIELDS = {"api_key"}

MAX_LEN = 512
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

# ---------- URL 域名白名单（防「改 base_url 保留密钥」把密钥送出去） ----------
# 场景：本机服务无登录，任何能访问配置页的人（含恶意网页借浏览器本机身份发起
# 的请求）都可能改 base_url 为攻击者服务器再触发连通性测试，服务端就会带着
# Bearer 密钥请求新地址 → 密钥外送。因此 URL 只允许已知服务商域名（可经环境
# 变量扩展），以及本机自建推理服务（http://127.0.0.1 / localhost）。
_KNOWN_LLM_HOSTS = {
    "api.deepseek.com", "dashscope.aliyuncs.com", "api.openai.com",
    "api.moonshot.cn", "open.bigmodel.cn", "api.siliconflow.cn",
    "ark.cn-beijing.volces.com", "api.lingyiwanwu.com", "api.baichuan-ai.com",
    "api.minimax.chat", "api.lkeap.cloud", "generativelanguage.googleapis.com",
    "api.anthropic.com", "api.x.ai",
}
_KNOWN_SEARCH_HOSTS = {
    "api.tavily.com", "api.searchapi.io", "google.serper.dev",
    "api.bing.microsoft.com", "api.search.brave.com", "serpapi.com",
}


def _ok(data: Any) -> dict:
    return {"code": 0, "data": data}


def mask(value: str | None) -> str:
    """只回掩码，绝不回完整密钥。"""
    v = str(value or "").strip()
    if not v:
        return ""
    return v[:6] + "…" + v[-4:] if len(v) > 12 else "（已设置）"


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _atomic_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass                              # Windows 上 chmod 可能无意义，忽略


def _quote(value: str) -> str:
    """值里含 # / 空格 / 引号时用双引号包住（与 env_loader.parse 规则一致）。"""
    esc = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + esc + '"' if any(c in value for c in ('#', '"', " ", "\t", "\\")) else value


def upsert_env(pairs: dict[str, str], path: str | None = None) -> dict:
    """把档位写回 .env：保留注释与未涉及的行，只替换/追加白名单键。

    ``path`` 显式传入（默认 ENV_FILE），与 ``_state()`` 展示的 env_file 保持同一
    份文件；不用 ``env_loader.find_env_file()`` 是为了避免 ASTOCK_ENV_FILE 指向
    别处时「写一份、显示另一份」。
    """
    path = path or ENV_FILE
    lines: list[str] = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    seen: set[str] = set()
    out: list[str] = []
    key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
    for line in lines:
        m = key_re.match(line)
        key = m.group(1) if m else None
        if key and key in pairs:
            out.append(f"{key}={_quote(pairs[key])}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in pairs.items():
        if key not in seen:
            if out and out[-1].strip() and not out[-1].lstrip().startswith("#"):
                out.append("")
            out.append(f"{key}={_quote(value)}")
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out).rstrip("\n") + "\n")
    os.replace(tmp, path)
    return {"path": path, "written": sorted(pairs)}


def _state() -> dict:
    llm = {**LLM_DEFAULTS, **_read_json(LLM_FILE)}
    search = {**SEARCH_DEFAULTS, **_read_json(SEARCH_FILE)}
    env_llm = (os.environ.get("LLM_API_KEY") or "").strip()
    env_search = (os.environ.get("SEARCH_API_KEY") or "").strip()
    llm_key = str(llm.get("api_key") or "").strip() or env_llm
    search_key = str(search.get("api_key") or "").strip() or env_search
    return {
        "enabled": True,
        "local_only": True,
        "presets": PRESETS,
        "llm": {
            "configured": bool(llm_key),
            "masked": mask(llm_key),
            "base_url": str(llm.get("base_url") or LLM_DEFAULTS["base_url"]),
            "model": str(llm.get("model") or LLM_DEFAULTS["model"]),
            "temperature": llm.get("temperature", 0.4),
            "source": ("both" if env_llm and str(llm.get("api_key") or "").strip()
                       else "env" if env_llm else "file" if str(llm.get("api_key") or "").strip()
                       else "none"),
            "file": "data/.secrets/llm.json",
        },
        "search": {
            "configured": bool(search_key),
            "masked": mask(search_key),
            "backend": str(search.get("backend") or "tavily"),
            "url": str(search.get("url") or ""),
            "results_path": str(search.get("results_path") or "results"),
            "max_queries": search.get("max_queries", 4),
            "max_results": search.get("max_results", 5),
            "source": ("both" if env_search and str(search.get("api_key") or "").strip()
                       else "env" if env_search else "file" if str(search.get("api_key") or "").strip()
                       else "none"),
            "file": "data/.secrets/search.json",
        },
        "switches": {k: (os.environ.get(k) or v_default(k))
                     for k in ENV_FIELDS},
        "ui": _ui_state(),
        "env_fields": {k: v["note"] for k, v in ENV_FIELDS.items()},
        "choices": {k: list(v["choices"]) for k, v in ENV_FIELDS.items()},
        "env_file": os.path.relpath(ENV_FILE, ROOT).replace(os.sep, "/"),
        "env_exists": os.path.isfile(ENV_FILE),
        "notice": "密钥只写入本机 data/.secrets/，永不回显明文；保存后即时生效，档位重启后仍保持。",
    }


def _ui_state() -> dict:
    """界面风格：深色 / 浅色。和密钥一起在同一页配置，但不存在 .secrets/。"""
    try:
        import ui_config
        return ui_config.describe()
    except Exception:
        return {"theme": "light", "choices": ["dark", "light"]}


def v_default(key: str) -> str:
    from app_profile import config
    cfg = config()
    return str(cfg.get("producer") or "llm_direct") if key == "PRODUCER" \
        else str(cfg.get("search_provider") or "none")


def _check_local(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host not in _LOCAL_HOSTS:
        raise HTTPException(403, "配置接口只允许本机访问（127.0.0.1 / localhost）")


def _extra_hosts(env_key: str) -> set[str]:
    """环境变量追加的白名单域名（逗号分隔），给自建网关/代理留口子。"""
    raw = (os.environ.get(env_key) or "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _check_url(field: str, value: str) -> None:
    """URL 字段（llm.base_url / search.url）域名白名单校验。

    放行：https + 已知服务商域名；本机推理服务（127.0.0.1/localhost，http/https）。
    其它一律拒绝，可用 LLM_BASE_URL_EXTRA / SEARCH_URL_EXTRA 追加。
    """
    if not value:
        return
    try:
        from urllib.parse import urlsplit
        u = urlsplit(value)
        host = (u.hostname or "").lower()
    except Exception:
        host = ""
    local = host in {"127.0.0.1", "localhost", "::1"} or host.endswith(".local")
    if local and u.scheme in ("http", "https"):
        return
    if u.scheme != "https":
        raise HTTPException(400, f"{field} 仅允许 https:// 地址（本机服务可用 http://127.0.0.1）")
    allow = ((_KNOWN_LLM_HOSTS if field == "base_url" else _KNOWN_SEARCH_HOSTS)
             | _extra_hosts("LLM_BASE_URL_EXTRA" if field == "base_url" else "SEARCH_URL_EXTRA"))
    if host not in allow:
        raise HTTPException(400, f"{field} 的域名 {host or '(无法解析)'} 不在白名单内；"
                                 f"如使用自建网关，可用 LLM_BASE_URL_EXTRA / SEARCH_URL_EXTRA 添加")


def _clean_value(field: str, value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise HTTPException(400, f"{field} 值类型不合法")
    s = str(value).strip()
    if len(s) > MAX_LEN:
        raise HTTPException(400, f"{field} 超过 {MAX_LEN} 字符上限")
    if _CTRL.search(s):
        raise HTTPException(400, f"{field} 含控制字符/换行")
    if field in URL_FIELDS and s and not s.startswith(("http://", "https://")):
        raise HTTPException(400, f"{field} 必须是 http:// 或 https:// 开头的地址")
    if field in INT_FIELDS:
        try:
            return max(1, min(20, int(s or "1")))
        except ValueError as e:
            raise HTTPException(400, f"{field} 必须是整数") from e
    if field == "temperature":
        try:
            return max(0.0, min(2.0, float(s or "0.4")))
        except ValueError:
            return 0.4
    return s


@router.post("/state")
def state(request: Request):
    """读取脱敏配置状态（掩码 + 档位），供配置页初始化。不返回任何密钥明文。"""
    _check_local(request)
    return _ok(_state())


@router.post("/save")
def save(request: Request, payload: dict = Body(...)):
    """保存配置。body::

        {"llm": {"base_url": "...", "model": "...", "api_key": "..."},
         "search": {"backend": "tavily", "api_key": "..."},
         "switches": {"PRODUCER": "llm_direct", "SEARCH_PROVIDER": "none"},
         "clear": ["llm.api_key", "search.api_key"]}

    ``api_key`` 留空表示**保持原值**（页面只显示掩码，不回传明文）；要删除必须
    走 ``clear``。返回只含键名与掩码。
    """
    _check_local(request)
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是对象")

    llm_in = payload.get("llm") or {}
    search_in = payload.get("search") or {}
    switches = payload.get("switches") or {}
    ui_in = payload.get("ui") or {}
    clear = payload.get("clear") or []
    if not all(isinstance(x, dict) for x in (llm_in, search_in, switches, ui_in)):
        raise HTTPException(400, "llm / search / switches / ui 必须是对象")
    if not isinstance(clear, list):
        raise HTTPException(400, "clear 必须是数组")

    llm = {**LLM_DEFAULTS, **_read_json(LLM_FILE)}
    search = {**SEARCH_DEFAULTS, **_read_json(SEARCH_FILE)}
    changed: list[str] = []

    for field, raw in llm_in.items():
        if field not in LLM_FIELDS:
            raise HTTPException(400, f"不允许的 LLM 字段：{field}")
        v = _clean_value(field, raw)
        if field == "api_key" and v == "":      # 空 = 保持原值
            continue
        if field == "base_url":
            _check_url("base_url", v)           # 白名单：防止改地址带走密钥
        llm[field] = v
        changed.append("llm." + field)
    for field, raw in search_in.items():
        if field not in SEARCH_FIELDS:
            raise HTTPException(400, f"不允许的检索字段：{field}")
        v = _clean_value(field, raw)
        if field == "api_key" and v == "":
            continue
        if field == "url":
            _check_url("url", v)                # 同上：检索密钥也只发给白名单地址
        search[field] = v
        changed.append("search." + field)

    for item in clear:
        if item == "llm.api_key":
            llm["api_key"] = ""
            changed.append("llm.api_key(清空)")
        elif item == "search.api_key":
            search["api_key"] = ""
            changed.append("search.api_key(清空)")
        else:
            raise HTTPException(400, f"不支持的清除项：{item}")

    env_pairs: dict[str, str] = {}
    for key, raw in switches.items():
        if key not in ENV_FIELDS:
            raise HTTPException(400, f"不允许的档位键：{key}")
        v = str(_clean_value(key, raw)).strip()
        allowed = ENV_FIELDS[key]["choices"]
        if v not in allowed:
            raise HTTPException(400, f"{key} 只能是 {', '.join(allowed)}")
        env_pairs[key] = v
        changed.append("switch." + key)

    if "theme" in ui_in:
        try:
            import ui_config
            ui_config.set_theme(ui_in["theme"])
            changed.append("ui.theme")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    try:
        _atomic_json(LLM_FILE, llm)
        _atomic_json(SEARCH_FILE, search)
        env_result = upsert_env(env_pairs) if env_pairs else {"written": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"写入配置失败：{type(e).__name__}: {e}") from e

    # 档位即时生效：app_profile / get_provider 每次读 os.environ，重启后靠 .env 保持
    for key, value in env_pairs.items():
        os.environ[key] = value
    # 刷新运维页快照（按实例隔离），让监控页立刻显示新的「已配 / 未配」
    snap = ops_config.write_snapshot()

    return _ok({"changed": changed, "env_written": env_result.get("written", []),
               "snapshot": snap.get("file") if snap.get("ok") else None,
               "state": _state(),
               "hint": "已保存。密钥类改动即时生效；档位已同步当前进程，无需重启。"})


@router.post("/test")
def test(request: Request, payload: dict | None = Body(default=None)):
    """连通性测试：真调一次 LLM 或检索（不落盘、不回显密钥、短超时）。"""
    _check_local(request)
    target = str((payload or {}).get("target") or "llm").strip().lower()
    if target == "llm":
        try:
            import ai
            from producers.llm_direct import chat
            cfg = ai.config()
            if not cfg:
                return _ok({"ok": False, "target": "llm",
                            "reason": "未配置 LLM Key，请先在上方填写并保存"})
            r = chat(cfg, [{"role": "user", "content": "回复两个字：正常"}], timeout=20)
            text = str(r.get("content") or "").strip()
            return _ok({"ok": bool(text), "target": "llm", "model": cfg.get("model"),
                        "reply": text[:40], "reason": "" if text else "模型返回为空"})
        except Exception as e:
            return _ok({"ok": False, "target": "llm",
                        "reason": f"{type(e).__name__}: {str(e)[:160]}"})
    if target == "search":
        try:
            from jobs import fetch_news
            provider = os.environ.get("SEARCH_PROVIDER") or "none"
            if provider not in ("none", "search_api"):
                return _ok({"ok": False, "target": "search",
                            "reason": f"检索档 {provider} 尚未实现"})
            p = fetch_news.get_provider(provider)
            rows, errs = p.fetch(["A股 收盘 复盘"], 2)
            return _ok({"ok": bool(rows), "target": "search", "provider": p.name,
                        "count": len(rows), "reason": "；".join(errs)[:160] if errs else ""})
        except Exception as e:
            return _ok({"ok": False, "target": "search",
                        "reason": f"{type(e).__name__}: {str(e)[:160]}"})
    raise HTTPException(400, "target 只能是 llm 或 search")


def mount(app) -> bool:
    """默认开启；HOST 不是本机地址时拒绝挂载，避免写密钥接口暴露到公网。"""
    host = (os.environ.get("HOST") or "127.0.0.1").strip().lower()
    enabled = (os.environ.get("CONFIG_API_ENABLED") or "1").strip().lower()
    if enabled in ("0", "false", "no", "off"):
        print("[config] CONFIG_API_ENABLED=0，Key 自助配置未挂载")
        return False
    if host not in _LOCAL_HOSTS:
        print(f"[config] HOST={host} 不是本机地址，为安全起见拒绝挂载 Key 配置接口")
        return False
    app.include_router(router)
    return True


__all__ = ["router", "mount", "state", "save", "test", "mask", "upsert_env"]
