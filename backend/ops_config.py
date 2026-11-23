"""运维页的脱敏配置快照（按实例隔离）。

设计约束：**不新增任何 API 路由**（否则影子差分 32:32 的 GET 路由集合会被打破）。
所以监控页是静态 ``frontend/ops.html``，走已有的 ``/static`` 挂载；它需要的
「当前档位 / Key 是否已配」无法从环境变量里凭空读到，由本模块在服务启动期
写成一个静态 JSON，已 gitignore，不入库。

按实例隔离（修复「监控页显示错实例档位」）：
  旧实现所有实例都写同一个 ``frontend/ops-config.json``，自检脚本起的临时实例
  或别的影子实例一启动就把它覆盖，8100 的页面读到的其实是最后启动那个实例的档位。
  现在改成每个实例只写 ``frontend/ops-config.<实例标识>.json``，前端按
  ``location.port`` 取自己那一份；快照里同时带上 tag / pid / boot_at，前端拿
  ``/api/health`` 的实时 ``boot_at`` 一比对，就能识别「别的实例」或「上一次进程
  留下的旧快照」，给出明确告警而不是静默显示错误档位。

  ``ASTOCK_OPS_SNAPSHOT=0`` 可让自检 / 批处理这类临时进程完全不写快照文件
  （``build_snapshot()`` 仍可离线调用，验收不受影响）。

安全红线：
  * 快照里**只有档位字面值与「已配/未配」标记**，绝不写任何密钥内容；
  * 密钥来源只记 env / file / both / none，不记路径以外的内容；
  * 实例标识做字符白名单，防止端口/环境变量被拼成越界路径；
  * 写失败一律静默降级，绝不影响 Web 服务启动。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from typing import Any

import data_paths

# 档位项：key -> (默认值, 中文说明)
SWITCHES: dict[str, tuple[str, str]] = {
    "APP_PROFILE": ("oss", "运行档位：oss 开源 / private 私有"),
    "DATA_SOURCE": ("tencent", "行情主数据源"),
    "PRODUCER": ("llm_direct", "AI 解读通道（无 Key 自动退规则）"),
    "SEARCH_PROVIDER": ("none", "资讯检索档：none / search_api"),
    "CONFIG_API_ENABLED": ("1", "1 = 开启 Key 自助配置（仅 POST 端点）"),
    "STORAGE_BACKEND": ("sqlite", "存储后端（mysql / postgres 仅接口预留）"),
    "STORAGE_READ": ("files", "读路径：files 文件 / db 数据库"),
    "STORAGE_BOOTSTRAP": ("auto", "db 档空库时是否从文件自举"),
    "STORAGE_STRICT": ("0", "1 = 接线失败直接报错，不静默回退"),
    "STORAGE_DUAL_WRITE": ("0", "预留；db 档写后自动整表同步"),
    "ASTOCK_DATA_DIR": ("", "数据目录；空 = backend/data 冻结夹具，设路径 = 真实数据实例"),
    "ASTOCK_DB_PATH": ("backend/data/astock.db", "SQLite 库路径"),
    "ASTOCK_OWNER": ("default", "多用户预留的属主标识"),
    "SCHEDULER_ENABLED": ("0", "1 = 挂载 /api/jobs/* 并启动调度"),
    "SCHEDULER_BACKEND": ("auto", "auto / apscheduler / builtin"),
    "SCHEDULER_DRY_RUN": ("1", "默认演习：只记 job_runs 不抓行情"),
    "SCHEDULER_CATCHUP": ("1", "启动时按最近交易日追补缺跑任务"),
    "SCHEDULER_TRADING_DAYS_ONLY": ("1", "非交易日跳过"),
    "SCHEDULER_MISFIRE": ("7200", "触发延迟超过该秒数不补跑"),
    "PREGEN_ON_EOD": ("1", "EOD 真实执行后自动生成 digest + pregen"),
    "EOD_CRON": ("30 15 * * 1-5", "盘后 EOD 覆盖 cron（标准 crontab：1-5=周一到周五）"),
    "ASTOCK_CALENDAR": ("", "自定义节假日文件（空=用周末规则）"),
    "HOST": ("127.0.0.1", "监听地址；不要改成 0.0.0.0 暴露公网"),
    "PORT": ("8100", "监听端口"),
}

# 密钥项：只输出「已配/未配 + 来源 + 用途」，绝不输出值
SECRETS: dict[str, tuple[str, str, str]] = {
    # key: (标题, 环境变量名, 可选的文件路径（相对 backend/）)
    "LLM": ("AI 解读（OpenAI 兼容接口）", "LLM_API_KEY", "data/.secrets/llm.json"),
    "SEARCH": ("资讯联网检索", "SEARCH_API_KEY", "data/.secrets/search.json"),
}

_UNSAFE = re.compile(r"[^0-9A-Za-z_-]")
SNAPSHOT_PREFIX = "ops-config"
_FALLBACK_SNAPSHOT = "ops-config.json"     # 兼容旧版：只在分实例文件缺失时读，并明确告警


def _safe_tag(raw: str) -> str:
    """实例标识只允许字母数字下划线短横线，且限长，杜绝路径穿越。"""
    return (_UNSAFE.sub("", str(raw or "")).strip()[:24] or "8000")


def instance_tag() -> str:
    """当前实例的标识 —— 优先用 uvicorn 真实监听端口，而不是 .env 里的 PORT。

    取数顺序：显式 ASTOCK_OPS_TAG > 命令行 --port > PORT 环境变量 > 8000。
    自检脚本用 ``uvicorn --port <随机端口>`` 起临时实例，命令行里的端口才是它
    真实监听的端口；只看 .env 的 PORT 会误判成 8100 并写到 8100 的文件上。
    """
    explicit = (os.environ.get("ASTOCK_OPS_TAG") or "").strip()
    if explicit:
        return _safe_tag(explicit)
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            return _safe_tag(argv[i + 1])
        if arg.startswith("--port="):
            return _safe_tag(arg.split("=", 1)[1])
    port = (os.environ.get("PORT") or "").strip()
    return _safe_tag(port or "8000")


def snapshot_filename(tag: str | None = None) -> str:
    """本实例该写的静态快照文件名（位于 frontend/ 下）。"""
    return f"{SNAPSHOT_PREFIX}.{_safe_tag(tag or instance_tag())}.json"


# 进程级唯一启动时间戳：模块导入时取一次，之后不再变化。
# 快照可能在启动时写一次、在配置页保存时又重写一次；若每次都取 now()，
# 就会和 /api/health 的 boot_at 差一两秒，前端的来源校验会误报「别的实例」。
# main.py 的 /api/health 直接引用同一个常量，两边永远一致。
BOOT_STAMP = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _boot_stamp() -> str:
    """与 /api/health 的 boot_at 同格式，供前端比对快照是否来自当前进程。"""
    return BOOT_STAMP


def _secret_state(env_name: str, file_rel: str, backend: str) -> dict[str, Any]:
    from_env = bool((os.environ.get(env_name) or "").strip())
    file_abs = os.path.join(backend, *file_rel.split("/"))
    from_file = False
    try:
        with open(file_abs, encoding="utf-8") as f:
            d = json.load(f)
        from_file = bool((d.get("api_key") or d.get("key") or "").strip())
    except Exception:
        pass
    if from_env and from_file:
        source = "both"
    elif from_env:
        source = "env"
    elif from_file:
        source = "file"
    else:
        source = "none"
    return {
        "configured": from_env or from_file,
        "source": source,
        "env_var": env_name,
        "file": file_rel,
        "file_exists": os.path.isfile(file_abs),
        "note": "文件优先" if env_name == "LLM_API_KEY" else "环境变量优先",
    }


def build_snapshot() -> dict[str, Any]:
    backend = os.path.dirname(os.path.abspath(__file__))
    switches = []
    for key, (default, note) in SWITCHES.items():
        raw = os.environ.get(key)
        switches.append({
            "key": key,
            "value": raw if raw not in (None, "") else default,
            "is_default": raw in (None, ""),
            "note": note,
        })
    secrets = []
    for name, (title, env_name, file_rel) in SECRETS.items():
        st = _secret_state(env_name, file_rel, backend)
        st.update({"id": name, "title": title})
        secrets.append(st)
    tag = instance_tag()
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "instance": {
            "tag": tag,
            "port": tag,
            "host": (os.environ.get("HOST") or "127.0.0.1").strip(),
            "pid": os.getpid(),
            "boot_at": _boot_stamp(),
            "file": snapshot_filename(tag),
        },
        "switches": switches,
        "data_dir": data_paths.describe(),
        "secrets": secrets,
        "notice": "本快照不含任何密钥值；改档位后需重启服务，改 Key 在配置页保存即时生效。",
    }


def write_snapshot() -> dict[str, Any]:
    """原子写**本实例专属**的静态快照；任何异常都吞掉（运维页会自行降级）。

    ``ASTOCK_OPS_SNAPSHOT=0`` 时直接跳过落盘 —— 自检脚本、离线批处理等临时
    进程不该碰任何实例的快照文件。
    """
    if (os.environ.get("ASTOCK_OPS_SNAPSHOT") or "").strip().lower() in ("0", "false", "no"):
        return {"ok": True, "skipped": True, "reason": "ASTOCK_OPS_SNAPSHOT=0"}
    backend = os.path.dirname(os.path.abspath(__file__))
    frontend = os.path.join(os.path.dirname(backend), "frontend")
    target = os.path.join(frontend, snapshot_filename())
    try:
        payload = build_snapshot()
        os.makedirs(frontend, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
        return {"ok": True, "path": target, "file": os.path.basename(target),
                "tag": payload["instance"]["tag"],
                "switches": len(payload["switches"]),
                "secrets": len(payload["secrets"])}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


__all__ = ["build_snapshot", "write_snapshot", "instance_tag", "snapshot_filename",
           "SWITCHES", "SECRETS", "SNAPSHOT_PREFIX", "_FALLBACK_SNAPSHOT"]
