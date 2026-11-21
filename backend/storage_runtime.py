"""读路径切换：STORAGE_READ=files | db。

定位
----
阶段 3 已经证明「同一份数据，文件版函数与 DB 版 bridge 逐字段相等」（26/26）。
本模块把那次对账的结论真正接进主链路，做法是**在运行期替换绿区模块的公开函数**，
而不是去改 6 个绿区存储模块的源码 —— 这样它们的 sha256 仍与生产逐字节一致，
默认档（files）也完全零侵入。

  STORAGE_READ=files（默认）  install() 什么也不做，所有函数保持原样；
  STORAGE_READ=db             进程启动时把文件态数据自举进 SQLite，读函数改走
                              db.bridge，写函数保持原实现（文件仍是完整副本），
                              每次写完后把该文件对应的表整表同步回库。

因此「切读路径」不是让文件失效：文件仍是权威副本与回滚点，库是读视图。
任何一步失败都打印明确告警并回退到 files（STORAGE_STRICT=1 时直接抛错），
绝不静默产出「半库半文件」的结果。

读函数覆盖范围（8 张资产表全切）：
  holdings / watchlist / realized / pnl_daily / tracking_plans /
  tracking_periods / news_daily / stops
其中 stops.load 内含「交易日变更 / 持仓变更 → 懒重算」的业务规则，
替换时保留这段规则，只把它的初始数据源换成库里的 payload。

环境变量：
  STORAGE_READ        files | db
  STORAGE_BOOTSTRAP   auto（默认）| off —— db 档且库为空时从 data/ 自举
  STORAGE_STRICT      1 = 初始化失败直接抛错，不回退
  ASTOCK_DB_PATH      SQLite 路径；:memory: 全内存，不落盘
"""
from __future__ import annotations

import functools
import importlib.util
import os
import sys
import threading
from dataclasses import replace
from types import ModuleType
from typing import Callable, Iterable

from data_paths import DATA_DIR
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DEFAULT_SRC = DATA_DIR
IMPORTER_PATH = os.path.join(ROOT, "tools", "importer.py")

from db import ASSET_TABLES, get_repository, load_config  # noqa: E402
from db import bridge as b  # noqa: E402

_lock = threading.RLock()
_originals: dict[str, dict[str, Callable]] = {}
_importer: ModuleType | None = None
_state: dict = {"installed": False, "mode": "files", "patched": [], "writes": [],
                "repo": None, "src": None, "bootstrap": None,
                "fallback_reason": None}


