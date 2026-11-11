# mirrors.tibhpc.net 镜像站

> 中国科学院西藏区域协同创新中心 HPC 公共镜像站。
> 提供生物数据库镜像、操作系统镜像、软件应用镜像的同步、展示与下载服务。
>
> 文档对照现网：`data/mirrors.db` + `app.py`（~7.1k 行）+ `admin/`（2026-11 核对）。

---

## 一、系统概述

| 端 | 路径 | 功能 |
|----|------|------|
| 用户端 | `https://mirrors.tibhpc.net/` | 浏览镜像列表、查看大小/状态/下次同步、下载文件 |
| 管理端 | `https://mirrors.tibhpc.net/admin` | 镜像增删改查、触发同步、定时调度(crontab/Ansible)、审计、监控、告警、AI 巡检/技能对话 |

### 两节点架构(关键约束)

| 节点 | IP | 角色 | 说明 |
|------|-----|------|------|
| web 机 | 172.16.8.13 | Flask + nginx | 对外服务。`/hpcfs` 是 **NFS 挂载**,列目录慢、`du` 会卡死。**8.13 永不自己算大小/状态,只接收上报** |
| fnode5 | 172.16.8.74 | 同步执行机 + 扫描机 | `/hpcfs` 是**本地磁盘**,du/rsync 秒级。同时也是 Ansible 控制机 |

两台挂载同一共享存储 `/hpcfs/fpublic`。ssh 单向免密 8.13 → 8.74。**所有重 IO(同步、du)在 8.74 执行,结果回写 8.13**。

### 数据目录

```
/hpcfs/fpublic/database/      # 生物数据库镜像根(BASE_MIRROR_PATH)
/hpcfs/fpublic/mirrors/        # 系统源根(BASE_SYSTEM_PATH)
  ├── os_mirrors/              # 操作系统镜像
  └── soft_mirrors/            # 软件应用镜像
```

---

## 二、架构

```
                      ┌──────────────────────────────────────┐
                      │     mirrors.tibhpc.net (公网)         │
                      └────────────────┬─────────────────────┘
                                       │
                         ┌─────────────▼─────────────┐
                         │  nginx (8.13)             │
                         │  - 静态页 index/soft/doc  │
                         │  - /api/  -> Flask:5000    │
                         │  - /admin/ -> ES module 前端│
                         │  - /browse/<key>/ 大目录分页│
                         │  - 其余路径 autoindex -> /hpcfs│
                         └─────────────┬─────────────┘
                                       │
                         ┌─────────────▼─────────────┐
                         │  Flask app.py (8.13)      │
                         │  - sync_data 内存字典    │
                         │  - SQLite 镜像元数据     │
                         │  - 管理端 API / 鉴权     │
                         │  - AI 巡检 + 技能 Agent  │
                         │  - 6 后台 daemon 线程    │
                         └─────────────┬─────────────┘
                                       │ ssh(单向免密 8.13->8.74)
                         ┌─────────────▼─────────────┐
                         │  fnode5 (8.74)            │
                         │  - wrapper.py + 同步脚本  │
                         │  - crontab 定时同步       │
                         │  - Ansible 控制机(32 主机)│
                         │  - du 大小扫描            │
                         └─────────────┬─────────────┘
                                       │ 跑完同步回写
                                       ▼
                  wrapper.py -> ssh 8.13 _report_client.py -> POST /api/report (X-Reload-Token)
```

**数据流**:

- **页面展示**:nginx → Flask `/api/status` → 内存 `sync_data` ← SQLite(由 `/api/report` 写入)
- **文件下载**:nginx 直接 `alias` 到 `/hpcfs`,不经 Flask
- **同步触发**:admin 页点同步 → Flask ssh 8.74 跑 wrapper/脚本 → 结果落 `/hpcfs` → 回写 `/api/report`
- **大小/状态采集**:8.74 wrapper 跑完同步后 `du` → 经 `_report_client.py` 调 `/api/report` 回写 8.13(8.13 永不本机 du)
- **超时续跑**:`/api/report` 判定可续跑 → 入 `resume_queue` → `_auto_resume_daemon` 再触发 wrapper

交互式架构图见 `deploy/architecture.md`。

---

## 三、镜像清单

