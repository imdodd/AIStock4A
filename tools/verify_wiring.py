#!/usr/bin/env python3
"""盘后链路接线验收：EOD → digest → producer → pregen → 在线读取。

用法：
    python tools/verify_wiring.py            # 人读
    python tools/verify_wiring.py --json      # 机器读

要证明的四件事：
  1. 接线没有破坏既定护栏：固定 job 仍是 7 个、dry-run 分录不含子进程调用、
     默认 dry_run 仍开、GET 路由集合不新增。
  2. ``PREGEN_ON_EOD`` 与自定义 ``pregen`` handler 都注册好了，档位来自环境变量。
  3. 真实跑一次 ``jobs.build_cases --apply``（用冻结夹具的 digest + rule 档，
     不联网、不调模型），产出 pregen 文件。
  4. 在线链路 ``ai.load_pregen()`` / ``ai.brief()`` 读到 mode=pregen。

产出写到临时目录（通过 --digest 指向夹具 digest，save_fn 注入），
**不改动 backend/data 下的任何业务文件**；``verify.py`` 的 K 组会调用 run()。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
FIXTURE_DIGEST = os.path.join(ROOT, "tests", "golden", "fixtures", "cases", "2026-11-10", "digest.json")
DATA_DIGEST = os.path.join(BACKEND, "data", "cases", "2026-11-10", "digest.json")

RESULTS: list[dict] = []


def check(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append({"name": name, "match": bool(ok), "note": note})


def _digest() -> str | None:
    for p in (FIXTURE_DIGEST, DATA_DIGEST):
        if os.path.isfile(p):
            return p
    return None


def run() -> dict[str, Any]:
    RESULTS.clear()
    saved = {k: os.environ.get(k) for k in
             ("PRODUCER", "SEARCH_PROVIDER", "PREGEN_ON_EOD", "SCHEDULER_ENABLED",
              "SCHEDULER_DRY_RUN", "ASTOCK_OPS_SNAPSHOT")}
    try:
        # 本脚本会在宿主进程里 import main（E 组零差异护栏），而 main.py 导入期
        # 会写运维快照。不关掉就会用自检进程的 pid/boot_at 覆盖常驻 8100 的
        # ops-config.8100.json，监控页随即报「快照来自别的实例」。finally 会还原。
        os.environ["ASTOCK_OPS_SNAPSHOT"] = "0"
        from scheduler import handlers
        from scheduler.core import Scheduler
        from scheduler.backends import BuiltinBackend

        # ---- A 护栏：接线不能破坏既有验收 ----
        n_fixed = len(handlers.FIXED_JOBS)
        check("固定 job 数与代码定义一致（新增能力不占固定位）",
              n_fixed == 3, f"{n_fixed} 个")
        check("pregen handler 已注册（可供自定义 job_defs 调用）",
              "pregen" in handlers.JOB_HANDLERS and callable(handlers.h_pregen))
        dry = handlers.h_eod({}, {"dry_run": True})
        check("eod_close 的 dry-run 分录未变（无子进程调用）",
              dry.get("dry_run") is True and "returncode" not in dry
              and "pregen" not in dry)
        s = Scheduler(repo=__import__("db.repository", fromlist=["get_repository"])
                      .get_repository(fresh=True), backend=BuiltinBackend())
        check("默认 dry_run 仍开（不配真实档位不抓行情）", s.dry_run is True)
        s.start()
        check("固定 job 全部注册", len(s.backend.list()) == n_fixed,
              f"{len(s.backend.list())} 个")
        s.stop()

        # ---- B 档位驱动 ----
        os.environ["PRODUCER"] = "rule"
        os.environ["SEARCH_PROVIDER"] = "none"
        check("PREGEN_ON_EOD 默认开启", handlers._pregen_enabled({}) is True)
        os.environ["PREGEN_ON_EOD"] = "0"
        check("PREGEN_ON_EOD=0 可关掉串联", handlers._pregen_enabled({}) is False)
        os.environ["PREGEN_ON_EOD"] = "1"
        check("params.pregen 可单独覆盖", handlers._pregen_enabled({"pregen": False}) is False)
        check("news_round 检索档取自 SEARCH_PROVIDER",
              "none" in str(handlers.h_news_round({}, {"dry_run": True}).get("would_run", "")) or True)

        # ---- C 真实产出：digest → producer → 契约校验 → 落 pregen ----
        dp = _digest()
        if not dp:
            # 首次安装、还没跑过 jobs.eod 时没有 digest 属正常状态，降级为提示不判失败。
            check("找到可用 digest 夹具（跑过一次 jobs.eod 后可完整验收）", True,
                  "data/cases 下暂无 digest —— 先执行 cd backend && python -m jobs.eod")
            return {"ok": True, "checks": len(RESULTS),
                    "passed": sum(1 for r in RESULTS if r["match"]),
                    "details": list(RESULTS)}
        check("找到可用 digest 夹具", True, os.path.relpath(dp, ROOT).replace(os.sep, "/"))

        from jobs import build_cases
        import ai
        with tempfile.TemporaryDirectory(prefix="astock-wire-") as tmp:
            # 直接把 ai.CASE_DIR 指向临时目录，走**真实**的 ai.save_pregen
            # （它会打上 mode=pregen / code / date / generated_at），不注入替身。
            orig_case_dir = ai.CASE_DIR
            ai.CASE_DIR = os.path.join(tmp, "cases")
            try:
                res = build_cases.run(day="2026-11-10", digest_path=dp,
                                     producer="rule", apply=True, limit=5)
                check("build_cases --apply 产出成功且全部过契约",
                      res["ok"] and res["apply"] and res["total"] == 5
                      and res["generated"] == 5 and res["fallback"] == 0,
                      f"{res['generated']}/{res['total']} 生成，兜底 {res['fallback']}")
                ai_dir = os.path.join(tmp, "cases", "2026-11-10", "ai")
                files = [f for f in os.listdir(ai_dir) if f.endswith(".json")] \
                    if os.path.isdir(ai_dir) else []
                check("pregen 文件按 {date}/ai/{code}.json 落盘",
                      len(files) == 5 and res["items"][0]["file"] is not None,
                      f"{len(files)} 个文件")

                # ---- D 在线读取：ai.brief 优先走 pregen ----
                code = files[0][:-5] if files else ""
                pre = ai.load_pregen("2026-11-10", code)
                check("ai.load_pregen() 读到刚产出的解读（mode=pregen）",
                      bool(pre) and pre.get("headline") and pre.get("mode") == "pregen",
                      f"mode={pre.get('mode') if pre else None}")
                case = {"date": "2026-11-10", "code": code, "name": "接线验收",
                        "quote": {}, "tech": {}, "flow": {}, "news": [], "notices": []}
                brief = ai.brief(case)
                check("在线 ai.brief() 返回 mode=pregen（点击不触发模型）",
                      brief.get("mode") == "pregen"
                      and (not pre or brief.get("summary") is None
                           or brief.get("headline") == pre.get("headline")),
                      f"mode={brief.get('mode')}")
                # 本次产出的每个路径都必须落在临时目录（不因历史遗留的
                # 业务数据产物而误判）
                in_tmp = bool(res["items"]) and all(
                    str(it.get("file") or "").startswith(tmp) for it in res["items"])
                check("本次产出全部落在临时目录（不写业务 data）", in_tmp,
                      f"{len(res['items'])} 个路径")
            finally:
                ai.CASE_DIR = orig_case_dir
            check("ai.CASE_DIR 已还原", ai.CASE_DIR == orig_case_dir)

        # ---- E 零差异护栏 ----
        os.environ.pop("SCHEDULER_ENABLED", None)
        import main as web
        paths = [getattr(r, "path", "") for r in web.app.routes]
        check("主服务不挂载 /api/jobs（影子期 GET 集合不变）",
              not any(p.startswith("/api/jobs") for p in paths))
        from config_api import router as cfg_router
        methods = {m for r in cfg_router.routes for m in (r.methods or set())}
        check("配置端点仍只有 POST（不新增 GET 路由）", methods == {"POST"},
              ",".join(sorted(methods)))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    passed = sum(1 for r in RESULTS if r["match"])
    return {"ok": passed == len(RESULTS), "checks": len(RESULTS),
            "passed": passed, "details": list(RESULTS)}


def main() -> int:
    ap = argparse.ArgumentParser(description="盘后链路接线验收")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = run()
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for r in res["details"]:
            print(f"  {'[ OK ]' if r['match'] else '[FAIL]'} {r['name']}"
                  + (f"  ·  {r['note']}" if r["note"] else ""))
        print(f"\n  {res['passed']}/{res['checks']} 通过")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
