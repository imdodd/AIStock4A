#!/usr/bin/env python3
"""开源版安装自检（Install Verification）

一条命令回答「这份代码在我机器上到底能不能跑」，分五组检查：

  A 环境      Python 版本、依赖、目录结构
  B 数据      快照文件是否齐全、日期是否新鲜、持仓清单是否合法
  C 接口      起一个临时实例，把只读接口全打一遍，看契约有没有破
  D 数据源    腾讯行情 / 东财板块 / 大模型配置的连通性（仅报告，不阻塞）
  E 存储层    SQLite / Repository 能否建表、UPSERT 是否幂等（用内存库，不落盘）
  F 调度层    交易日历 / cron / job_defs / 启动追补（内存库，不落盘不联网）

用法：
    python tools/verify.py                    # 全量（会临时起服务，默认端口随机）
    python tools/verify.py --offline          # 不联网，只查 A/B/C 里的本地部分
    python tools/verify.py --url http://127.0.0.1:8000   # 检查已在运行的实例，不另起进程
    python tools/verify.py --json             # 输出机器可读结果，便于 CI

退出码：0 全部通过（「无数据」不算失败，只提示去跑 EOD）；1 存在失败项。

设计约定：这份脚本只用标准库，不依赖项目任何模块，可以单独拷走当体检工具用。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
FRONTEND = os.path.join(ROOT, "frontend")
DATA = os.path.join(BACKEND, "data")

OK, WARN, SKIP, FAIL = "OK", "WARN", "SKIP", "FAIL"
MARK = {OK: "[ OK ]", WARN: "[WARN]", SKIP: "[ -- ]", FAIL: "[FAIL]"}

results: list[dict] = []
_group = ""
DATA_WRITABLE = True          # check_env 会改写；不可写时接口 500 要归因为环境问题


def add(level: str, name: str, detail: str = "") -> None:
    results.append({"group": _group, "level": level, "name": name, "detail": detail})
    if not globals().get("args") or args.json:
        return
    line = f"  {MARK[level]} {name}"
    if detail:
        line += f"  ·  {detail}"
    print(line)


def group(title: str) -> None:
    global _group
    _group = title
    if not globals().get("args") or args.json:
        return
    print(f"\n{title}")


# ---------------------------------------------------------------- A 环境
def check_env() -> None:
    group("A · 环境与依赖")

    v = sys.version_info
    if v >= (3, 11):
        add(OK, "Python 版本", f"{v.major}.{v.minor}.{v.micro}")
    else:
        add(FAIL, "Python 版本", f"{v.major}.{v.minor} 低于要求，需要 3.11+")

    for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pydantic", "pydantic")):
        try:
            m = __import__(mod)
            add(OK, f"依赖 {pkg}", getattr(m, "__version__", "已安装"))
        except Exception as e:
            add(FAIL, f"依赖 {pkg}", f"{type(e).__name__}：先 pip install -r requirements.txt")

    for path, label in (
        (os.path.join(BACKEND, "main.py"), "后端入口 backend/main.py"),
        (os.path.join(FRONTEND, "index.html"), "前端单页 frontend/index.html"),
        (os.path.join(FRONTEND, "echarts.min.js"), "图表库 frontend/echarts.min.js"),
        (os.path.join(ROOT, "requirements.txt"), "依赖清单 requirements.txt"),
        (os.path.join(ROOT, "examples", "holdings.example.json"), "样例持仓 examples/"),
    ):
        if os.path.exists(path):
            size = os.path.getsize(path)
            add(OK, label, f"{size:,} 字节")
        else:
            add(FAIL, label, "文件缺失，仓库可能不完整")

    # 数据目录可写：首次运行要靠它落盘，只读挂载会导致接口 500
    global DATA_WRITABLE
    probe = os.path.join(DATA, ".verify_probe")
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        add(OK, "data 目录可写", DATA)
    except Exception as e:
        DATA_WRITABLE = False
        add(FAIL, "data 目录可写",
            f"{type(e).__name__}：{DATA} 不可写，持仓/自选类接口会全部 500")

    # 后端能否导入（会连带暴露 config / engines 里的语法与循环依赖问题）
    try:
        code = "import main; print(len(main.app.routes))"
        p = subprocess.run([sys.executable, "-c", code], cwd=BACKEND,
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, ASTOCK_OPS_SNAPSHOT="0"))
        if p.returncode == 0:
            n = (p.stdout or "").strip().splitlines()[-1]
            add(OK, "后端模块可导入", f"注册 {n} 条路由")
        else:
            tail = (p.stderr or "").strip().splitlines()
            add(FAIL, "后端模块可导入", tail[-1] if tail else "导入失败")
    except Exception as e:
        add(FAIL, "后端模块可导入", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- B 数据
def _json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def check_data() -> None:
    group("B · 数据与状态")

    snap = os.path.join(DATA, "snapshot")
    market = _json(os.path.join(snap, "market.json"))
    if market is None:
        add(WARN, "快照 market.json", "尚未生成 —— 需要先跑 python -m jobs.eod")
    else:
        d = str(market.get("date") or "")
        add(OK, "快照 market.json", f"date={d} 覆盖 {len(market.get('indices') or [])} 个指数")
        try:
            gen = datetime.strptime(d[:10], "%Y-%m-%d").date()
            today = date.today()
            back = today
            while back.weekday() >= 5:      # 退到最近一个工作日
                back -= timedelta(days=1)
            gap = (back - gen).days
            if gap <= 1:
                add(OK, "快照新鲜度", f"{d} 是最新交易日")
            elif gap <= 4:
                add(WARN, "快照新鲜度", f"落后 {gap} 天，可能碰上假期，或 EOD 没跑完")
            else:
                add(WARN, "快照新鲜度", f"落后 {gap} 天 —— 请重新跑 python -m jobs.eod")
        except Exception:
            add(WARN, "快照新鲜度", f"date 字段无法解析：{d!r}")

    for fn, label, minimum in (
        ("sectors.json", "行业阶段 sectors.json", 100),
        ("signals.json", "信号 signals.json", 1),
        ("focus_pool.json", "关注池 focus_pool.json", 1),
        ("transitions.json", "阶段迁移 transitions.json", 1),
    ):
        obj = _json(os.path.join(snap, fn))
        if obj is None:
            add(WARN, label, "未生成 —— EOD 没跑到这一步")
            continue
        n = len(obj) if isinstance(obj, (list, dict)) else 0
        add(OK if n >= minimum else WARN, label, f"{n} 条" + ("" if n >= minimum else "，偏少"))

    mem_dir = os.path.join(snap, "members")
    if os.path.isdir(mem_dir):
        n = len([f for f in os.listdir(mem_dir) if f.endswith(".json")])
        add(OK if n >= 50 else WARN, "行业成分股 members/", f"{n} 个行业文件")
    else:
        add(WARN, "行业成分股 members/", "未生成 —— 行业阶段分布会缺数据")

    # 持仓 / 自选清单：这两份是用户可写的唯一事实来源
    for fn, label, keys in (
        ("holdings.json", "持仓清单 portfolio/holdings.json", ("code",)),
        ("watchlist.json", "自选清单 portfolio/watchlist.json", ("code",)),
    ):
        p = os.path.join(DATA, "portfolio", fn)
        rows = _json(p)
        if rows is None:
            add(WARN, label, "未创建 —— 打开前端增删一只即会自动生成")
            continue
        if not isinstance(rows, list):
            add(FAIL, label, "内容不是数组，格式坏了")
            continue
        bad = [r for r in rows if not isinstance(r, dict) or not r.get("code")]
        if bad:
            add(WARN, label, f"{len(rows)} 条，其中 {len(bad)} 条缺 code")
        else:
            add(OK, label, f"{len(rows)} 条")


# ---------------------------------------------------------------- C 接口
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(base: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _get(base: str, path: str, timeout: float = 25.0):
    """返回 (status, payload, err)"""
    url = base + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8")), None
            except Exception:
                return r.status, None, "响应不是 JSON"
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8")), None
        except Exception:
            return e.code, None, raw[:120].decode("utf-8", "replace")
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"


CJK = re.compile(r"[\u4e00-\u9fff]")


def _judge(path: str, status: int, payload, err: str, writable: bool = None) -> tuple[str, str]:
    """把一次响应归类成 OK / WARN / SKIP / FAIL。

    这里的分寸很关键：新装未跑 EOD 时接口返回「快照未生成」是设计好的行为，
    不算失败；只有 5xx 且没有任何说明、或连接不上，才是真出问题。
    """
    if writable is None:
        writable = DATA_WRITABLE
    if status == 0:
        return FAIL, err or "连接失败"
    if status == 200:
        if isinstance(payload, dict) and "code" in payload:
            data = payload.get("data")
            n = len(data) if isinstance(data, (list, dict)) else data
            return OK, f"200 · {type(data).__name__} {n if n is not None else ''}".strip()
        return WARN, "200 但响应缺少 code 字段，契约可能变了"
    detail = ""
    if isinstance(payload, dict):
        detail = str(payload.get("detail") or payload.get("message") or "")
    if status >= 500 and not writable:
        return SKIP, f"{status} · data 目录不可写导致，非接口问题"
    if status in (400, 404, 409, 422, 503) or (500 <= status < 600 and CJK.search(detail)):
        return SKIP, f"{status} · {detail[:60]}" if detail else f"{status} 业务性拒绝"
    return FAIL, f"{status} · {detail[:60] or err or '无错误说明'}"


# 本地只读接口：不依赖外网，必须全部可用
LOCAL_ROUTES = [
    "/api/health",
    "/api/market",
    "/api/sectors",
    "/api/transitions",
    "/api/signals",
    "/api/focus/pool",
    "/api/watchlist",
    "/api/holdings",
    "/api/holdings/pnl",
    "/api/holdings/pnl/history",
    "/api/holdings/pnl/stats",
    "/api/tracking",
    "/api/alerts",
    "/api/members/index",
    "/api/news/index",
    "/api/screener",
    "/api/search?" + urllib.parse.urlencode({"q": "茅台"}),
    "/api/profile/sh600519",
]

# 依赖外部行情源的接口：失败只提示，不算仓库坏
NET_ROUTES = [
    "/api/research/status",
    "/api/market/flow",
    "/api/live/sh600519",
    "/api/chart/sh600519",
]


def check_api(base: str) -> None:
    group(f"C · 接口契约（{base}）")

    status, payload, err = _get(base, "/api/health")
    if status != 200:
        add(FAIL, "服务可达", err or f"HTTP {status}")
        return
    add(OK, "服务可达", "/api/health 200")

    for path in LOCAL_ROUTES[1:]:
        lvl, note = _judge(path, *_get(base, path))
        add(lvl, path, note)

    group("C+ · 依赖外网的接口（失败不代表安装有问题）")
    for path in NET_ROUTES:
        lvl, note = _judge(path, *_get(base, path), writable=True)
        if lvl == FAIL:
            lvl = WARN
            note += " —— 上游行情源当前不可用"
        add(lvl, path, note)

    # 前端静态资源
    group("C++ · 前端静态资源")
    for path in ("/", "/static/echarts.min.js"):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as r:
                body = r.read()
                add(OK if r.status == 200 else WARN, path, f"{len(body):,} 字节")
        except Exception as e:
            add(FAIL, path, f"{type(e).__name__} —— 图表库取不到会让页面所有图表空白")


# ---------------------------------------------------------------- D 数据源
def check_sources() -> None:
    group("D · 外部数据源与配置")

    def probe(name: str, url: str, expect: str = "") -> None:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36",
            "Referer": "https://gu.qq.com/",
        })
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                body = r.read(4096).decode("utf-8", "replace")
                if expect and expect not in body:
                    add(WARN, name, f"200 但内容不含 {expect!r}")
                else:
                    add(OK, name, f"200 · {len(body)} 字节")
        except Exception as e:
            add(WARN, name, f"{type(e).__name__} —— 本机网络或上游限流")

    probe("腾讯行情（主数据源）",
          "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,5,qfq", "qfqday")
    probe("腾讯行业板块",
          "https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type=hy2&sort_type=PriceRatio&direct=down&offset=0&count=2", "rank_list")
    probe("东财板块（行业成分股）",
          "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=2&fid=f3&fs=m:90+t:2&fields=f12,f14")

    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if key:
        add(OK, "大模型配置", f"已配置 {os.environ.get('LLM_MODEL') or '默认模型'}")
    else:
        add(SKIP, "大模型配置", "未配置 LLM_API_KEY —— AI 解读模块会提示未接通，其余功能不受影响")

    add(SKIP, "手动确认项", "浏览器打开首页，确认「大盘」页有阶段分布、无红色报错")


# ---------------------------------------------------------------- E 存储层
def check_storage() -> None:
    """DB 层冒烟。用独立子进程 + :memory: 库，不依赖项目模块、不产生任何文件。"""
    group("E · 存储层（SQLite / Repository）")

    files = ("schema.sql", "tables.py", "conn.py", "repository.py", "bridge.py")
    missing = [f for f in files
               if not os.path.isfile(os.path.join(BACKEND, "db", f))]
    if missing:
        add(FAIL, "存储层文件齐全", f"缺少 {', '.join(missing)}")
        return
    add(OK, "存储层文件齐全", f"{len(files)} 个模块")

    probe = (
        "import json,sys\n"
        f"sys.path.insert(0, r'{BACKEND}')\n"
        "from dataclasses import replace\n"
        "from db import SqliteRepository, load_config\n"
        "cfg = replace(load_config(), path=':memory:')\n"
        "r = SqliteRepository(cfg)\n"
        "tables = r.stats()['tables']\n"
        "row = {'code':'sh900001','name':'probe','cost':None,'quantity':None}\n"
        "r.put_row('holdings', {'code':'sh900001'}, row, '2026-01-01T00:00:00')\n"
        "r.put_row('holdings', {'code':'sh900001'}, row, '2026-01-01T00:00:00')\n"
        "print(json.dumps({'ok': r.count('holdings') == 1 and len(tables) == 8,\n"
        "                  'tables': tables,\n"
        "                  'version': r.meta_get('schema_version')}))\n"
    )
    try:
        cp = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, timeout=30,
                            env=dict(os.environ, ASTOCK_DB_PATH=":memory:",
                                     PYTHONPATH=BACKEND))
        out = json.loads(cp.stdout.strip().splitlines()[-1])
        add(OK if out["ok"] else FAIL, "内存库建表与幂等 UPSERT",
            f"8 张资产表 · schema {out['version']} · 重复写同一行仍为 1 条")
    except Exception as e:
        add(WARN, "内存库冒烟", f"{type(e).__name__}: {e} —— 存储层可选，主链路仍走文件")


def check_scheduler() -> None:
    group("F · 调度层（日历 / job_defs / 追补）")

    files = ("__init__.py", "calendar.py", "cron.py", "state.py",
             "handlers.py", "backends.py", "core.py", "api.py")
    missing = [f for f in files
               if not os.path.isfile(os.path.join(BACKEND, "scheduler", f))]
    if missing:
        add(FAIL, "调度层文件齐全", f"缺少 {', '.join(missing)}")
        return
    add(OK, "调度层文件齐全", f"{len(files)} 个模块")

    probe = (
        "import json,sys,os\n"
        f"sys.path.insert(0, r'{BACKEND}')\n"
        "os.environ['ASTOCK_DB_PATH'] = ':memory:'\n"
        "os.environ['SCHEDULER_CATCHUP'] = '0'\n"
        "from db.repository import get_repository\n"
        "from scheduler import calendar as cal, cron, handlers\n"
        "from scheduler.backends import BuiltinBackend\n"
        "from scheduler.core import Scheduler\n"
        "r = get_repository(fresh=True)\n"
        "tables = [x[0] for x in r.connection.execute(\n"
        "    \"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]\n"
        "s = Scheduler(repo=r, backend=BuiltinBackend())\n"
        "s.start()\n"
        "catch = s.run_catchup()\n"
        "res = {'has_defs': 'job_defs' in tables, 'has_runs': 'job_runs' in tables,\n"
        "       'fixed': len(handlers.FIXED_JOBS), 'registered': len(s.backend.list()),\n"
        "       'dry_run': s.dry_run, 'catchup': len(catch),\n"
        "       'cron_ok': bool(cron.next_fire('0 16 * * 1-5')),\n"
        "       'weekend_ok': cal.is_trading_day('2026-11-15') is False}\n"
        "s.stop()\n"
        "print(json.dumps(res))\n"
    )
    try:
        if BACKEND not in sys.path:
            sys.path.insert(0, BACKEND)
        from scheduler.handlers import FIXED_JOBS   # 固定 job 以代码定义为准，不写死
        n_fixed = len(FIXED_JOBS)
        cp = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                            text=True, timeout=40,
                            env=dict(os.environ, ASTOCK_DB_PATH=":memory:",
                                     SCHEDULER_CATCHUP="0", PYTHONPATH=BACKEND))
        out = json.loads(cp.stdout.strip().splitlines()[-1])
        ok = (out["has_defs"] and out["has_runs"] and out["fixed"] == n_fixed
              and out["registered"] == n_fixed and out["dry_run"] is True
              and 0 < out["catchup"] <= n_fixed
              and out["cron_ok"] and out["weekend_ok"])
        add(OK if ok else FAIL, "调度冒烟（内存库，不落盘不联网）",
            f"{n_fixed} 固定 job · 追补 {out['catchup']} 条 · dry_run={out['dry_run']} "
            f"· job_defs/job_runs 均就绪")
    except Exception as e:
        add(WARN, "调度层冒烟", f"{type(e).__name__}: {e} —— 调度可选，默认不启用")


def check_read_switch() -> None:
    """G 组：读路径开关零侵入 + db 档逐字段等价 + 库内探针（内存库，不落盘）。"""
    group("G · 读路径切换（STORAGE_READ）")

    f = os.path.join(BACKEND, "storage_runtime.py")
    r = os.path.join(ROOT, "tools", "verify_read_switch.py")
    if not (os.path.isfile(f) and os.path.isfile(r)):
        add(FAIL, "读路径模块齐全", f"缺少 {f} 或 {r}")
        return
    add(OK, "读路径模块齐全", "storage_runtime.py + verify_read_switch.py")
    try:
        if os.path.dirname(r) not in sys.path:
            sys.path.insert(0, os.path.dirname(r))
        import contextlib
        import verify_read_switch as vrs
        # install() 的启动日志走 stderr，避免污染 --json 的 stdout
        with contextlib.redirect_stdout(sys.stderr):
            res = vrs.run(os.path.join(BACKEND, "data"))
        bad = [c["name"] for c in res["details"] if not c["match"]]
        # 首次安装、还没铺样例数据时，资产表自举为 0 属正常状态，降级为提示。
        no_sample = not os.path.isfile(os.path.join(
            BACKEND, "data", "portfolio", "holdings.json"))
        if res["ok"]:
            add(OK, "读路径验收（files/db/隐藏文件/库内探针）",
                f"{res['passed']}/{res['checks']} 通过")
        elif no_sample and bad:
            add(WARN, "读路径验收",
                f"{res['passed']}/{res['checks']} 通过，"
                f"data/portfolio 下暂无样例数据（首次安装正常）；"
                f"跑 start.bat 或参考 examples/ 铺入样例后重跑")
        else:
            add(FAIL, "读路径验收（files/db/隐藏文件/库内探针）",
                f"{res['passed']}/{res['checks']} 通过"
                + (f"，失败：{', '.join(bad[:3])}" if bad else ""))
    except Exception as e:
        add(WARN, "读路径验收", f"{type(e).__name__}: {e} —— 默认 files 档不受影响")


def check_producers() -> None:
    """H 组：三份契约 + 三档 producer + 防幻觉 + 历史格式迁移（全离线）。"""
    group("H · 产出契约与 producers（阶段 5）")

    files = ("contracts/models.py", "contracts/validate.py", "contracts/__init__.py",
             "producers/base.py", "producers/rule.py", "producers/llm_direct.py",
             "producers/agent_hook.py", "producers/manual.py", "producers/discover.py",
             "jobs/build_cases.py")
    docs = ("digest.schema.json", "ai_brief.schema.json", "news_patch.schema.json")
    missing = [f for f in files if not os.path.isfile(os.path.join(BACKEND, f))]
    missing += [f for f in docs
                if not os.path.isfile(os.path.join(ROOT, "docs", "contracts", f))]
    if missing:
        add(FAIL, "契约层文件齐全", f"缺少 {', '.join(missing)}")
        return
    add(OK, "契约层文件齐全", f"{len(files)} 个模块 + {len(docs)} 份 schema")
    try:
        if os.path.dirname(__file__) not in sys.path:
            sys.path.insert(0, os.path.dirname(__file__))
        import verify_producers as vp
        res = vp.run()
        bad = [c["name"] for c in res["details"] if not c["match"]]
        add(OK if res["ok"] else WARN, "契约/防幻觉/producer 验收",
            f"{res['passed']}/{res['checks']} 通过"
            + (f"，未过：{', '.join(bad[:3])}" if bad else ""))
    except Exception as e:
        add(WARN, "契约验收", f"{type(e).__name__}: {e} —— 默认主链路不依赖 producers")
    with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as f:
        src = f.read()
    add(OK if ("import producers" not in src and "import contracts" not in src)
        else FAIL, "主链路零侵入（在线路由不引用 producers）",
        "盘后批处理才调 producer，在线只读 pregen")


def check_ops() -> None:
    """I 组：.env 加载器与运维页（全离线，不写密钥、不联网）。"""
    group("I · 配置加载与运维监控")
    try:
        import contextlib
        if BACKEND not in sys.path:
            sys.path.insert(0, BACKEND)
        import env_loader
        p = env_loader.parse(
            '# c\nexport APP_PROFILE=private\n'
            'LLM_API_KEY="sk-a#b"  # inline\nSEARCH_API_KEY=\'q\'\nBAD LINE\n')
        ok = (p.get("APP_PROFILE") == "private" and p.get("LLM_API_KEY") == "sk-a#b"
              and p.get("SEARCH_API_KEY") == "q" and "BAD" not in str(p))
        add(OK if ok else FAIL, ".env 解析（注释/export/引号/带 # 的 Key）",
            f"{len(p)} 个键")
        r = env_loader.load_env()          # 只读状态，不改已有变量
        add(OK if isinstance(r.get("applied"), list) else FAIL, ".env 加载接口",
            f"found={r.get('found')} applied={len(r.get('applied') or [])} "
            f"skipped={len(r.get('skipped') or [])}")
        import ops_config
        os.environ["__OPS_SECRET_PROBE__"] = "MUST_NOT_LEAK_9527"
        blob = json.dumps(ops_config.build_snapshot(), ensure_ascii=False)
        safe = "MUST_NOT_LEAK_9527" not in blob
        add(OK if safe else FAIL, "配置快照脱敏（密钥值不落地）",
            f"{len(ops_config.SWITCHES)} 档位 / {len(ops_config.SECRETS)} 密钥项")
        os.environ.pop("__OPS_SECRET_PROBE__", None)
        # 按实例隔离：不同端口的实例必须写不同文件，且 --port 优先于 .env 的 PORT
        old_port, old_tag = os.environ.get("PORT"), os.environ.get("ASTOCK_OPS_TAG")
        os.environ["PORT"] = "8100"
        os.environ.pop("ASTOCK_OPS_TAG", None)
        f8100 = ops_config.snapshot_filename()
        os.environ["ASTOCK_OPS_TAG"] = "8102"
        f8102 = ops_config.snapshot_filename()
        os.environ.pop("ASTOCK_OPS_TAG", None)
        if old_port is None:
            os.environ.pop("PORT", None)
        else:
            os.environ["PORT"] = old_port
        if old_tag is not None:
            os.environ["ASTOCK_OPS_TAG"] = old_tag
        isolated = (f8100 != f8102 and f8100 != "ops-config.json"
                    and ops_config.instance_tag() and "/" not in f8102)
        add(OK if isolated else FAIL, "配置快照按实例隔离（不互相覆盖）",
            f"{f8100} / {f8102}")
        # 离线自检脚本会在宿主进程里 import main，导入期会写快照；必须在
        # import 之前关掉，否则自检进程会用自己 pid/boot_at 覆盖常驻实例快照。
        no_pollute = True
        for name in ("verify_wiring.py", "verify_scheduler.py"):
            with open(os.path.join(ROOT, "tools", name), encoding="utf-8") as f:
                src = f.read()
            idx = src.find("import main as web")
            no_pollute = no_pollute and idx > 0 and "ASTOCK_OPS_SNAPSHOT" in src[:idx]
        add(OK if no_pollute else FAIL, "离线自检不污染常驻实例快照",
            "verify_wiring / verify_scheduler 在 import main 前关闭快照写入")
        # boot_at 必须是进程级常量：启动时写一次、配置页保存再写一次都不能变，
        # 否则与 /api/health 差一两秒，前端来源校验会误报「快照来自别的实例」。
        same_stamp = (ops_config._boot_stamp() == ops_config._boot_stamp()
                      and ops_config.BOOT_STAMP == ops_config._boot_stamp())
        with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as f:
            main_src = f.read()
        add(OK if same_stamp and "ops_config.BOOT_STAMP" in main_src else FAIL,
            "快照 boot_at 与 /api/health 同源（不会误报实例不符）",
            "进程级 BOOT_STAMP，保存配置重写快照也不变")
        page = os.path.join(ROOT, "frontend", "ops.html")
        exists = os.path.isfile(page)
        with open(page, encoding="utf-8") as f:
            html = f.read()
        no_api = ("/api/health" in html and "/api/jobs/status" in html
                  and "/static/ops-config.json" in html and "CFG_PORT" in html
                  and "renderSnapshotCheck" in html)
        add(OK if exists and no_api else FAIL, "运维页为静态文件（不新增 API 路由）",
            f"{len(html)} 字节，按端口取专属快照 + 来源/过期校验")
        with open(os.path.join(BACKEND, "main.py"), encoding="utf-8") as f:
            src = f.read()
        add(OK if "env_loader.load_env()" in src else FAIL, "服务入口最先加载 .env")
        # 前端不得再硬编码生产 8000 兜底地址
        with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
            fe = f.read()
        no_fallback = "http://127.0.0.1:8000" not in fe and "localhost:8000" not in fe
        add(OK if no_fallback else FAIL, "前端无硬编码 8000 生产端口兜底",
            "静态预览需显式 ?api= 指定后端")
        # J 组：Key 自助配置（临时目录隔离，绝不碰真实 .env / 密钥文件）
        group("J · Key 自助配置（离线验收）")
        cfg_api = os.path.join(BACKEND, "config_api.py")
        cfg_page = os.path.join(FRONTEND, "config.html")
        if not (os.path.isfile(cfg_api) and os.path.isfile(cfg_page)):
            add(FAIL, "配置模块与页面齐全", "缺少 config_api.py 或 config.html")
        else:
            add(OK, "配置模块与页面齐全", "后端 config_api.py + 静态页 config.html")
            try:
                if os.path.dirname(__file__) not in sys.path:
                    sys.path.insert(0, os.path.dirname(__file__))
                import verify_config as vc
                with contextlib.redirect_stdout(sys.stderr):   # mount() 的提示不污染 --json
                    res = vc.run()
                bad = [c["name"] for c in res["details"] if not c["match"]]
                add(OK if res["ok"] else FAIL, "配置验收（脱敏/落盘/护栏/即时生效）",
                    f"{res['passed']}/{res['checks']} 通过"
                    + (f"，未过：{', '.join(bad[:3])}" if bad else ""))
            except Exception as e:
                add(WARN, "配置验收", f"{type(e).__name__}: {e}")
            with open(cfg_api, encoding="utf-8") as f:
                src_api = f.read()
            add(OK if '@router.post("/state")' in src_api and '@router.get' not in src_api
                else FAIL, "配置端点只有 POST（不破坏 GET 32:32 差分）",
                "state / save / test 均为 POST")
    except Exception as e:
        add(WARN, "配置加载与运维页", f"{type(e).__name__}: {e}")

    # K 组：盘后链路接线（离线，产出写临时目录）
    group("K · 盘后链路接线（EOD → digest → producer → pregen）")
    wiring = os.path.join(ROOT, "tools", "verify_wiring.py")
    if not os.path.isfile(wiring):
        add(FAIL, "接线验收脚本齐全", "缺少 tools/verify_wiring.py")
    else:
        try:
            import contextlib
            if os.path.dirname(__file__) not in sys.path:
                sys.path.insert(0, os.path.dirname(__file__))
            # 自检是默认档位断言：宿主进程此前的 import main / load_env 可能已把
            # .env 的调度与数据目录档位带进进程环境，先清掉，保证 wiring 在
            # 干净默认值（内存库、默认 dry-run）下验收。
            for _k in ("SCHEDULER_ENABLED", "SCHEDULER_DRY_RUN",
                       "SCHEDULER_BACKEND", "SCHEDULER_CATCHUP",
                       "ASTOCK_DATA_DIR", "ASTOCK_DB_PATH",
                       "STORAGE_READ", "STORAGE_STRICT", "STORAGE_BOOTSTRAP"):
                os.environ.pop(_k, None)
            import verify_wiring as vw
            with contextlib.redirect_stdout(sys.stderr):    # 屏蔽模块导入期的启动日志
                res = vw.run()
            bad = [c["name"] for c in res["details"] if not c["match"]]
            add(OK if res["ok"] else FAIL, "接线验收（护栏/真实产出/在线读取）",
                f"{res['passed']}/{res['checks']} 通过"
                + (f"，未过：{', '.join(bad[:3])}" if bad else ""))
        except Exception as e:
            add(WARN, "接线验收", f"{type(e).__name__}: {e}")
        with open(os.path.join(BACKEND, "scheduler", "handlers.py"), encoding="utf-8") as f:
            hw = f.read()
        add(OK if "pregen_chain" in hw and '"pregen": h_pregen' in hw
            else FAIL, "pregen 已接进 eod_close 且不占固定 job 位",
            "可作为自定义 job_defs 单独挂载")

    # L 组：数据目录分家（不改任何业务数据，只做纯静态与 import 级验证）
    group("L · 数据目录分家（ASTOCK_DATA_DIR）")
    dp = os.path.join(BACKEND, "data_paths.py")
    if not os.path.isfile(dp):
        add(FAIL, "统一数据目录模块存在", "缺少 backend/data_paths.py")
    else:
        try:
            import importlib
            if BACKEND not in sys.path:
                sys.path.insert(0, BACKEND)
            import data_paths as dp_mod
            probe = os.path.join(ROOT, ".tmp", "data-dir-probe")
            old = os.environ.get("ASTOCK_DATA_DIR")
            try:
                os.environ.pop("ASTOCK_DATA_DIR", None)
                importlib.reload(dp_mod)
                default_ok = (os.path.normcase(dp_mod.DATA_DIR)
                              == os.path.normcase(os.path.join(BACKEND, "data")))
                os.environ["ASTOCK_DATA_DIR"] = probe
                importlib.reload(dp_mod)
                switch_ok = (os.path.normcase(dp_mod.DATA_DIR)
                             == os.path.normcase(probe))
            finally:
                if old is None:
                    os.environ.pop("ASTOCK_DATA_DIR", None)
                else:
                    os.environ["ASTOCK_DATA_DIR"] = old
                importlib.reload(dp_mod)
            add(OK if default_ok and switch_ok else FAIL, "数据目录默认值与切换生效",
                f"默认={default_ok}，切换={switch_ok}")
        except Exception as e:
            add(WARN, "数据目录解析", f"{type(e).__name__}: {e}")

        patterns = ('os.path.join(HERE, "data"', 'os.path.join(BACKEND, "data"',
                    'os.path.join(ROOT, "data"', 'os.path.join(HERE, "..", "data"',
                    'HERE / "data"', 'BASE / "data"')
        stale = []
        for dirpath, dirnames, filenames in os.walk(BACKEND):
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", "data", ".secrets")]
            for fn in filenames:
                if not fn.endswith(".py") or fn == "data_paths.py":
                    continue
                try:
                    src = open(os.path.join(dirpath, fn), encoding="utf-8").read()
                except OSError:
                    continue
                if any(p in src for p in patterns):
                    stale.append(os.path.relpath(os.path.join(dirpath, fn), BACKEND))
        add(OK if not stale else FAIL, "无写死数据路径残留",
            f"{len(stale)} 个文件" + (f"：{', '.join(stale[:3])}" if stale else "，全部经 data_paths 解析"))

        watched = ("jobs/fetch_daily.py", "jobs/eod.py",
                   "scheduler/handlers.py", "screener.py")
        missing = []
        for rel in watched:
            p = os.path.join(BACKEND, *rel.split("/"))
            if not os.path.isfile(p):
                missing.append(rel)
                continue
            if "data_paths" not in open(p, encoding="utf-8").read():
                missing.append(rel)
        add(OK if not missing else FAIL, "关键写模块已接入统一数据目录",
            f"{len(watched) - len(missing)}/{len(watched)}"
            + (f"，缺：{', '.join(missing)}" if missing else ""))


# ---------------------------------------------------------------- 主流程
def main() -> int:
    global args
    p = argparse.ArgumentParser(description="开源版安装自检")
    p.add_argument("--url", help="检查已在运行的实例，如 http://127.0.0.1:8100")
    p.add_argument("--offline", action="store_true", help="跳过联网检查")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    args = p.parse_args()

    if not args.json and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    check_env()
    check_data()

    proc = None
    base = args.url
    if base:
        check_api(base.rstrip("/"))
    else:
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        if not args.json:
            print(f"启动临时实例 {base}（检查完自动关闭）")
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   ASTOCK_OPS_SNAPSHOT="0")   # 临时实例不写快照，避免污染常驻实例的运维页
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                 "--port", str(port), "--log-level", "error"],
                cwd=BACKEND, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if _wait_ready(base):
                check_api(base)
            else:
                group("C · 接口契约")
                add(FAIL, "临时实例启动", "45 秒内未就绪，看 backend 目录下直接跑 uvicorn 的报错")
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True) if os.name == "nt" else proc.kill()

    if not args.offline:
        check_sources()
    else:
        group("D · 外部数据源与配置")
        add(SKIP, "联网检查", "已用 --offline 跳过")

    check_storage()
    check_scheduler()
    check_read_switch()
    check_producers()
    check_ops()

    # ---------------- 汇总
    counts = {k: sum(1 for r in results if r["level"] == k) for k in (OK, WARN, SKIP, FAIL)}

    if args.json:
        print(json.dumps({"counts": counts, "results": results}, ensure_ascii=False, indent=2))
        return 0 if counts[FAIL] == 0 else 1

    print("\n" + "─" * 66)
    print(f"  通过 {counts[OK]} · 提示 {counts[WARN]} · 跳过 {counts[SKIP]} · 失败 {counts[FAIL]}")

    if counts[FAIL]:
        print("\n  有失败项。多数情况是 pip 依赖没装全，或 data 目录不可写。")
        print("  逐项看上面的 [FAIL] 提示即可。")
    elif not os.path.exists(os.path.join(DATA, "snapshot", "market.json")):
        print("\n  环境没问题，但还没有行情数据。下一步：")
        print("    cd backend && python -m jobs.eod      # 首次全量抓取，5~8 分钟")
    else:
        print("\n  一切正常。启动服务：cd backend && python -m uvicorn main:app --port 8000")

    return 0 if counts[FAIL] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