共 **60** 个镜像（**23** 生物库 + **37** 系统/软件源）。其中 **47** 个已接入同步脚本，**13** 个仅有占位条目（无脚本）。

### 生物数据库镜像(23)

| key | 名称 | url | 同步脚本 |
|-----|------|-----|---------|
| EBI | EBI | ebi/ | ebi.py |
| Ensembl | Ensembl-DB | ensembl/ | ensembl.py |
| GTDB | GTDB | GTDB/ | gtdb.py |
| PDB-NetXGen | PDB-NetXGen | pdb_nextgen/ | - |
| PMC | PMC | PMC/ | pmc.py |
| TARA | TARA | TARA/ | - |
| alphafill | Alphafill | alphafill/ | - |
| alphafold | AlphaFold DB | alphafold/ | alphafold.py |
| antiSMASH | antiSMASH-database | antiSMASH-database/ | - |
| biocyc | BioCyc | biocyc/ | - |
| classify_predicted_pdbs | AlphaFold2 Predicted DB | classify_predicted_pdbs/ | classify_predicted_pdbs.py |
| ddbj | DDBJ | ddbj/ | ddbj.py |
| ensembl_genomes | Ensembl Genomes | ensembl_genomes/ | ensembl_genomes.py |
| insdc | INSDC | insdc/ | - |
| interpro | InterPro | interpro/ | interpro.py |
| kegg | kegg | kegg/ | - |
| ncbi | NCBI | ncbi/ | ncbi.py |
| pdb | PDB | pdb-redo/ | pdb.py |
| pfam | Pfam | pfam/ | pfam.py |
| prosite | Prosite | prosite/ | - |
| rcsb_pdb | RCSB PDB | pdb/ | rcsb_pdb.py |
| rhea | Rhea | rhea/ | - |
| uniprot | UniProt | uniprot/ | lftp_uniprot.py |

### 系统/软件源镜像(37)

| key | url | 同步脚本 |
|-----|-----|---------|
| almalinux | os_mirrors/cd-images/almalinux/ | almalinux_sync.py |
| almalinux-elevate | os_mirrors/almalinux-elevate/ | almalinux-elevate.py |
| almalinux-epel | os_mirrors/cd-images/almalinux-epel/ | - |
| almalinux-kitten | os_mirrors/almalinux-kitten/ | almalinux-kitten.py |
| apptainer | soft_mirrors/apptainer/ | apptainer.py |
| bioconda | soft_mirrors/bioconda/ | bioconda.py |
| bioconductor | soft_mirrors/bioconductor/ | bioconductor.py |
| centos-stream | os_mirrors/centos-stream/ | centos.py |
| centos-vault | os_mirrors/cd-images/centos-vault/ | centos-vault.py |
| conda | soft_mirrors/conda/ | conda.py |
| conda-forge | soft_mirrors/conda-forge/ | conda-forge.py |
| cran | soft_mirrors/CRAN/ | cran.py |
| debian | os_mirrors/cd-images/debian-releases/ | debian.py |
| docker-ce | soft_mirrors/docker-ce/ | docker-ce.py |
| epel | os_mirrors/epel/ | epel.py |
| fedora | os_mirrors/cd-images/fedora/ | fedora_sync.py |
| golang | soft_mirrors/golang/ | golang.py |
| julia | soft_mirrors/julia/ | julia.py |
| kubernetes | soft_mirrors/kubernetes/ | kubernetes.py |
| mongodb | soft_mirrors/mongodb/ | mongodb.py |
| mysql | soft_mirrors/mysql/ | mysql.py |
| nodejs | soft_mirrors/nodejs-release/ | nodejs.py |
| nvidia | soft_mirrors/NVIDIA/ | nvidia.py |
| openeuler | os_mirrors/cd-images/openEuler/ | openeuler.py |
| opensuse | os_mirrors/cd-images/openSUSE/ | opensuse_sync.py |
| pypi | soft_mirrors/pypi/ | pypi_sync.py |
| python | soft_mirrors/python/ | python.py |
| rhel | os_mirrors/rhel/ | - |
| rhel-iso | os_mirrors/cd-images/rhel-releases/ | - |
| rocky | os_mirrors/cd-images/rocky-releases/ | rocky_sync.py |
| rocky-vault | os_mirrors/cd-images/rocky-vault/ | rocky-vault.py |
| rust | soft_mirrors/rust/ | rust.py |
| spack | soft_mirrors/spack/ | spack.py |
| ubuntu | os_mirrors/ubuntu/ | ubuntu-apt.py |
| ubuntu-cd | os_mirrors/cd-images/ubuntu-releases/ | ubuntu.py |
| ubuntu-iso | os_mirrors/cd-images/ubuntu_official_source/ | ubuntu_iso_sync.py |
| windows | os_mirrors/cd-images/windows-releases/ | - |

