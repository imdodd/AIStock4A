# astock-v2 · A 股阶段跟踪研究平台

温斯坦四阶段 + 利弗莫尔关键点的**盘后批处理管道 + 只读查询看板**。

它不是传统 Web 应用，而是「一个每天自动跑的批处理管道 + 一张看板」：页面打开时几乎不联网、不计算，直接读盘后算好的结果，因此秒开、不会被上游限流拖死，代价是数据停留在最近一次收盘。

- **单机、文件即数据库**：所有状态落在本地目录，`portfolio/` 是你自己的名单与台账；换机器拷走即可。
- **算法层与工程层分离**：`engines/` 纯算法零 I/O；存储（SQLite 可选）、调度、配置、监控全部运行期接线，不改算法源码。
- **AI 只复述证据**：LLM 解读必须标注 `[依据:字段]`，每个数字都要能在证据包里核对到，编数直接拒绝。
- **隐私默认安全**：`.env`、`backend/data/`、`data-live/`、密钥文件全部在 `.gitignore` 中，仓库里没有任何真实持仓。

## 功能一览

前端单页 `frontend/index.html`，6 个页签：

| 页签 | 内容 |
|---|---|
| 大盘 | 四大指数温斯坦阶段 + 综合仓位建议 + 情绪指标 |
| 板块 | 申万二级 124 板块阶段分布、行业资金流 |
| 关注 | 关注池买点/卖点候选、个股提醒卡 |
| 异动 | 量能异动扫描、证据链（公告/资金流/龙虎榜/涨停池）、AI 异动解读 |
| 持仓 | 持仓盈亏、动态止损、个股跟踪（定期 LLM 分析出期）、盈亏日历、清仓股复盘 |
| 消息 | 资讯流（LLM 判断增量并标注板块/个股/利多利空，无 Key 降级为固定源事实流） |

配套运维页：`config.html`（Key 自助配置）、`jobs.html`（调度任务管理）、`ops.html`（只读运行监控）。

## 快速开始

### 方式一：一键启动（Windows）

双击 `start.bat`。首次运行会自动：检查 Python → 创建虚拟环境 → 安装依赖 → 生成 `.env` → 启动服务并打开浏览器。

要求本机装有 **Python 3.10+**（安装时勾选 *Add python.exe to PATH*）。

### 方式二：手动命令

```bash
# 1. 建虚拟环境并装依赖（运行时依赖只有 fastapi + uvicorn + pydantic，
#    apscheduler 为可选，不装自动退回标准库调度后端）
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt -r requirements-scheduler.txt

# 2. 生成配置（默认值即可运行；想配 AI/检索 Key 见下文）
copy .env.example .env

# 3. 安装自检（离线，不联网不写数据）
cd backend && ..\tools\verify.py --offline

# 4. 启动服务
.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

浏览器打开 `http://127.0.0.1:8000/`（**必须通过服务地址访问**；`file://` 或静态预览需显式加 `?api=http://127.0.0.1:8000`，前端不兜底任何固定端口）。

### 首次使用：让系统长出自己的数据

服务启动后页面还没有数据，先跑一次盘后管道（约 10–20 分钟，多为行业成分股抓取）：

```bash
cd backend
..\.venv\Scripts\python -m jobs.eod
```

然后把你的持仓/自选填进 `backend/data/portfolio/`（仓库已内置贵州茅台等 3 只**样例持仓**，在界面里删掉换成自己的即可），再跑一次 `jobs.eod`，全站数据就有了。之后每个交易日收盘后由调度器自动跑（需 `SCHEDULER_ENABLED=1`，见 `.env.example`）。

## 配置说明

配置文件是仓库根的 `.env`（首次从 `.env.example` 复制；被 `.gitignore` 排除，永不入库）。改档位需重启服务，改 Key 不需要。

**需要填的 Key 只有两个，且都是可选**（不填也能完整运行）：

| 用途 | `.env` 变量 | 说明 |
|---|---|---|
| AI 解读 / 跟踪分析（OpenAI 兼容） | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | DeepSeek、通义等均可；无 Key 时 AI 解读自动退到规则版 |
| 资讯联网检索 | `SEARCH_API_KEY` / `SEARCH_API_BACKEND` | 默认 Tavily（`app.tavily.com` 免费注册，每月 1000 次）；也可 Serper 或 generic 档 |

**Key 自助配置（推荐）**：主界面右上角齿轮 → 「LLM 密钥配置」，或直接打开 `http://127.0.0.1:8000/static/config.html`。选服务商预设、粘贴密钥、保存即时生效，无需改文件重启。安全约束：页面只回 `前6…后4` 掩码，永不显示明文；密钥落 `backend/data/.secrets/`（已 gitignore）；配置接口只有 POST 且强制本机。

