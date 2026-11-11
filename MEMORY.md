# MEMORY.md — mirrors.tibhpc.net 关键变更记录

## admin_ui_overhaul（admin 后台推翻重写）

- **时间**：2026-08-07 ~ 2026-08-11
- **内容**：217KB 单文件 `admin.html` 重写为原生 ES module 多文件架构（无构建链），17 视图 / core 框架层拆分。
- **结构**：
  - `admin.html` 薄壳（登录页 + SVG sprite + 侧栏/顶栏 + modal/confirm/toast 挂载点）
  - `admin/css/{tokens,base,components}.css`
  - `admin/js/core/{api,dom,state,components,format,poller,router}.js`
    - `components.js` v3：27 个字符串工厂（旧 16 个 API 兼容 + Tip/Callout/Collapse/Dropdown/MenuItem/Segmented/Kv/Ring/Steps/Avatar/CopyBtn），CopyBtn 依赖模块内自动绑定的 data-copy 全局委托
  - `admin/js/views/*.js` 17 个视图，`main.js` 用 `registerLazy` 动态 import 懒加载（首屏只载当前路由，mirrors/sync 空闲预取）
- **硬约束兑现**：后端 `app.py` 零改动；所有写操作走原端点，`audit_log`/`sync_history` 审计链路自动保留；CSRF 注入与 401/403 处理复刻。
- **nginx**：`location ^~ /admin/ { alias /var/www/mirrors.tibhpc.net/admin/; }`（`^~` 优先于数据库通配正则）；`location = /admin` 代理到 Flask 5000。
- **注意**：
  - 旧版 `admin.html.legacy.bak` 已不存在，无一键回滚，改前端前先确认。
  - 视图模块只依赖 core 导出（import 校验脚本见会话记录）；新增视图须在 `main.js` 注册 `registerLazy` 并补 `router.js` 的 `ROUTE_META`。
  - 视图内禁止 inline `onclick`（ES module 无全局作用域），用事件委托 `data-action` 或直接 `addEventListener`。
  - 告警角标接口 `/alerts` 每次调用有 0.5s CPU 采样；`state.fetchAlertsCached` 提供 60s TTL，dashboard 已接入，勿绕过缓存高频调用。
- **验收遗留**：17 视图浏览器逐屏对比与写操作审计矩阵需域账号登录后人工核验。

## abnormal_sources_fix（2026-10-22）

修复 ncbi / interpro / uniprot / Ensembl / ensembl_genomes 长期 timeout 或 pending：

- **根因（系统性）**：8.74 的 Python `sqlite3` 链接 SQLite 3.7.17，不支持 `INSERT ... ON CONFLICT`。`app.py` 写 `resume_queue` / `settings` 报 `near "ON": syntax error`，自动续跑队列从未生效。改为 UPDATE-then-INSERT / `INSERT OR REPLACE`，重启 flask-mirror 后队列已正常写入。
- **interpro**：`matches-api-data.tar.gz` 1.9T，旧逻辑统一 4h 超时，每轮必被杀。>500G 单文件改为 18h 窗口、2 次重试；wrapper 26h → 60h。
- **uniprot**：上游已无 `uniparc_external`（rsync 404），移除；`reference_proteomes` 改为二级子目录拆分；大目录按 4/6h 分档超时；wrapper 48h。
- **ncbi**：261T，优先库单轮常 >26h。wrapper 26h → 168h；巨库继续靠多次 cron 断点续传。
- **Ensembl**：旧脚本整 `pub/ -aL`（含 release-19~116 历史 + 5.7T data_files）绝对跑不完。改为只同步 `current/`（约 129G）+ 根散文件并重建 current_* 软链；`data_files` 默认跳过（需同步时设 `ENSEMBL_SYNC_DATA_FILES=1`）；wrapper 48h。
- **ensembl_genomes**：current 组由整组一条 rsync 改为按二级子目录逐条同步（plants/current 整组 4h 反复被 EBI reset，8 次重试耗 47h）；重试 8 → 2；wrapper 72h。
- **操作**：2026-10-22 09:45 起手动重启 interpro / uniprot；ncbi、Ensembl、ensembl_genomes 在 `resume_queue` 排队，cron（周五/周六）兜底。备份：`app.py.bak.sqlfix.20261022`、`*.bak.fix.20261022`。