> **无脚本(13)**:生物库 PDB-NetXGen / TARA / alphafill / antiSMASH / biocyc / insdc / kegg / prosite / rhea；系统源 almalinux-epel / rhel / rhel-iso / windows。既无脚本也无 cron，未接入，单独排期。

---

## 四、同步与调度机制

### 同步脚本

所有同步脚本位于 `/hpcfs/fpublic/manage/baiwx/script/`,在 **8.74** 执行(本地盘,rsync 快)。`wrapper.py` 是统一入口:接收脚本名 → 执行对应 `.py` → `du` 算大小 → ssh 回写 8.13。

```
/hpcfs/fpublic/manage/baiwx/script/
├── wrapper.py          # 统一入口,靠 basename 自动匹配镜像 key
├── *.py                # 同步脚本(rsync / lftp / bandersnatch 等)
├── wrapper.log         # 结构化日志
└── wrapper.cron.log    # crontab 兜底日志(捕获异常)
```

### cron 调度(两条链路,关键)

**链路 A(真实调度源,在用)**:8.74 系统 crontab → `wrapper.py <脚本>` → 同步脚本 + du → ssh 8.13 `_report_client.py` → `POST /api/report` 回写。已覆盖有 `sync_script` 的镜像,闭环完整。

> **全量接入新镜像 = 补脚本 + 加 crontab 行**(wrapper 靠 basename 自动匹配 key,无需改 wrapper)。

**链路 B(默认禁用,勿开!)**:8.13 `cron_sync_daemon`(每分钟)读 DB `cron_expr/cron_enabled` → ssh 8.74 **裸跑脚本**(不经 wrapper)→ 自己改内存 + 写 sync_history,**不算 size、不调 /api/report**。当前默认需显式设置 `ENABLE_DB_CRON_DAEMON=1` 才会启动。**勿开启**,否则与链路 A 双触发且数据不全。DB cron 字段仅作历史兼容元数据。

辅助:
- `crontab_cache_daemon`(每 5 分钟 ssh 8.74 `crontab -l`):缓存脚本→cron 映射,给 `/api/status` 算"下次同步时间"(读 8.74 真实 crontab,非 DB)。
- `/api/admin/crontab`(GET/PUT) + `/api/admin/crontab/task`(增删改):直接读写 8.74 crontab(PUT 带备份 + 回滚)。
- `_auto_resume_daemon`:消费 `resume_queue`,对超时/可续跑镜像自动再触发 wrapper(分档并发上限)。

### 同步触发方式

1. **手动触发**:admin 页点「同步」→ Flask 异步 ssh 8.74 跑 wrapper → 写 sync_history
2. **定时同步**:链路 A,8.74 crontab 定时跑 wrapper
3. **失败告警**:同步失败调 `notify_sync_fail` → 读 settings webhook → 推钉钉/企微/邮件
4. **AI 自动重试**:`AGENT_AUTO_RETRY` 开启时,巡检自动重试失败镜像
5. **超时续跑**:上报判定可续跑 → `resume_queue` → `_auto_resume_daemon` 再跑

### 同步历史

每次同步写 `sync_history` 表:key、name、status、trigger_type(manual/schedule)、trigger_by、started_at、finished_at、duration_sec、output_summary。admin「同步历史」页可查(7 天成功率卡片 + 趋势图 + 分页明细)。

闭环探针:`GET /api/admin/loop`、`GET /healthz?deep=1`(daemon 心跳 / resume_queue / 最近上报)。

---

## 五、大小 / 状态上报机制(07-28 重构)

### 问题背景

8.13 通过 NFS 读 `/hpcfs`,`du -sh` 大目录会卡死(ncbi 数百 T、ebi 数百 T 等)。旧 `mirror-scan.service`(app-02.py)在 8.13 本机 du,曾卡死 18 小时。

