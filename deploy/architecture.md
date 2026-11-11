# mirrors.tibhpc.net 系统架构图

> 与代码 / nginx / SQLite 现网交叉验证。交互式版本见 Cursor Canvas。

---

## 1. 拓扑总览

```mermaid
flowchart TB
  U[公网用户<br/>浏览器 / pip / yum] --> NGX

  subgraph WEB["Web 机 · 172.16.8.13"]
    NGX[nginx :443<br/>TLS · 路由分发]
    FLASK[Flask app.py<br/>127.0.0.1:5000]
    ADMIN[admin 前端<br/>ES Module ×17]
    REPORT[_report_client.py]
    DB[(SQLite<br/>data/mirrors.db)]
    NGX -->|/api/| FLASK
    NGX -->|/admin/| ADMIN
    NGX -->|autoindex| HPCFS
    FLASK --> DB
    REPORT -->|POST /api/report| FLASK
  end

  subgraph EXEC["执行机 · 172.16.8.74"]
    WRAP[wrapper.py + 同步脚本]
    CRON[root crontab]
    ANSI[Ansible 控制机<br/>32 主机 / 5 分组]
    DU[du 扫描]
  end

  subgraph STORE["共享存储 · /hpcfs/fpublic"]
    HPCFS[(GPFS)]
    DBDIR[database/]
    OSM[os_mirrors/]
    SOFT[soft_mirrors/]
    HPCFS --- DBDIR
    HPCFS --- OSM
    HPCFS --- SOFT
  end

  AI[AI 网关<br/>172.16.25.252:3030<br/>glm-5.2]

  FLASK -->|ssh 单向免密| WRAP
  FLASK -->|ssh| ANSI
  FLASK --> AI
  CRON --> WRAP
  WRAP --> HPCFS
  DU --> HPCFS
  WRAP -->|ssh + stdin| REPORT
```

**硬约束**

| 约束 | 含义 |
|------|------|
| 8.13 永不本机 `du` | `/hpcfs` 是 NFS，大目录会卡死 |
| 状态唯一来源 | `/api/report`；`save_to_db()` 不回写 `status` / `last_sync_date` |
| 链路 B 默认禁用 | `ENABLE_DB_CRON_DAEMON=0`，勿与 8.74 crontab 双开 |

---

## 2. 同步数据流（链路 A）

```mermaid
sequenceDiagram
  actor Admin
  participant Flask as Flask 8.13
  participant SSH as ssh
  participant Wrap as wrapper.py 8.74
  participant Disk as /hpcfs 本地盘
  participant Client as _report_client.py

  Admin->>Flask: POST /mirrors/{key}/sync
  Flask->>Flask: status=syncing · 防重入
  Flask->>SSH: ssh 8.74 wrapper.py script
  SSH->>Wrap: flock + 执行脚本
  Wrap->>Disk: rsync/lftp/bandersnatch
  Wrap->>Disk: du -sh
  Wrap->>Client: ssh 8.13 · stdin JSON
  Client->>Flask: POST /api/report + X-Reload-Token
  Flask->>Flask: UPDATE mirrors · sync_history · 刷内存
  Flask-->>Admin: 状态刷新（success/fail）
```

---

## 3. nginx 路由分发

```mermaid
flowchart LR
  REQ[HTTPS 请求] --> NGX{nginx}

  NGX -->|/= /index.html| S1[静态首页]
  NGX -->|/admin| P1[proxy Flask]
  NGX -->|/admin/| S2[alias 前端文件]
  NGX -->|/api/admin/agent/| P2[proxy · 超时 180s]
  NGX -->|/api/| P3[proxy · 超时 60s]
  NGX -->|/biocyc/| P4[proxy /browse 分页]
  NGX -->|/os_mirrors/ /soft_mirrors/| A1[autoindex /hpcfs]
  NGX -->|/db/…| A2[autoindex database]
  NGX -->|*.py *.log *.db /data/| DENY[404]
```

---

## 4. 组件分层

```mermaid
flowchart TB
  subgraph FE["用户端 / 管理端"]
    IDX[index.html / soft.html / doc.html]
    ADM[admin.html + admin/js + admin/css]
  end

  subgraph BE["Flask app.py"]
    AUTH[鉴权 CSRF LDAP]
    API[路由 API]
    SYNC[同步编排]
    AGENT[AI 巡检]
    DAEMON[4 Daemon 线程]
  end

  subgraph REMOTE["8.74"]
    W[wrapper.py]
    SCR[同步脚本]
    CT[crontab]
    AN[Ansible]
  end

  FE --> BE
  BE --> REMOTE
  REMOTE -->|/api/report| BE
```

### Flask Daemon

| 线程 | 间隔 | 默认 | 职责 |
|------|------|------|------|
| `size_scan_daemon` | 600s | 开 | DB → 内存 reload |
| `crontab_cache_daemon` | 300s | 开 | 缓存 8.74 crontab |
| `_agent_inspect_daemon` | 1800s | 开 | AI 巡检 / 自动重试 |
| `cron_sync_daemon` | 60s | **关** | DB cron 触发（勿开） |

---

## 5. 数据模型

```mermaid
erDiagram
  mirrors ||--o{ sync_history : key
  mirrors {
    TEXT key PK
    TEXT name
    TEXT url
    TEXT status
    TEXT total_size_gb
    TEXT sync_script
    INT is_system
  }
  sync_history {
    INT id PK
    TEXT key
    TEXT status
    TEXT trigger_type
    TEXT node
    INT duration_sec
  }
  audit_log {
    INT id PK
    TEXT action
    TEXT target
    TEXT operator
  }
  ansible_runs {
    TEXT run_id
    TEXT playbook
    TEXT status
    INT rc
  }
  settings {
    TEXT k PK
    TEXT v
  }
```

---

## 6. 信任边界

```mermaid
flowchart LR
  PUB[公网] -->|HTTPS 只读| NGX[nginx]
  OPS[管理员] -->|Cookie + CSRF| API[/api/admin/]
  NODE74[8.74] -->|X-Reload-Token| RPT[/api/report]
  FLASK[Flask] -->|ssh 免免密| NODE74
  FLASK -->|Bearer| AI[AI 网关]
```

| 边界 | 入口 | 鉴权 |
|------|------|------|
| 公网 → nginx | :443 | 无（只读） |
| 浏览器 → admin API | Cookie | session + CSRF |
| 8.74 → report | 本机 :5000 | `X-Reload-Token` |
| 8.13 → 8.74 | ssh | 单向免密 |
| Flask → AI | 内网 HTTP | Bearer Key |