**检索档**（`SEARCH_PROVIDER`）：

- `llm_websearch`：LLM 服务端联网直出资讯（DeepSeek 等，按次计费一天不到 ¥1），自动按模板展开查询词、判断增量；
- `search_api`：Tavily / Serper 自配 Key；
- `none`：零 Key 固定源（东财+新浪）事实流直出，只保证「有新闻可看」。

**资讯判断层**（`NEWS_JUDGE=1` 默认开）：LLM 先判断增量——查询词从持仓/自选 + S1/S2 行业动态生成，只收对大局或板块个股**确有增量**的新闻并标注 `sectors/stocks/codes/impact/logic`；无 Key 或全被契约拒绝时本轮不写，宁缺毋滥。`NEWS_JUDGE=0` 退回纯事实层。

其余全是档位开关（`APP_PROFILE` / `STORAGE_READ` / `SCHEDULER_*` / `PRODUCER` …），完整清单与默认值见 `.env.example`。

## 运维与监控

| 页面 | 地址 | 用途 |
|---|---|---|
| 运行监控 | `/static/ops.html` | 只读：服务健康、运行档位、Key 已配/未配（不显值）、任务运行态 |
| 调度管理 | `/static/jobs.html` | 查看固定任务、启停/改 cron/手工触发自定义任务 |
| Key 配置 | `/static/config.html` | 密钥自助配置 |

定时任务默认关闭（`SCHEDULER_ENABLED=0`），开启后支持 APScheduler / 标准库双后端、交易日历、启动追补、自定义任务热增删。`SCHEDULER_DRY_RUN=1`（默认）下只记运行记录不真抓数据，确认无误后再设 0。

## 目录结构

```
backend/
  main.py            FastAPI 服务（GET 查询 + POST 配置/操作）
  engines/           纯算法：温斯坦阶段 / 利弗莫尔关键点 / 六档记录法 / 周期聚合 / 图表装配
  datasource/        取数层：腾讯 / 东财资金流(三层降级) / 证据链 / 板块成员
  jobs/              盘后管道：eod / build_snapshot / fetch_news / news_judge / build_cases / tracking_review
  scheduler/         交易日历 / cron / 双后端调度器 / job_defs / 追补
  contracts/         digest / ai_brief / news_patch 三份 pydantic 契约 + 落盘前校验
  producers/         rule / llm_direct / agent / manual 四档解读产出
  db/                SQLite 存储层（Repository + 运行期接线，STORAGE_READ=db 启用）
  tracking_llm.py    分层取证 + LLM 定期跟踪分析 + 数字契约校验
  data_paths.py      统一数据目录解析（ASTOCK_DATA_DIR）
  config_api.py      Key 自助配置（仅本机、仅 POST、掩码回显）
frontend/            index.html 单页 + config/jobs/ops 三个运维页
tools/               verify.py 安装自检 / importer.py 导入 / verify_storage.py 对账
                     verify_scheduler.py / verify_producers.py / verify_wiring.py 等验收
docs/                SYSTEM-DESIGN.md 架构与算法详设 / NEWS-PIPELINE.md 资讯链路 / contracts/ JSON Schema
examples/            持仓/自选/日历脱敏样例
contrib/private/     私有实现覆盖层（APP_PROFILE=private 时本地加载，不发布）
```

## 存储与契约（简要）

- **文件即数据库**：默认读 JSON 文件；设 `STORAGE_READ=db` 后，启动时自举进 SQLite 并运行期替换 14 个读函数、包装 12 个写函数，算法模块源码一行不改。失败回退 files，`STORAGE_STRICT=1` 时直接抛错。MySQL/PostgreSQL 仅接口骨架，选中明确报错，不静默降级。
- **产出契约**：AI 解读与资讯 patch 必须过 pydantic 契约（`docs/contracts/*.schema.json` 由模型导出）。落盘前校验：`[依据:字段]` 必须能解析到证据、**每个数字都要能在证据包或检索来源里找到**（防编数）、买卖建议词一律拒绝、snippet 命中 prompt injection 整条拒绝。校验不过就退规则档或整条跳过，绝不静默落盘。

## 文档

| 文档 | 内容 |
|---|---|
| `docs/SYSTEM-DESIGN.md` | 架构、算法详设、数据落盘、设计决策 |
| `docs/NEWS-PIPELINE.md` | 资讯链路逐环节拆解（含排查指南） |
| `CONTRIBUTING.md` | 参与贡献须知 |
| `DISCLAIMER.md` / `NOTICE` | 免责声明与许可 |

## 免责

本项目仅用于个人研究，数据来源为公开接口，不构成任何买卖建议。见 `DISCLAIMER.md`。