### 解决方案:上报机制

废弃 8.13 本机 du,改为 **8.74 跑完后主动上报**:

```
8.74 wrapper.py 跑完同步
   ↓ du 本地盘(秒级)
   ↓ ssh 8.13 调 _report_client.py(payload 走 stdin 避引号地狱)
8.13 _report_client.py
   ↓ POST /api/report(带 X-Reload-Token,secrets.compare_digest 校验;瞬时失败自动重试)
8.13 app.py /api/report
   ├─ 落库 mirrors(status / total_size_gb / last_sync_date)
   ├─ 刷内存 sync_data
   ├─ 写 sync_history
   ├─ 超时/可续跑 → resume_queue
   └─ 失败则调 notify_sync_fail
```

**8.13 永不自己 du**。旧 `mirror-scan.service` 已删除,`app-02.py` 已禁用。旧 `/api/update_sizes`、`/api/admin/reload-sizes` 仍保留兼容。

### app.py 端配合

- `size_scan_daemon`:启动回填 `last_sync_date`(纯 stat,不 du),之后每 10 分钟从 db reload 大小到内存;**本机不 du**
- `_scan_system_source_size`:admin 手动采集系统源大小,远程 ssh 8.74 跑 du(不本地卡)
- `/api/report` 是 8.13 的**唯一**状态来源

---

## 六、AI 运维能力

### 自动巡检

一轮巡检 = 统计状态 →(可选)`AGENT_AUTO_RETRY` 自动重试失败镜像 → 调 AI 诊断根因 → 结构化报告。

**网关配置**(`/etc/systemd/system/flask-mirror.env`,以现网 env 为准):

```
ANTHROPIC_BASE_URL=...          # Messages / OpenAI 兼容网关
ANTHROPIC_MODEL=glm-5.2         # 现网推荐;claude-* 门面名在部分网关上会 503
AGENT_SKILL_BASE_URL=...        # 技能 Agent 对话网关(可与上不同)
AGENT_AUTO_RETRY=1
AGENT_INSPECT_INTERVAL=1800
```

> **坑**:部分 new-api 风格网关列出 `claude-*` 门面名,实际通道不可用 → 503。**改模型名只动 env**,`daemon-reload` + `restart` 生效。

**巡检 API**:

| 方法 | 路径 | 功能 |
|------|------|------|
| POST/GET | `/api/admin/agent/inspect` | 触发一轮巡检 / 取最近一次 |
| POST | `/api/admin/agent/sync/<key>` | AI 诊断单个镜像 |
| GET | `/api/admin/agent/logs` | 巡检日志 |

`_agent_inspect_daemon` 后台自动巡检。

### 技能 Agent(自主排查)

