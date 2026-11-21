# 私有实现覆盖层

`APP_PROFILE=private` 时在这里放本地专有实现，例如：

- 按契约投递 `digest.json` / `ai/{code}.json` / `news_patch.json` 的 agent 通道
- 自建 SearXNG、私有搜索 API、私有凭据读取逻辑
- 本地自动化任务适配

**整个目录已被 `.gitignore` 排除（本 README 除外），不会进入发布构建。**

约定：覆盖层只能实现 `contracts/` 与 `producers/` / `search/` 定义的接口，
不要在 `backend/` 主代码里 `import contrib.private`。主代码必须能在没有本目录时
独立启动并通过 `tools/verify.py --offline`。
