-- astock-v2 存储层 DDL（SQLite 方言）
--
-- 设计口径：
--   1) 只把「用户手写 / 不可重建 / 需要事务」的资产入库；raw/ snapshot/ cases/
--      profile/ charts/ 等大块不可变产物继续留文件。
--   2) 每张表都带 owner 列，为将来多用户预留；单用户时恒为 'default'。
--   3) payload 保存整行原始 JSON（ensure_ascii=False）。业务列是从 payload 抽出的
--      索引列，只用于查询与排序，不参与还原 —— 因此库读回的数据与文件逐字段等价，
--      不会因为「ORM 映射漏字段」而丢信息。
--   4) 所有主键都可幂等 UPSERT；realized 是只增台账，用 payload 指纹去重。
--
-- 表分组：
--   core 7 张  holdings / watchlist / stops / realized / tracking_plans /
--              tracking_periods / news_daily
--   补充 1 张  pnl_daily（实测发现 portfolio/pnl_daily.json 也是不可重建台账）
--   系统 3 张  job_runs（阶段 4 去重）/ job_defs（自定义 cron）/ schema_meta
-- schema 1.1 起加入 job_defs；全部用 CREATE TABLE IF NOT EXISTS，老库自动升级。

CREATE TABLE IF NOT EXISTS schema_meta (
  key         TEXT PRIMARY KEY,
  value       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings (
  owner       TEXT NOT NULL DEFAULT 'default',
  code        TEXT NOT NULL,
  name        TEXT,
  cost        REAL,
  quantity    REAL,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, code)
);
CREATE INDEX IF NOT EXISTS ix_holdings_owner ON holdings(owner);

CREATE TABLE IF NOT EXISTS watchlist (
  owner       TEXT NOT NULL DEFAULT 'default',
  code        TEXT NOT NULL,
  name        TEXT,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, code)
);
CREATE INDEX IF NOT EXISTS ix_watchlist_owner ON watchlist(owner);

-- stops 是一个整体对象（date / items / history），不是行数组，按 scope 存一份
CREATE TABLE IF NOT EXISTS stops (
  owner       TEXT NOT NULL DEFAULT 'default',
  scope       TEXT NOT NULL DEFAULT 'default',
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, scope)
);

-- 已清仓台账：只增不改，fingerprint = sha256(payload)[:32]
CREATE TABLE IF NOT EXISTS realized (
  owner        TEXT NOT NULL DEFAULT 'default',
  fingerprint  TEXT NOT NULL,
  code         TEXT,
  date         TEXT,
  payload      TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  PRIMARY KEY (owner, fingerprint)
);
CREATE INDEX IF NOT EXISTS ix_realized_code ON realized(owner, code);

-- 每日盈亏汇总：依赖当日快照，事后无法从行情重建，故入库
CREATE TABLE IF NOT EXISTS pnl_daily (
  owner       TEXT NOT NULL DEFAULT 'default',
  date        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, date)
);

CREATE TABLE IF NOT EXISTS tracking_plans (
  owner       TEXT NOT NULL DEFAULT 'default',
  code        TEXT NOT NULL,
  name        TEXT,
  status      TEXT,
  cad         TEXT,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, code)
);
CREATE INDEX IF NOT EXISTS ix_plans_cad ON tracking_plans(owner, cad, status);

CREATE TABLE IF NOT EXISTS tracking_periods (
  owner       TEXT NOT NULL DEFAULT 'default',
  code        TEXT NOT NULL,
  date        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, code, date)
);
CREATE INDEX IF NOT EXISTS ix_periods_code_date ON tracking_periods(owner, code, date DESC);

CREATE TABLE IF NOT EXISTS news_daily (
  owner       TEXT NOT NULL DEFAULT 'default',
  date        TEXT NOT NULL,
  item_count  INTEGER,
  payload     TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, date)
);

-- 阶段 4：同一交易日同一任务只跑一次，靠主键去重
CREATE TABLE IF NOT EXISTS job_runs (
  owner       TEXT NOT NULL DEFAULT 'default',
  job_type    TEXT NOT NULL,
  trade_date  TEXT NOT NULL,
  status      TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT,
  detail      TEXT,
  PRIMARY KEY (owner, job_type, trade_date)
);
CREATE INDEX IF NOT EXISTS ix_job_runs_type ON job_runs(owner, job_type, trade_date DESC);

-- 自定义调度定义：存可读 cron 与参数，不存 APScheduler 的 pickle job 对象。
-- 重启后由 scheduler.core 读表重建；改 handler 实现不会导致反序列化失败。
CREATE TABLE IF NOT EXISTS job_defs (
  owner       TEXT NOT NULL DEFAULT 'default',
  id          TEXT NOT NULL,
  job_type    TEXT NOT NULL,
  cron        TEXT NOT NULL,
  params      TEXT NOT NULL DEFAULT '{}',
  enabled     INTEGER NOT NULL DEFAULT 1,
  note        TEXT,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (owner, id)
);