技能循环可调用列镜像、查历史/日志、探上游、读写 crontab、跑 playbook、触发同步等工具,支持多轮会话落库。

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/admin/agent/skills` | 可用技能列表 |
| POST | `/api/admin/agent/ask` | 同步问答 |
| POST | `/api/admin/agent/ask/stream` | 流式问答 |
| GET/DELETE | `/api/admin/agent/sessions[/<sid>]` | 会话列表 / 详情 / 删除 |

---

## 七、Ansible 调度

8.74 是 **Ansible 控制机**(`/hpcfs/fpublic/manage/ansible/`,管约 32 主机 / 5 分组:`sync_nodes`/`bnode`/`web`/`rhel9`/`rhel8`)。admin 经 8.13 ssh 8.74 在该目录跑 ansible。

**API**(`/api/admin/ansible/*`):

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/ansible/inventory` | inventory 分组/主机 |
| GET | `/ansible/playbooks` | 列 `playbooks/` 下 .yml |
| GET | `/ansible/log` | ansible.log 内容 |
| POST | `/ansible/run` | 异步执行 playbook(timeout 1800s) |
| GET | `/ansible/runs` | 执行历史 |
| GET | `/ansible/runs/<id>` | 单次执行详情 |

- 表 `ansible_runs`(run_id/playbook/lim/status/started_at/finished_at/rc/output/triggered_by)
- limit 白名单:`all`/`sync_nodes`/`bnode`/`web`/`rhel9`/`rhel8`
- playbook 限 `playbooks/` 下现成 .yml(**不编辑、不 ad-hoc**)

---

## 八、API 接口

### 用户端

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 镜像站首页(index.html) |
| GET | `/api/status` | 全部镜像状态(已脱敏:不含 sync_cmd/sync_script/is_scanning) |
| POST | `/api/sync` | 触发同步(@login_required) |
| GET | `/api/log/<key>` | 查看镜像最近同步时间/大小(@login_required) |
| GET | `/api/permission` | 权限信息(@login_required) |
| GET | `/db/<key>/<filename>` | 文件下载(Flask,实际下载走 nginx) |
| GET | `/browse/<key>/...` | 目录分页浏览(大目录专用) |
| GET | `/healthz` | 健康检查(`?deep=1` 含 daemon/resume_queue) |

### 上报接口(内部,8.74 调用)

| 方法 | 路径 | 鉴权 | 功能 |
|------|------|------|------|
| POST | `/api/report` | `X-Reload-Token` | **8.13 唯一状态来源**:回写 status/size/last_sync_date + 刷内存 + 写 history + 续跑入队 |
| POST | `/api/update_sizes` | `X-Reload-Token` | 旧兼容 |
| GET/POST | `/api/admin/reload-sizes` | `X-Reload-Token` | 旧兼容:刷内存大小 |

### 管理端(需登录 `/api/admin/login`,写操作需 CSRF token)

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/admin/login` | 登录(限流 5 次/300s;本地 admin 或 AD 域) |
| POST | `/api/admin/logout` | 登出 |
| GET | `/api/admin/check` | 检查登录态(返 csrf_token) |
| GET/POST | `/api/admin/mirrors` | 镜像列表 / 新建 |
| GET | `/api/admin/mirrors/health` | 镜像健康度评分 |
| PUT/DELETE | `/api/admin/mirrors/<key>` | 修改 / 删除 |
| POST | `/api/admin/mirrors/<key>/sync` | 触发单个镜像同步 |
| GET | `/api/admin/dashboard` | 仪表盘统计 |
| GET | `/api/admin/loop` | 同步闭环探针(daemon / resume_queue / 上报) |
| GET | `/api/admin/disks` | 磁盘使用 |
| GET | `/api/admin/traffic` | 流量采样 / 下载排行 |
| GET/POST/... | `/api/admin/upstreams*` | 上游源纳管 + 延迟探测 + discover |
| GET | `/api/admin/sync/status` | 同步状态 |
| GET | `/api/admin/sync/history` | 同步历史 |
| GET | `/api/admin/sync/stats` | 同步统计 |
| GET/PUT | `/api/admin/crontab` | 读写 8.74 crontab(PUT 带备份回滚) |
| PATCH/POST/DELETE | `/api/admin/crontab/task` | 单条 cron 任务增删改 |
| GET/POST | `/api/admin/ansible/*` | Ansible inventory/playbooks/run |
| * | `/api/admin/agent/*` | 巡检 + 技能对话(见第六节) |
| GET | `/api/admin/files/<key>` | 文件浏览 |
| GET | `/api/admin/audit` | 审计日志 |
| GET | `/api/admin/logs/access` | 访问日志 |
| GET | `/api/admin/logs/sync` | 同步日志 |
| GET | `/api/admin/monitor` | 监控数据 |
| GET | `/api/admin/alerts` | 告警列表 |
| GET/PUT | `/api/admin/settings` | 设置(GET 脱敏返 `***`,PUT 保留原值) |
| POST | `/api/admin/settings/test` | 测试告警通道(钉钉/企微/邮件) |
| GET/POST/DELETE | `/api/admin/firewall` | 端口管理(22/80/443 受保护禁删) |
| GET | `/api/admin/users` | 用户列表 |
| GET | `/api/admin/system-sources` | 系统源子目录列表 |
| POST | `/api/admin/system-source-size` | 手动采集系统源大小(远程 8.74 du) |

---

## 九、数据库

**库文件**:`/hpcfs/fhome/baiwx/mirrors/data/mirrors.db`(SQLite)
> 项目根目录若存在 `mirrors.db` 空文件,勿用。

### 表

| 表 | 说明 |
|----|------|
| `mirrors` | 镜像元数据(60 条) |
| `sync_history` | 同步历史(manual / schedule) |
| `audit_log` | 管理端写操作审计(operator 取 session["admin_user"]) |
| `settings` | 键值对(webhook URL、告警开关、钉钉 secret 等,GET 脱敏) |
| `ansible_runs` | Ansible 执行记录 |
| `resume_queue` | 超时/可续跑镜像队列(`not_before` / attempts / tier) |
| `agent_sessions` / `agent_messages` | AI 技能对话会话与消息 |

### mirrors 表字段

| 字段 | 说明 |
|------|------|
| key | TEXT PK,镜像标识 |
| name / description | 显示名 / 描述 |
| url | 相对路径 |
| status | success / pending / syncing / fail / none |
| total_size_gb | du 输出(如 257T、541G) |
| last_sync_date | 最后同步时间 |
| sync_cmd | rsync 命令(JSON 数组,首元素须白名单) |
| sync_script | 同步脚本路径(须位于 `/hpcfs/fpublic/` 下) |
| is_scanning / is_system | 0/1 |
| cron_expr / cron_enabled | 定时表达式 / 开关(**仅元数据,实际调度读 8.74 crontab**) |
| created_at / updated_at | 时间戳 |

### 常用查询

```bash
DB=/hpcfs/fhome/baiwx/mirrors/data/mirrors.db

# 镜像概览
sqlite3 -header -column $DB \
  "SELECT key,name,total_size_gb,last_sync_date,is_system FROM mirrors ORDER BY is_system,key;"

# 状态分布
sqlite3 $DB "SELECT status, COUNT(*) FROM mirrors GROUP BY status;"

# 同步历史(看 cron 是否在跑)
sqlite3 -header -column $DB \
  "SELECT key,status,trigger_type,started_at FROM sync_history ORDER BY id DESC LIMIT 20;"

# 续跑队列
sqlite3 -header -column $DB \
  "SELECT key,reason,tier,not_before,attempts FROM resume_queue ORDER BY not_before;"

# 完整性
sqlite3 $DB "PRAGMA integrity_check;"
```

---

## 十、admin 管理端

管理端已改为 **原生 ES module 多文件架构(无构建链)**:

```
admin.html                 # 薄壳:登录页 + SVG sprite + 侧栏/顶栏 + modal/confirm/toast
admin/css/{tokens,base,components}.css
admin/js/core/{api,dom,state,components,format,poller,router,chart}.js
admin/js/views/*.js        # 13 个视图,main.js registerLazy 懒加载
admin/js/main.js
```

视图:`dashboard` / `mirrors` / `browse` / `sync` / `synchistory` / `crontab` / `ansible` / `monitor` / `storage` / `upstreams` / `logs` / `settings` / `agent`。

**硬约束**:后端写操作走原端点,`audit_log`/`sync_history` 审计链路保留;CSRF 注入与 401/403 处理在 `core/api.js`;视图禁止 inline `onclick`,用 `data-action` 委托。

nginx:`location ^~ /admin/` alias 前端静态文件;`location = /admin` 代理到 Flask 5000。

---

## 十一、文件结构

```
/hpcfs/fhome/baiwx/mirrors/          # 开发/共享副本(部署目录可能在 /www/wwwroot/...)
├── app.py              # Flask 后端(单文件,~7.1k 行)
├── index.html          # 用户端首页
├── soft.html / doc.html
├── admin.html          # 管理端薄壳
├── admin/              # ES module 前端
├── loading.html        # 大目录中转页
├── 404.html
├── logo.png / logo/
├── _report_client.py   # 8.13 本地回写客户端(ssh stdin -> /api/report)
├── restart.sh          # systemctl restart flask-mirror
├── requirements.txt    # Flask + ldap3
├── MEMORY.md           # 关键变更短记录
├── deploy/
│   ├── architecture.md # 拓扑 / 数据流 / ER
│   └── logrotate.mirrors-app
├── app.log
├── vendor/             # chart.umd.min.js
└── data/
    ├── mirrors.db      # SQLite 数据库
    └── upstreams.json  # 上游源纳管

/hpcfs/fpublic/manage/baiwx/script/
├── wrapper.py
├── *.py
└── wrapper.log / wrapper.cron.log

/hpcfs/fpublic/manage/ansible/       # 8.74 Ansible 控制目录
```

---

## 十二、服务部署(systemd)

root 运行,密钥从 `/etc/systemd/system/flask-mirror.env`(600)经 `EnvironmentFile` 注入。

| 服务 | 状态 | 说明 |
|------|------|------|
| `flask-mirror.service` | enabled / active | app.py,绑 127.0.0.1:5000 |
| `mirror-scan.service` | **已删除**(07-28) | 旧本机 du,卡死根源 |
| nginx 80/443 | 运行 | 静态页 + /api/ 反代 + /browse 分页 + autoindex |

**后台 daemon**(app.py 启动时起线程):

| 线程 | 间隔 | 默认 | 职责 |
|------|------|------|------|
| `size_scan_daemon` | 600s | 开 | DB → 内存 reload(本机不 du) |
| `crontab_cache_daemon` | 300s | 开 | 缓存 8.74 crontab |
| `_agent_inspect_daemon` | 1800s | 开 | AI 巡检 / 自动重试 |
| `_traffic_daemon` | 采样 | 开 | 网卡流量采样 / nginx 下载统计 |
| `_auto_resume_daemon` | 短周期 | 开 | 消费 `resume_queue` 续跑 |
| `cron_sync_daemon` | 60s | **关** | DB cron 触发(勿开) |

---

## 十三、运维操作

### 重启服务

```bash
systemctl restart flask-mirror.service
# 或
./restart.sh
```

> ⚠ **切勿手动 `python3 app.py` 起服务**:会留孤儿进程占 5000,systemd 拉新进程报 `Address already in use` 进失败循环。`restart.sh` 仅封装 `systemctl restart flask-mirror`。

### 改 AI 模型 / 密钥

```bash
vim /etc/systemd/system/flask-mirror.env   # 改 ANTHROPIC_MODEL= 等
systemctl daemon-reload && systemctl restart flask-mirror
```

### 查看日志

```bash
tail -f /hpcfs/fhome/baiwx/mirrors/app.log                   # Flask(部署目录以现网为准)
tail -f /hpcfs/fpublic/manage/baiwx/script/wrapper.log       # 同步结构化日志
tail -f /hpcfs/fpublic/manage/baiwx/script/wrapper.cron.log  # crontab 兜底
journalctl -u flask-mirror -f
```

### nginx

```bash
nginx -t && nginx -s reload
```

关键 location:`/api/` → `127.0.0.1:5000`、`/browse/<key>/` → Flask 分页、`^~ /admin/` → 前端静态、`^~ /vendor/` → 本地 JS、其余镜像路径 → autoindex。

### 大小长期「获取中」排查

查 8.74 的 wrapper.py / crontab 是否在跑、`/api/report` 回写是否成功(看 sync_history / `/api/admin/loop`),**不要在 8.13 跑 du**。

### 超大库长期 pending/timeout

见 `MEMORY.md`（2026-10-22）:ncbi / interpro / uniprot / Ensembl / ensembl_genomes 超时窗口与断点续传策略;SQLite 3.7 不支持 `ON CONFLICT`,写 `resume_queue` 须用 UPDATE-then-INSERT。

---

## 十四、安全加固

### 2026-07-26 加固

- **鉴权恢复**:`login_required` 真校验 `session["admin_logged_in"]` + 12h TTL
- **密钥外置**:删除源码默认密码/secret_key,改从环境变量注入,缺失拒启动;`/etc/systemd/system/flask-mirror.env`(600)
- **内部接口令牌**:`/api/report`、`/api/update_sizes`、`/api/admin/reload-sizes` 加 `X-Reload-Token`(`secrets.compare_digest`)
- **命令注入收窄**:`sync_cmd` 首元素白名单(rsync/lftp/bandersnatch/wget/curl/python3/bash)、`sync_script` 须位于 `/hpcfs/fpublic/` 下
- **路径穿越加固**:`is_safe_path` 用 `realpath`+`commonpath`
- **未鉴权写接口封堵**:`/api/sync`、`/api/permission` 加 `@login_required`
- **监听收口**:Flask 绑 `127.0.0.1`;firewalld 移除 5000/tcp 公网
- **admin 防火墙页**:`/api/admin/firewall`(22/80/443 受保护禁删)

### 2026-07-31 加固

- **CSRF**:`before_request` 拦 `/api/admin/` 写请求,`secrets.compare_digest(X-CSRF-Token, session.csrf_token)`;登录签 csrf_token
- **登录限流**:`_login_fails`(ip+user),5 次/300s 锁定返 retry_after
- **密码**:`secrets.compare_digest`
- **settings 脱敏**:GET 有值返 `***`(空值返空),PUT 值为 `***`/`""` 保留原值
- **audit_log**:`operator` 读 `session["admin_user"]`
- **健康度评分**:`/api/admin/mirrors/health` + `_health_score`

### 未做

Flask 降权为非 root 运行(需先配 sudoers 白名单供 ssh/du 用)。

---

## 十五、开发日志(摘要)

### 2026-07:SQLite / admin / 上报重构 / AI / Ansible
- 元数据迁 SQLite;`audit_log`/`settings`/`sync_history`
- `/browse` 大目录分页;8.13 永不本机 du;wrapper → `/api/report` 闭环
- AI 巡检 agent;Ansible 调度页;CSRF / 登录限流 / settings 脱敏

### 2026-08:脱敏 + admin 组件化 + UI 推翻重写
- `/api/status` 公网脱敏(删 sync_cmd/sync_script/is_scanning)
- admin 217KB 单文件重写为 ES module 多文件(17→现 13 视图懒加载);后端零改写端点

### 2026-09~10:镜像扩容 + 超大库续跑修复
- 镜像增至 60(生物 23 + 系统/软件 37);脚本接入约 47
- 2026-10-22:`resume_queue` 因旧 SQLite 不支持 `ON CONFLICT` 从未生效 → 改为 UPDATE-then-INSERT;interpro/uniprot/ncbi/Ensembl/ensembl_genomes 超时与同步范围调整(详见 `MEMORY.md`)
- `_auto_resume_daemon` / `/api/admin/loop` / `_report_client` 重试加固闭环

### 2026-11:README 与现网对齐
- 清单、daemon、表结构、API、admin 前端结构按现码与 `data/mirrors.db` 刷新

---

## 十六、待办

- [ ] **无脚本镜像排期**(13):生物 PDB-NetXGen/TARA/alphafill/antiSMASH/biocyc/insdc/kegg/prosite/rhea;系统 almalinux-epel/rhel/rhel-iso/windows(部分为版权/源关不可镜像)
- [ ] 钉钉加签 secret(框架已实现,等 SEC 后写入 settings + `/api/admin/settings/test`)
- [ ] Flask 降权为非 root 运行
- [x] ~~/api/status 公网脱敏~~
- [x] ~~admin XSS 基础防护(esc) / CSRF / 登录限流~~
- [x] ~~超时续跑队列(resume_queue)~~
- [x] ~~admin 模块化前端~~

---

## 十七、已知坑 / 约束备忘

1. **8.13 永不本机 du**:NFS 挂载,du 大目录卡死。状态只走 `/api/report`。
2. **勿手起 app.py**:孤儿进程占 5000,systemd 失败循环。重启用 `systemctl restart flask-mirror` 或 `./restart.sh`。
3. **AI 模型以 env 为准**:部分网关上 `claude-*` 门面会 503;现网常用 `glm-5.2`。
4. **链路 B(`cron_sync_daemon`)默认禁用且勿开**:仅 `ENABLE_DB_CRON_DAEMON=1` 时启动;否则与链路 A 双触发且数据不全。
5. **真库在 `data/mirrors.db`**:根目录空 `mirrors.db` 勿用。
6. **cron 字段仅历史兼容元数据**:实际调度读 8.74 真实 crontab。
7. **8.74 的 Python sqlite3 可能很旧**(如 3.7):不要写 `INSERT ... ON CONFLICT`;写队列用 UPDATE-then-INSERT / `INSERT OR REPLACE`。
8. **超大库靠断点续传**:ncbi 等单轮可超过一天,靠 cron + `resume_queue` 多次续跑,勿随意杀 wrapper。
9. **admin 无构建链**:改 `admin/js/views/*` 后清浏览器缓存即可;新增视图须在 `main.js` 注册 `registerLazy` 并补 `router.js` 的 `ROUTE_META`。
