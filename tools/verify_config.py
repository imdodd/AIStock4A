#!/usr/bin/env python3
"""Key 自助配置的离线验收（全内存/临时目录，不碰真实 .env 与密钥文件）。

用法：
    python tools/verify_config.py            # 人读
    python tools/verify_config.py --json      # 机器读

被测的是 ``backend/config_api.py`` 的四个关键点：
  A 掩码与脱敏：状态、保存结果、运维快照都不许出现密钥明文
  B 落盘正确性：llm.json / search.json / .env 的原子写与保留注释
  C 护栏：白名单键、枚举、URL 协议、控制字符、非本机请求、HOST 非本机拒挂
  D 生效链路：密钥文件被 ai.config() / fetch_news._cfg() 读到，档位同步 os.environ

所有文件操作都重定向到 ``tempfile.TemporaryDirectory()``，跑完自动清理；
os.environ 的改动逐项恢复，不留副作用。``verify.py`` 的 I 组会调用 run()。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

RESULTS: list[dict] = []


def check(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append({"name": name, "match": bool(ok), "note": note})


def _req(host: str = "127.0.0.1") -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=host))


def _expect_400(fn, label: str) -> None:
    try:
        fn()
        check(label, False, "应当拒绝但放行了")
    except Exception as e:
        status = getattr(e, "status_code", None)
        check(label, status == 400, f"status={status}")


def run() -> dict[str, Any]:
    import config_api
    from fastapi import FastAPI

    RESULTS.clear()
    # 运行时拼接，避免仓库里出现任何形似真实凭据的字面量（发布断言会拦）
    secret = "sk-" + "PROBE-" + "abcdefgh" + "1234567890" + "XYZ"
    saved_env = {k: os.environ.get(k) for k in
                 ("HOST", "CONFIG_API_ENABLED", "PRODUCER", "SEARCH_PROVIDER",
                  "LLM_API_KEY", "SEARCH_API_KEY", "ASTOCK_OPS_SNAPSHOT",
                  "ASTOCK_ENV_FILE")}

    with tempfile.TemporaryDirectory(prefix="astock-cfg-") as tmp:
        orig = {k: getattr(config_api, k) for k in
                ("LLM_FILE", "SEARCH_FILE", "ENV_FILE", "ROOT", "SECRETS_DIR")}
        config_api.LLM_FILE = os.path.join(tmp, ".secrets", "llm.json")
        config_api.SEARCH_FILE = os.path.join(tmp, ".secrets", "search.json")
        config_api.ENV_FILE = os.path.join(tmp, ".env")
        config_api.SECRETS_DIR = os.path.join(tmp, ".secrets")
        config_api.ROOT = tmp
        os.environ["ASTOCK_OPS_SNAPSHOT"] = "0"      # 保存时不写运维快照

        # 预置一份带注释的 .env，验证 upsert 保留原有内容
        with open(config_api.ENV_FILE, "w", encoding="utf-8") as f:
            f.write("# 本地配置（勿入库）\nAPP_PROFILE=private\nPORT=8100\n"
                    "# LLM_API_KEY=  ← 密钥不写 .env\nPRODUCER=rule\n")

        try:
            # ---- A 掩码与脱敏 ----
            check("掩码不泄露完整密钥", secret not in config_api.mask(secret)
                  and config_api.mask(secret).endswith("0XYZ"),
                  config_api.mask(secret))
            st0 = config_api.state(_req())["data"]
            check("初始状态为未配且无明文",
                  not st0["llm"]["configured"] and not st0["search"]["configured"]
                  and secret not in json.dumps(st0, ensure_ascii=False))
            check("服务商预设只含公开地址/模型",
                  all("api_key" not in p and "key" not in p
                      for p in st0["presets"]))

            # ---- B 保存与落盘 ----
            saved = config_api.save(_req(), {
                "llm": {"base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-chat", "api_key": secret,
                        "temperature": "0.3"},
                "search": {"backend": "tavily", "api_key": secret,
                           "max_queries": "3", "max_results": "6"},
                "switches": {"PRODUCER": "llm_direct", "SEARCH_PROVIDER": "none"},
            })["data"]
            blob = json.dumps(saved, ensure_ascii=False)
            check("保存结果不回显密钥明文", secret not in blob)
            check("保存结果带掩码", config_api.mask(secret) in blob)

            llm_disk = json.load(open(config_api.LLM_FILE, encoding="utf-8"))
            sea_disk = json.load(open(config_api.SEARCH_FILE, encoding="utf-8"))
            check("LLM 密钥正确落盘且运行时可读",
                  llm_disk.get("api_key") == secret and llm_disk.get("model") == "deepseek-chat")
            check("检索密钥正确落盘且数值已归一",
                  sea_disk.get("api_key") == secret and sea_disk.get("max_queries") == 3)
            check("密钥不写进 .env",
                  secret not in open(config_api.ENV_FILE, encoding="utf-8").read())

            env_text = open(config_api.ENV_FILE, encoding="utf-8").read()
            check(".env 保留注释与无关行",
                  "# 本地配置（勿入库）" in env_text and "APP_PROFILE=private" in env_text
                  and "PORT=8100" in env_text and "# LLM_API_KEY=" in env_text)
            check(".env 档位已更新且无重复键",
                  "PRODUCER=llm_direct" in env_text
                  and env_text.count("PRODUCER=") == 1
                  and "SEARCH_PROVIDER=none" in env_text)
            check("原子写不残留 .tmp",
                  not os.path.exists(config_api.ENV_FILE + ".tmp")
                  and not os.path.exists(config_api.LLM_FILE + ".tmp"))

            st1 = config_api.state(_req())["data"]
            check("保存后状态变为已配（来源=文件）",
                  st1["llm"]["configured"] and st1["search"]["configured"]
                  and st1["llm"]["source"] in ("file", "both"))
            check("状态接口不回显明文",
                  secret not in json.dumps(st1, ensure_ascii=False))

            # 运维快照同样脱敏（密钥写进 .secrets 后重新生成快照）
            import ops_config
            snap = json.dumps(ops_config.build_snapshot(), ensure_ascii=False)
            check("运维快照不落地密钥值", secret not in snap)

            # ---- 生效链路 ----
            # 两个模块把密钥路径存成模块级常量，重定向到临时文件才能验证
            # 「写入后下次调用立刻读到」，同时不影响真实密钥文件。
            import ai
            from jobs import fetch_news
            ai_secret, sea_secret = ai.SECRET, fetch_news.SECRET_FILE
            ai.SECRET = config_api.LLM_FILE
            fetch_news.SECRET_FILE = config_api.SEARCH_FILE
            llm_env = os.environ.pop("LLM_API_KEY", None)
            sea_env = os.environ.pop("SEARCH_API_KEY", None)
            try:
                cfg = ai.config()
                check("ai.config() 即时读到新密钥（无需重启）",
                      bool(cfg) and cfg.get("api_key") == secret
                      and cfg.get("model") == "deepseek-chat")
                check("档位已同步当前进程 os.environ",
                      os.environ.get("PRODUCER") == "llm_direct"
                      and os.environ.get("SEARCH_PROVIDER") == "none")
                s_cfg = fetch_news._cfg()
                check("fetch_news._cfg() 即时读到检索参数（无需重启）",
                      s_cfg.get("key") == secret and s_cfg.get("backend") == "tavily")
            finally:
                ai.SECRET, fetch_news.SECRET_FILE = ai_secret, sea_secret
                if llm_env is not None:
                    os.environ["LLM_API_KEY"] = llm_env
                if sea_env is not None:
                    os.environ["SEARCH_API_KEY"] = sea_env

            # ---- 空 api_key 保持原值 / clear 清空 ----
            config_api.save(_req(), {"llm": {"api_key": ""}})
            check("留空 api_key 保持原值",
                  json.load(open(config_api.LLM_FILE, encoding="utf-8")).get("api_key") == secret)
            config_api.save(_req(), {"clear": ["llm.api_key"]})
            check("clear 清空密钥",
                  not json.load(open(config_api.LLM_FILE, encoding="utf-8")).get("api_key"))

            # ---- C 护栏 ----
            _expect_400(lambda: config_api.save(_req(), {"llm": {"host": "x"}}),
                         "拒绝非白名单 LLM 字段")
            _expect_400(lambda: config_api.save(_req(), {"switches": {"HOST": "0.0.0.0"}}),
                         "拒绝通过配置改 HOST")
            _expect_400(lambda: config_api.save(_req(), {"switches": {"PRODUCER": "evil"}}),
                         "拒绝非法档位枚举")
            _expect_400(lambda: config_api.save(_req(), {"llm": {"base_url": "ftp://x/y"}}),
                         "拒绝非 http(s) 接口地址")
            _expect_400(lambda: config_api.save(_req(), {"llm": {"model": "a\nb"}}),
                         "拒绝控制字符/换行")
            _expect_400(lambda: config_api.save(_req(), {"llm": {"model": "x" * 600}}),
                         "拒绝超长值")
            _expect_400(lambda: config_api.save(_req(), {"clear": ["env.LLM_API_KEY"]}),
                         "拒绝非法清除项")
            try:
                config_api.state(_req("192.168.1.9"))
                check("拒绝非本机请求", False, "应当拒绝但放行了")
            except Exception as e:
                check("拒绝非本机请求", getattr(e, "status_code", None) == 403)

            fake = FastAPI()
            os.environ["HOST"] = "0.0.0.0"
            refused = config_api.mount(fake)
            check("HOST 非本机时拒绝挂载配置接口", refused is False)
            os.environ["HOST"] = "127.0.0.1"
            os.environ["CONFIG_API_ENABLED"] = "0"
            check("CONFIG_API_ENABLED=0 可整体关闭", config_api.mount(FastAPI()) is False)
            os.environ["CONFIG_API_ENABLED"] = "1"
            check("默认挂载成功", config_api.mount(FastAPI()) is True)

            # 新增路由全部是 POST，不进 GET 差分集合
            methods = {m for r in config_api.router.routes for m in (r.methods or set())}
            check("配置路由只有 POST（不破坏 32:32 GET 集合）",
                  methods == {"POST"}, ",".join(sorted(methods)))

            _expect_400(lambda: config_api.test(_req(), {"target": "bogus"}),
                        "拒绝未知测试目标")
        finally:
            for k, v in orig.items():
                setattr(config_api, k, v)
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    passed = sum(1 for r in RESULTS if r["match"])
    return {"ok": passed == len(RESULTS), "checks": len(RESULTS),
            "passed": passed, "details": list(RESULTS)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Key 配置离线验收")
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