# ---------------------------------------------------------------- 工具
def _load_importer() -> ModuleType:
    """按需加载 tools/importer.py 的 collect()，不把它变成运行时依赖。"""
    global _importer
    if _importer is None:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        spec = importlib.util.spec_from_file_location("astock_importer", IMPORTER_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载导入器：{IMPORTER_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _importer = mod
    return _importer


def _patch(mod: ModuleType, name: str, fn: Callable, group: str) -> None:
    _originals.setdefault(mod.__name__, {}).setdefault(name, getattr(mod, name))
    setattr(fn, "__storage__", "db")
    setattr(mod, name, fn)
    _state[group].append(f"{mod.__name__}.{name}")


def _orig(mod_name: str, name: str) -> Callable:
    return _originals[mod_name][name]


def _sync(repo, src: str, tables: Iterable[str]) -> dict:
    """写操作后：从文件重新采集这几张表，整表覆盖回库（与文件保持逐字段相等）。"""
    names = tuple(tables)
    collected, _ = _load_importer().collect(src, names)
    counts = {}
    with repo.tx():
        for t in names:
            counts[t] = repo.replace_all(t, collected[t])
    return counts


def _wrap_write(mod: ModuleType, name: str, repo, src: str,
                tables: tuple[str, ...], after: Callable | None = None) -> None:
    """保留原写函数（含备份 / 审计日志 / 原子替换），只在其成功后追加一次库同步。"""
    original = getattr(mod, name)

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            _sync(repo, src, tables)
        except Exception as exc:
            print(f"[storage] {mod.__name__}.{name} 写后同步失败："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        if after is not None:
            try:
                after()
            except Exception as exc:
                print(f"[storage] {mod.__name__}.{name} 后置重建失败："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return result

    setattr(wrapper, "__storage__", "db-write-sync")
    setattr(mod, name, wrapper)
    _state["writes"].append(f"{mod.__name__}.{name}→{'+'.join(tables)}")


def state() -> dict:
    """给自检工具看的状态快照；不进任何 API 响应，避免扰动 diff。"""
    with _lock:
        out = {k: v for k, v in _state.items()
               if k != "repo" and not k.startswith("_")}
        out["repo_ready"] = _state["repo"] is not None
        return out


def uninstall() -> None:
    """恢复全部原函数（只给自检脚本用；服务运行期不调用）。"""
    with _lock:
        for mod_name, fns in _originals.items():
            mod = sys.modules.get(mod_name)
            if mod is not None:
                for name, fn in fns.items():
                    setattr(mod, name, fn)
        _originals.clear()
        _state.update({"installed": False, "mode": "files", "patched": [],
                       "writes": [], "repo": None, "src": None, "bootstrap": None,
                       "fallback_reason": None})


# ---------------------------------------------------------------- 安装
def install(cfg=None, src: str | None = None, repo=None, force: bool = False) -> dict:
    """幂等安装。默认档直接返回；db 档完成自举 + 读写接线。

    cfg / src / repo 允许自检脚本注入（例如 :memory: 库 + 夹具目录），
    服务运行时全部留空即可。
    """
    global _state
    with _lock:
        if _state["installed"] and not force:
            return state()
        if force:
            uninstall()

        cfg = cfg or load_config()
        src = os.path.abspath(src or os.environ.get("ASTOCK_SRC") or DEFAULT_SRC)
        _state["src"] = src
        _state["mode"] = cfg.read_from

        if cfg.read_from != "db":
            _state["installed"] = True
            print("[storage] STORAGE_READ=files：保持文件读路径，未替换任何函数")
            return state()

        strict = (os.environ.get("STORAGE_STRICT") or "").strip() in ("1", "true", "yes")
        try:
            import news
            import pnl
            import store
            import stops
            import tracking
            import watchstore

            active_repo = repo or get_repository(cfg)
            bootstrap = {"mode": "skipped", "tables": {}, "warnings": []}
            if (os.environ.get("STORAGE_BOOTSTRAP") or "auto").strip().lower() != "off":
                total = sum(active_repo.count(t) for t in ASSET_TABLES)
                if total == 0:
                    collected, warns = _load_importer().collect(src, ASSET_TABLES)
                    with active_repo.tx():
                        for t in ASSET_TABLES:
                            bootstrap["tables"][t] = active_repo.replace_all(
                                t, collected[t])
                    bootstrap["mode"] = "from_files"
                    bootstrap["warnings"] = warns
                    bootstrap["src"] = src
                else:
                    bootstrap["mode"] = "existing_db"
                    bootstrap["tables"] = {t: active_repo.count(t) for t in ASSET_TABLES}
            _state["bootstrap"] = bootstrap

            # ---- 读：8 张表全切到 db.bridge ----
            def holdings_load():
                return b.db_holdings_load(active_repo)

            def holdings_codes():
                return b.db_holdings_codes(active_repo)

            def watchlist_load():
                return b.db_watchlist_load(active_repo)

            def watchlist_codes():
                return [r["code"] for r in b.db_watchlist_load(active_repo)]

            def watchlist_items():
                return b.db_watchlist_items(active_repo)

            def realized_list():
                return b.db_realized_list(active_repo)

            def pnl_history():
                return b.db_pnl_history(120, active_repo)

            def news_dates():
                return b.db_news_dates(10 ** 6, active_repo)

            def news_load(date: str | None = None):
                return b.db_news_get(date, active_repo)

            def news_index(limit: int = 30):
                return b.db_news_index(limit, active_repo)

            def plan_get(code: str):
                return b.db_plan_get(code, active_repo)

            def plans_index():
                return b.db_plans_index(active_repo)

            def period_list(code: str):
                return b.db_period_list(code, active_repo)

            def stops_load(current_trade_date: str | None = None,
                           force: bool = False) -> dict:
                """库读 + 保留原懒重算规则（交易日变更 / 持仓变更 / force）。"""
                data = b.db_stops_get(active_repo)
                stale = stops._needs_recompute(data, b.db_holdings_load(active_repo))
                if (force or stale or not data
                        or (current_trade_date and data.get("date") != current_trade_date)):
                    try:
                        data = stops.recompute()
                    except Exception as exc:
                        print(f"[stops] 重算失败：{type(exc).__name__}: {exc}")
                        data = data or {"date": None, "items": {}, "history": {}}
                return data

            reads = (
                (store, "load", holdings_load), (store, "codes", holdings_codes),
                (watchstore, "load", watchlist_load),
                (watchstore, "codes", watchlist_codes),
                (watchstore, "items", watchlist_items),
                (pnl, "load_realized", realized_list),
                (pnl, "load_history", pnl_history),
                (news, "list_dates", news_dates), (news, "load", news_load),
                (news, "index", news_index),
                (tracking, "load", plan_get), (tracking, "list_all", plans_index),
                (tracking, "period_list", period_list),
                (stops, "load", stops_load),
            )
            for mod, name, fn in reads:
                _patch(mod, name, fn, "patched")

            # ---- 写：保留原函数，成功后同步对应表 ----
            def reindex_after():
                _orig("tracking", "_reindex")()

            _wrap_write(store, "add", active_repo, src, ("holdings",))
            _wrap_write(store, "update", active_repo, src, ("holdings",))
            _wrap_write(store, "remove", active_repo, src, ("holdings",))
            _wrap_write(watchstore, "add", active_repo, src, ("watchlist",))
            _wrap_write(watchstore, "remove", active_repo, src, ("watchlist",))
            _wrap_write(pnl, "record", active_repo, src, ("pnl_daily",))
            _wrap_write(pnl, "record_realized", active_repo, src, ("realized",))
            _wrap_write(news, "save", active_repo, src, ("news_daily",))
            _wrap_write(news, "purge", active_repo, src, ("news_daily",))
            _wrap_write(tracking, "save", active_repo, src, ("tracking_plans",),
                        after=reindex_after)
            _wrap_write(tracking, "save_period", active_repo, src,
                        ("tracking_periods",), after=reindex_after)
            _wrap_write(stops, "recompute", active_repo, src, ("stops",))
            # _reindex 保留原实现；它内部调用的 load / period_list 已经是 db 版，
            # 所以写出来的 index.json 与库一致。
            _originals.setdefault("tracking", {}).setdefault("_reindex", tracking._reindex)

            _state["repo"] = active_repo
            _state["installed"] = True
            print(f"[storage] STORAGE_READ=db：自举 {bootstrap['mode']}，"
                  f"替换 {len(_state['patched'])} 个读函数、"
                  f"包装 {len(_state['writes'])} 个写函数，src={src}")
            return state()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if strict:
                raise
            uninstall()
            _state.update({"installed": True, "mode": "db", "src": src,
                           "fallback_reason": reason})
            print(f"[storage] db 档初始化失败，已回退 files：{reason}", file=sys.stderr)
            return state()


def install_from_env() -> dict:
    """main.py 用的入口：环境变量里没开就完全不动作。"""
    return install()


def config_for_test(**kw):
    """自检脚本用：在不改进程环境的前提下构造一份内存库配置。"""
    return replace(load_config(), **kw)


__all__ = ["install", "install_from_env", "uninstall", "state", "config_for_test",
           "DEFAULT_SRC"]
