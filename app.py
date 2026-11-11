from flask import Flask, jsonify, request, send_from_directory, session, redirect, url_for, send_file, Response, stream_with_context
from datetime import datetime, timedelta
from functools import wraps
import threading
import uuid
import secrets
import os
import sys
import pwd
import grp
import socket
import stat
import subprocess
import time
import json
import re
import shlex
import sqlite3
import html
import fcntl
from urllib.parse import quote
from werkzeug.exceptions import NotFound

try:
    from ldap3 import Server, Connection, ALL, SUBTREE
    _HAS_LDAP3 = True
except ImportError:
    _HAS_LDAP3 = False

app = Flask(__name__)

BASE_MIRROR_PATH = '/hpcfs/fpublic/database'
BASE_SYSTEM_PATH = '/hpcfs/fpublic/mirrors'

# 远程同步执行机：admin 在本机(8.13)点同步 → ssh 到此机(8.74)跑 rsync → 数据落共享 /hpcfs。
# 两台都挂载同一共享存储，本机可直接读取展示。单向免密：8.13→8.74 已配通。
SYNC_EXEC_HOST = "root@172.16.8.74"
# 控制锁与 wrapper/ai_dispatch 统一（共享盘；旧 /var/lock 仅兼容探测）
_SCRIPT_DIR = "/hpcfs/fpublic/manage/baiwx/script"
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
try:
    from mirror_tiers import (  # noqa: E402
        LOCK_DIR as _MIRROR_LOCK_DIR,
        LOCK_DIR_LEGACY as _MIRROR_LOCK_LEGACY,
        MAX_HUGE as _MIRROR_MAX_HUGE,
        MAX_LARGE as _MIRROR_MAX_LARGE,
        MAX_PARALLEL as _MIRROR_MAX_PARALLEL,
        classify_tier as _shared_classify_tier,
    )
    from mirror_admission import (  # noqa: E402
        can_admit as _shared_can_admit,
        held_keys as _shared_held_keys,
        is_key_running as _shared_is_key_running,
    )
    _HAS_MIRROR_ADMISSION = True
except Exception as _adm_imp_err:
    _HAS_MIRROR_ADMISSION = False
    _MIRROR_LOCK_DIR = "/hpcfs/fpublic/manage/ansible/locks"
    _MIRROR_LOCK_LEGACY = "/var/lock/mirror-sync"
    _MIRROR_MAX_PARALLEL = 12
    _MIRROR_MAX_HUGE = 2
    _MIRROR_MAX_LARGE = 4
    print(f"⚠️  mirror_admission 导入失败，resume 回退本地逻辑: {_adm_imp_err}", flush=True)

SYNC_FLOCK_DIR = _MIRROR_LOCK_DIR
SYNC_FLOCK_DIR_LEGACY = _MIRROR_LOCK_LEGACY
SYNC_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")
SYNC_WRAPPER_PATH = "/hpcfs/fpublic/manage/baiwx/script/wrapper.py"
# Ansible 分发调度器：与 wrapper 接口一致，crontab 可切换执行方式
SYNC_AI_DISPATCH_PATH = "/hpcfs/fpublic/manage/baiwx/script/ai_dispatch.py"
ENABLE_DB_CRON_DAEMON = os.environ.get("ENABLE_DB_CRON_DAEMON", "0") == "1"

# Ansible 控制机（同 8.74）：/hpcfs/fpublic/manage/ansible/ 下管 32 主机 / 5 分组。
# admin 页选 playbook + 目标主机组 -> ssh 8.74 跑 ansible-playbook -> 异步记录 ansible_runs。
ANSIBLE_DIR = "/hpcfs/fpublic/manage/ansible"
# 分组 + auto(调度器自选单节点) + bnode1..bnode30
ALLOWED_ANSIBLE_LIMITS = (
    "all", "sync_nodes", "bnode", "web", "rhel9", "rhel8", "auto",
) + tuple(f"bnode{i}" for i in range(1, 31))
# 这些 limit 会命中多台主机；镜像同步(run_sync) 严禁使用（共享 NFS 会踩踏）
_ANSIBLE_MULTIHOST_LIMITS = frozenset({
    "all", "sync_nodes", "bnode", "web", "rhel9", "rhel8",
})
_SYNC_PLAYBOOKS = frozenset({"run_sync.yml", "run_sync.yaml"})
_ansible_runs = {}            # {run_id: {...}} 运行中状态查询
_ansible_runs_lock = threading.Lock()


def _is_allowed_ansible_limit(lim):
    lim = (lim or "").strip()
    return lim in ALLOWED_ANSIBLE_LIMITS


def _is_sync_playbook(playbook):
    return (playbook or "").strip().lower() in _SYNC_PLAYBOOKS


def _resolve_run_sync_limit(lim, sync_script="", key=""):
    """镜像同步只允许单节点，且一律交 ai_dispatch（带 flock + /api/report）。

    返回 (limit, error_or_None, redirect_to_dispatch:bool)
    - auto / 组名 → limit=auto，redirect=True
    - bnodeN → limit=bnodeN（供 --node），redirect=True（禁止裸 ansible-playbook）
    """
    lim = (lim or "auto").strip() or "auto"
    if lim in _ANSIBLE_MULTIHOST_LIMITS or lim == "auto":
        return "auto", None, True
    if re.fullmatch(r"bnode([1-9]|[12][0-9]|30)", lim):
        return lim, None, True
    return lim, f"run_sync 仅允许 auto 或单台 bnodeN，禁止 -l {lim}", False


def _launch_ai_dispatch(sync_script, preferred_node=None):
    """在 8.74 后台启动 ai_dispatch；preferred_node 为 bnodeN 时带 --node。

    返回 (ok:bool, detail:dict|str)
    """
    try:
        sync_script = _normalize_sync_script_arg(sync_script)
    except ValueError as ve:
        return False, str(ve)
    node_arg = ""
    if preferred_node and re.fullmatch(r"bnode([1-9]|[12][0-9]|30)", preferred_node):
        node_arg = f" --node {shlex.quote(preferred_node)}"
    r = _ansible_ssh(
        f"nohup /usr/bin/python3 {shlex.quote(SYNC_AI_DISPATCH_PATH)} "
        f"{shlex.quote(sync_script)}{node_arg} "
        f">>/hpcfs/fpublic/manage/baiwx/script/ai_dispatch.log 2>&1 &"
        f" echo DISPATCH_OK",
        timeout=20)
    if "DISPATCH_OK" not in (r.stdout or ""):
        return False, f"启动 ai_dispatch 失败: {(r.stderr or r.stdout or '')[:300]}"
    return True, {
        "ok": True,
        "routed": "ai_dispatch",
        "limit": preferred_node or "auto",
        "sync_script": sync_script,
        "message": (
            f"已交 ai_dispatch 指定节点 {preferred_node}"
            if preferred_node else "已交 ai_dispatch 按体积选单节点执行"
        ),
    }

def _log_sync(msg):
    """把同步过程日志写入 app.log，供 admin 同步日志页查看。"""
    try:
        with open(SYNC_LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception as e:
        print(f"⚠️  写同步日志失败：{e}", flush=True)

# 跟踪当前正在执行的同步任务：key -> {trigger_type, trigger_by, started_at}
# 由 trigger_sync_via_wrapper 写入；/api/report 回写时 _pop_sync_trigger 消费。
_sync_trigger = {}
_sync_trigger_lock = threading.Lock()


def _notify_async(fn, *args):
    """通知（钉钉/邮件/可选 AI 写文案）走后台线程，避免拖住 /api/report 回写。"""
    try:
        threading.Thread(
            target=fn, args=args, daemon=True, name="mirror-notify"
        ).start()
    except Exception as e:
        print(f"⚠️  启动通知线程失败：{e}", flush=True)


def _set_sync_trigger(key, trigger_type, trigger_by):
    with _sync_trigger_lock:
        _sync_trigger[key] = {"type": trigger_type, "by": trigger_by}


def _peek_sync_trigger(key):
    with _sync_trigger_lock:
        trig = _sync_trigger.get(key)
        return dict(trig) if trig else None


def _clear_sync_trigger(key):
    with _sync_trigger_lock:
        _sync_trigger.pop(key, None)


def _pop_sync_trigger(key, default_type, default_by):
    with _sync_trigger_lock:
        trig = _sync_trigger.pop(key, None) or {}
    return {
        "type": trig.get("type", default_type),
        "by": trig.get("by", default_by),
    }

def record_sync_history(key, status, started_at, output_summary="", node=None):
    """同步结束后写一条历史记录。finished_at/duration 在此处计算。
    node 标记执行节点（8.74 单机 / Ansible 分发的各执行节点），供多节点架构追溯。"""
    try:
        with db_conn() as conn:
            name = sync_data.get(key, {}).get("name", key)
            trig = _pop_sync_trigger(key, "manual", "admin")
            finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dur = None
            try:
                dur = int((datetime.now() - datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")).total_seconds())
            except Exception:
                pass
            conn.execute(
                """INSERT INTO sync_history
                   (key, name, status, trigger_type, trigger_by, started_at, finished_at, duration_sec, output_summary, node)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (key, name, status, trig["type"], trig["by"],
                 started_at, finished, dur, (output_summary or "")[:2000], node),
            )
            conn.commit()
    except Exception as e:
        print(f"⚠️  写同步历史失败：{e}")

def notify_sync_fail(key, detail=""):
    """同步失败 → AI 润色（可选）→ 钉钉/邮件告警。"""
    try:
        s = get_all_settings()
        if s.get("sync_fail_alert") != "1":
            return
        name = sync_data.get(key, {}).get("name", key)
        facts = (
            f"镜像 key：{key}\n镜像名：{name}\n事件：同步失败\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n详情：\n{(detail or '')[:800]}"
        )
        title, content = _notify_compose("alert", f"镜像同步失败：{name}", facts)
        _push_notify("alert", title, content)
    except Exception as e:
        print(f"⚠️  告警推送失败：{e}")

def notify_sync_success(key, detail=""):
    """同步成功 → AI 润色（可选）→ 钉钉/邮件成功通知。"""
    try:
        s = get_all_settings()
        if s.get("notify_sync_success") != "1":
            return
        name = sync_data.get(key, {}).get("name", key)
        size = sync_data.get(key, {}).get("total_size_gb", "")
        facts = (
            f"镜像 key：{key}\n镜像名：{name}\n事件：同步成功\n"
            f"大小：{size or '未知'}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"摘要：\n{(detail or '')[:500]}"
        )
        title, content = _notify_compose("success", f"镜像同步成功：{name}", facts)
        _push_notify("success", title, content)
    except Exception as e:
        print(f"⚠️  成功通知推送失败：{e}")

# 告警冷却去重：同一 title 在冷却窗口内只推一次，避免刷屏
_alert_last_sent = {}  # title -> monotonic 秒
_alert_lock = threading.Lock()

def _notify_compose(kind, title, facts):
    """按设置决定是否让 AI 写标题/正文；失败则回退原文。"""
    s = get_all_settings()
    if s.get("notify_ai_rewrite") != "1":
        return title, facts
    try:
        return _ai_write_notify(kind, title, facts)
    except Exception as e:
        print(f"⚠️  AI 写通知失败，用原文：{e}")
        return title, facts

# AI 文案校验黑名单：提示词里的占位符/格式说明词。模型偶尔会把格式说明
# 原样复述（如 "TITLE: <一行短标题>" 或 "TITLE: 上下文而不是直接输出"），
# 或先输出大段推理过程再给答案。这类输出必须识别出来并回退原始文案，
# 绝不能发去钉钉。
_AI_NOTIFY_BAD_MARKS = (
    "短标题", "正文多行", "一行短", "直接输出", "格式输出",
    "请严格", "不要其它前后缀", "不要其他前后缀", "上下文",
    "TITLE:", "BODY:",
)


def _ai_notify_text_ok(t, b):
    """校验 AI 生成的标题/正文是真实文案，而非占位符/格式复述/推理过程。
    t 传 None 表示只校验正文。"""
    if not b:
        return False
    if t is not None and not t:
        return False
    if t is not None and len(t) > 60:
        return False
    if len(b) > 800:
        return False
    # 提示词占位符形如 <一行短标题>；正常运维文案不含尖括号
    if (t and ("<" in t or ">" in t)) or "<" in b or ">" in b:
        return False
    for m in _AI_NOTIFY_BAD_MARKS:
        if (t and m in t) or m in b:
            return False
    return True


def _ai_write_notify(kind, title, facts):
    """调用技能网关写钉钉文案。返回 (title, body)。
    解析严格校验：输出不像正式文案时一律回退 (title, facts)。"""
    kind_cn = "告警/失败" if kind == "alert" else "成功/完成"
    system = (
        "你是 HPC 镜像站运维助手，负责写钉钉群通知文案。"
        "要求：简体中文；语气专业、清楚；2～6 行；不要 Markdown；不要堆表情；"
        "不要编造未提供的数据；保留镜像名、key、时间、原因等关键事实；"
        "告警类点出影响与建议下一步（一句即可）；成功类简短祝贺并带关键结果。"
        "只输出 TITLE 与 BODY 两段内容本身：不要复述格式说明，"
        "不要输出任何注释、解释或思考过程。"
    )
    user = (
        f"通知类型：{kind_cn}\n参考标题：{title}\n事实材料：\n{facts}\n\n"
        "请严格按下面格式输出（不要其它前后缀）：\n"
        "TITLE: <一行短标题>\n"
        "BODY:\n<正文多行>"
    )
    msg = _agent_openai_call(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        tools=None, max_tokens=350, timeout=20,
    )
    text = _strip_think((msg.get("content") if isinstance(msg, dict) else str(msg)) or "").strip()
    if not text:
        return title, facts
    new_title, body = title, facts
    # 模型可能先复述格式说明/推理过程，真正的答案取最后一个 TITLE: 之后的段落
    idx = text.rfind("TITLE:")
    if idx >= 0 and "BODY:" in text[idx:]:
        after_t = text[idx + len("TITLE:"):]
        tit_part, body_part = after_t.split("BODY:", 1)
        title_lines = tit_part.strip().splitlines()
        cand_title = title_lines[0].strip().strip("\"'“”`* ") if title_lines else ""
        cand_body = body_part.strip()
        if _ai_notify_text_ok(cand_title, cand_body):
            new_title, body = cand_title, cand_body
        else:
            print(f"⚠️  AI 文案未通过校验，回退原文：{text[:120]!r}")
    else:
        # 无 TITLE:/BODY: 结构：除非整体干净，否则视为模型自由发挥，回退原文，
        # 避免把推理过程/格式复述直接发出去
        if "\n" in text and _ai_notify_text_ok(None, text):
            body = text
        else:
            print(f"⚠️  AI 文案格式异常，回退原文：{text[:120]!r}")
    return new_title[:80], body[:1500]

def _push_alert(title, content):
    """兼容旧调用：按告警渠道推送。"""
    return _push_notify("alert", title, content)

def _push_notify(kind, title, content):
    """统一通知入口：读 settings，按渠道发钉钉 / 邮件。
    kind: alert|success。同一标题在 alert_cooldown 秒内只推一次。"""
    try:
        s = get_all_settings()
        try:
            cooldown = int(s.get("alert_cooldown", "300"))
        except ValueError:
            cooldown = 300
        # 成功通知冷却可单独更长，避免刷屏；仍复用同一配置，最少 60s
        if kind == "success":
            cooldown = max(cooldown, 60)
        now = time.monotonic()
        cooldown_key = f"{kind}:{title}"
        with _alert_lock:
            last = _alert_last_sent.get(cooldown_key)
            if last is not None and (now - last) < cooldown:
                return False
            _alert_last_sent[cooldown_key] = now
        sent = False
        if s.get("notify_dingtalk") == "1" and s.get("notify_dingtalk_webhook"):
            sent = _send_dingtalk(s, title, content, kind=kind) or sent
        if s.get("notify_email") == "1" and s.get("notify_email_addr") and s.get("smtp_server"):
            sent = _send_email(s, title, content, kind=kind) or sent
        return sent
    except Exception as e:
        print(f"⚠️  告警推送异常：{e}")
        return False

def _dingtalk_sign(secret):
    """钉钉机器人加签：timestamp + sign 拼到 webhook。"""
    import urllib.request, urllib.parse, hashlib, hmac, base64
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return f"&timestamp={timestamp}&sign={sign}"

def _dingtalk_body(s, title, content, kind="alert"):
    """拼出实际发往钉钉的 text 内容（标签 + 关键词前缀）。
    测试接口回显与实际发送都走这里，保证 admin 页看到的 = 群里收到的。"""
    kw = (s.get("notify_dingtalk_keyword") or "").strip()
    tag = "【镜像站告警】" if kind == "alert" else "【镜像站通知】"
    body = f"{tag}{title}\n{content}"
    # 自定义关键词安全：正文必须包含机器人里配置的关键词
    if kw and kw not in body:
        body = f"{kw}\n{body}"
    return body


def _send_dingtalk(s, title, content, kind="alert"):
    """钉钉机器人 webhook（text 类型），支持加签 secret、关键词与 @ 手机号。"""
    try:
        import urllib.request
        webhook = (s.get("notify_dingtalk_webhook") or "").strip()
        if not webhook:
            return False
        secret = s.get("notify_dingtalk_secret", "").strip()
        if secret:
            sep = "&" if "?" in webhook else "?"
            webhook = f"{webhook}{sep}{_dingtalk_sign(secret)[1:]}"  # 去掉开头的 &
        at_mobiles = [m.strip() for m in (s.get("notify_dingtalk_at", "") or "").split(",") if m.strip()]
        body = _dingtalk_body(s, title, content, kind=kind)
        payload = {"msgtype": "text", "text": {"content": body}}
        if at_mobiles:
            payload["at"] = {"atMobiles": at_mobiles, "isAtAll": False}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                j = json.loads(raw)
            except Exception:
                return resp.status == 200
            errcode = j.get("errcode", 0)
            if errcode == 0:
                return True
            print(f"⚠️  钉钉推送失败：errcode={errcode} errmsg={j.get('errmsg')}")
            return False
    except Exception as e:
        print(f"⚠️  钉钉推送失败：{e}")
        return False

def _send_email(s, title, content, kind="alert"):
    """SMTP 发送告警邮件。支持 SSL(465) / STARTTLS(587)。"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.utils import formataddr, formatdate
        server = s.get("smtp_server", "").strip()
        port = int(s.get("smtp_port", "465") or "465")
        user = s.get("smtp_user", "").strip()
        pwd = s.get("smtp_password", "").strip()
        sender_name = s.get("smtp_sender_name", "镜像站监控").strip() or "镜像站监控"
        to_addr = s.get("notify_email_addr", "").strip()
        use_ssl = port == 465
        tag = "【镜像站告警】" if kind == "alert" else "【镜像站通知】"
        msg = MIMEText(f"{tag}{title}\n\n{content}", "plain", "utf-8")
        msg["Subject"] = f"{tag}{title}"
        msg["From"] = formataddr((sender_name, user or to_addr))
        msg["To"] = to_addr
        msg["Date"] = formatdate(localtime=True)
        smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_cls(server, port, timeout=15) as smtp:
            if not use_ssl:
                try:
                    smtp.starttls()
                except Exception as e:
                    print(f"⚠️  SMTP STARTTLS 失败（可能不支持），继续明文发送：{e}", flush=True)
            if user and pwd:
                smtp.login(user, pwd)
            smtp.sendmail(user or to_addr, [a.strip() for a in to_addr.split(",") if a.strip()], msg.as_string())
        return True
    except Exception as e:
        print(f"⚠️  邮件推送失败：{e}")
        return False

# SQLite 数据库（镜像元数据唯一数据源）
# 放在项目目录下，避免 /var/tmp 被清理导致丢失
DB_FILE = "/hpcfs/fhome/baiwx/mirrors/data/mirrors.db"

# ====================== 管理端鉴权 ======================
# 密码与密钥必须从环境变量注入，不设则拒绝启动（杜绝弱默认值留在源码）
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
# secret_key 必须固定，否则每次重启 session 全失效
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
# session cookie 安全属性：仅 HTTPS 下发送(防中间人窃取)、Lax 防 CSRF 跨站带 Cookie、
# HttpOnly 防 JS 读取。配合 nginx 80->443 强制跳转与已设 HSTS，session 不走明文 HTTP。
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True,
)
# 内部刷新接口的共享令牌（8.74 扫描脚本调用 reload-sizes 时带此 header）
RELOAD_TOKEN = os.environ.get("RELOAD_TOKEN")
# session 有效期（秒）
ADMIN_SESSION_TTL = 90 * 60  # 会话有效期 90 分钟，超时需重新登录

# ===== AD 域认证配置 =====
# 域控地址（host:port 或 host），ldap3 默认 389；用 LDAPS 时设 AD_LDAP_USE_SSL=1
AD_LDAP_HOST = os.environ.get("AD_LDAP_HOST", "").strip()
AD_LDAP_PORT = int(os.environ.get("AD_LDAP_PORT", "0") or 0)
AD_LDAP_USE_SSL = os.environ.get("AD_LDAP_USE_SSL", "0") == "1"
# 用户搜索 base（如 dc=tibhpc,dc=net）和用户 DN 模板
AD_BASE_DN = os.environ.get("AD_BASE_DN", "").strip()
# 允许登录的组 DN（memberOf 必须含此 DN），如 CN=admin,CN=Users,dc=tibhpc,dc=net
AD_ADMIN_GROUP_DN = os.environ.get("AD_ADMIN_GROUP_DN", "").strip()

def _check_auth_env():
    """启动期校验关键密钥是否已注入，缺失则报错退出，避免带着弱默认值裸奔。"""
    missing = [k for k, v in (
        ("ADMIN_PASSWORD", ADMIN_PASSWORD),
        ("FLASK_SECRET_KEY", app.secret_key),
        ("RELOAD_TOKEN", RELOAD_TOKEN),
    ) if not v]
    if missing:
        raise RuntimeError(f"缺少必要环境变量: {', '.join(missing)}。请在 systemd unit 注入。")

# 旧的 persist 文件，已停用（仅保留备份兼容）
PERSIST_FILE = "/var/tmp/mirror_persist.json"

sync_data = {
    "pdb": {
        "name": "PDB",
        "description": "蛋白质三维结构数据库",
        "url": "pdb-redo/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": ["rsync", "-avz", "ftp.example.com::pdb/", "/hpcfs/fpublic/database/pdb-redo/"],
        "is_scanning": False
    },
    "uniprot": {
        "name": "UniProt",
        "description": "高质量蛋白质序列、功能注释与分类数据库",
        "url": "uniprot/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": ["rsync", "-avz", "rsync://rsync.uniprot.org/uniprot/", "/hpcfs/fpublic/database/uniprot/"],
        "is_scanning": False
    },
    "ncbi": {
        "name": "NCBI",
        "description": "美国国家生物技术信息中心，提供核酸、蛋白质、基因组等生物医学数据",
        "url": "ncbi/",
        "status": "pending",
        "total_size_gb": "获取中",
        # 展示用；真实执行走 sync_script=ncbi.py（lftp/FTP）。NCBI 已关 rsync daemon。
        "sync_cmd": ["lftp", "-c", "mirror ftp://ftp.ncbi.nlm.nih.gov/ /hpcfs/fpublic/database/ncbi/"],
        "is_scanning": False
    },
    "prosite": {
        "name": "Prosite",
        "description": "蛋白质家族与功能域数据库",
        "url": "prosite/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": ["rsync", "-avz", "rsync://ftp.expasy.org/databases/prosite/", "/hpcfs/fpublic/database/prosite/"],
        "is_scanning": False
    },
    "rhea": {
        "name": "Rhea",
        "description": "生物化学反应与代谢反应数据库",
        "url": "rhea/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": ["rsync", "-avz", "rsync://ftp.rhea-db.org/rhea/", "/hpcfs/fpublic/database/rhea/"],
        "is_scanning": False
    },
    "classify_predicted_pdbs": {
        "name": "AlphaFold2 Predicted DB",
        "description": "蛋白质结构预测数据库",
        "url": "classify_predicted_pdbs/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "antiSMASH": {
        "name": "antiSMASH-database",
        "description": "天然产物生物合成基因簇数据库",
        "url": "antiSMASH-database/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "Ensembl": {
        "name": "Ensembl-DB",
        "description": "涵盖植物、细菌、真菌、病毒等多物种基因组数据库",
        "url": "ensembl/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "insdc": {
        "name": "INSDC",
        "description": "全球权威的核苷酸序列数据归档与共享协作体",
        "url": "insdc/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "ddbj": {
        "name": "DDBJ",
        "description": "日本 DNA 数据库，收录全球研究者提交的原始测序、组装与注释序列",
        "url": "ddbj/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "PMC": {
        "name": "PMC",
        "description": "生物医学与生命科学期刊文献全文数据库",
        "url": "PMC/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "TARA": {
        "name": "TARA",
        "description": "海洋生物（病毒、细菌、古菌、真核生物）的基因组、宏基因组与转录组数据",
        "url": "TARA/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "PDB-NetXGen": {
        "name": "PDB-NetXGen",
        "description": "下一代蛋白质结构档案库，整合多源注释与分子连接信息",
        "url": "pdb_nextgen/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "EBI": {
        "name": "EBI",
        "description": "欧洲生物信息学研究所，提供核酸、蛋白质、结构、通路、小分子等核心生物数据资源与分析工具",
        "url": "ebi/",
        "status": "pending",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
   "GTDB": {
        "name": "GTDB",
        "description": "基于基因组的原核生物标准化分类数据库",
        "url": "GTDB/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
   "alphafill": {
        "name": "Alphafill",
        "description": "为 AlphaFold 蛋白结构补全配体、金属离子与辅因子的数据库",
        "url": "alphafill/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
   "kegg": {
        "name": "kegg",
        "description": "京都基因与基因组百科全书",
        "url": "kegg/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    },
    "biocyc": {
        "name": "BioCyc",
        "description": "基因组数据库集合，提供基因、蛋白质与代谢途径分析工具",
        "url": "biocyc/",
        "status": "success",
        "total_size_gb": "获取中",
        "sync_cmd": [],
        "is_scanning": False
    }
}

lock = threading.Lock()
is_scanning = False

# ====================== SQLite 加载/保存 ======================
def get_db_conn():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn

import contextlib

@contextlib.contextmanager
def db_conn():
    """SQLite 连接上下文管理器：保证异常路径也正确关闭，防泄漏。
    用法: with db_conn() as conn: conn.execute(...)"""
    conn = get_db_conn()
    try:
        yield conn
    finally:
        conn.close()

def load_from_db():
    """启动时从 SQLite 加载全部镜像条目，覆盖默认 sync_data。
    若 DB 不存在或为空，保留硬编码默认值，服务仍可启动。"""
    if not os.path.exists(DB_FILE):
        print(f"⚠️  数据库不存在：{DB_FILE}，使用硬编码默认数据")
        return
    try:
        with db_conn() as conn:
            rows = conn.execute("SELECT * FROM mirrors").fetchall()
        if not rows:
            print(f"⚠️  数据库为空：{DB_FILE}，使用硬编码默认数据")
            return
        # 用 DB 数据重建 sync_data
        sync_data.clear()
        for r in rows:
            try:
                cmd = json.loads(r["sync_cmd"]) if r["sync_cmd"] else []
            except Exception:
                cmd = []
            sync_data[r["key"]] = {
                "name": r["name"],
                "description": r["description"],
                "url": r["url"],
                "status": r["status"] or "success",
                "total_size_gb": r["total_size_gb"] or "获取中",
                "sync_cmd": cmd,
                "sync_script": (r["sync_script"] or "") if "sync_script" in r.keys() else "",
                "is_scanning": bool(r["is_scanning"]),
                "is_system": bool(r["is_system"]),
                "last_sync_date": r["last_sync_date"],
                "cron_expr": (r["cron_expr"] or "") if "cron_expr" in r.keys() else "",
                "cron_enabled": bool(r["cron_enabled"]) if "cron_enabled" in r.keys() else False,
            }
        print(f"✅ 成功从数据库加载 {len(sync_data)} 个条目：{DB_FILE}")
    except Exception as e:
        print(f"⚠️  加载数据库失败：{e}，使用硬编码默认数据")

def save_to_db():
    """把当前 sync_data 的大小/扫描标志写回 SQLite。

    注意：刻意不回写 status 与 last_sync_date。
    这两个字段的唯一权威来源是 /api/report（8.74 wrapper 跑完同步后回写）。
    本机(8.13)的内存虽会被 /api/report 刷新，但若同时存在第二个 Flask 实例
    （如 8.74 上误跑的 flask-mirror），其内存无人刷新、长期停留在旧 fail 状态；
    一旦它调用本函数全量 UPDATE，就会用陈旧内存把 /api/report 刚写入的正确
    status 覆盖回 fail，导致前端反复显示失败。故 status/last_sync_date 只由
    /api/report 单一来源写入，本函数仅持久化本地扫描产物 total_size_gb / is_scanning。
    """
    try:
        with db_conn() as conn:
            for k, v in sync_data.items():
                conn.execute("""
                    UPDATE mirrors SET
                        total_size_gb = ?,
                        is_scanning = ?,
                        updated_at = datetime('now','localtime')
                    WHERE key = ?
                """, (
                    v.get("total_size_gb"),
                    1 if v.get("is_scanning") else 0,
                    k,
                ))
            conn.commit()
    except Exception as e:
        print(f"⚠️  保存到数据库失败：{e}")

# 兼容旧调用名（扫描代码里仍调用 save_persisted_data）
save_persisted_data = save_to_db

# ====================== 管理端：完整 CRUD（直接读写 SQLite） ======================
ALLOWED_STATUS = ("success", "pending", "syncing", "fail", "timeout")
# sync_cmd 首元素白名单：仅同步工具。bash/python3/curl 已移除（解释器/下载器可任意执行或
# 落盘任意路径）。需要自定义脚本请用 sync_script（须位于 /hpcfs/fpublic/ 下，经 flock 跑）。
ALLOWED_SYNC_PROGRAMS = ("rsync", "lftp", "bandersnatch", "wget")
# sync_script 必须位于此前缀目录下，防止指向任意 .py 文件
ALLOWED_SYNC_SCRIPT_PREFIXES = ("/hpcfs/fpublic/",)

def _row_to_dict(r):
    """把 sqlite3.Row 转成管理端使用的字段字典。"""
    try:
        cmd = json.loads(r["sync_cmd"]) if r["sync_cmd"] else []
    except Exception:
        cmd = []
    return {
        "key": r["key"],
        "name": r["name"],
        "description": r["description"],
        "url": r["url"],
        "status": r["status"] or "success",
        "total_size_gb": r["total_size_gb"] or "获取中",
        "last_sync_date": r["last_sync_date"],
        "sync_cmd": cmd,
        "sync_script": (r["sync_script"] or "") if "sync_script" in r.keys() else "",
        "is_scanning": bool(r["is_scanning"]),
        "is_system": bool(r["is_system"]),
        "cron_expr": (r["cron_expr"] or "") if "cron_expr" in r.keys() else "",
        "cron_enabled": bool(r["cron_enabled"]) if "cron_enabled" in r.keys() else False,
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }

def _sync_mem_from_row(r):
    """把一行 DB 数据同步进内存 sync_data（用户端读内存，保证即时生效）。"""
    try:
        cmd = json.loads(r["sync_cmd"]) if r["sync_cmd"] else []
    except Exception:
        cmd = []
    with lock:
        sync_data[r["key"]] = {
            "name": r["name"],
            "description": r["description"],
            "url": r["url"],
            "status": r["status"] or "success",
            "total_size_gb": r["total_size_gb"] or "获取中",
            "sync_cmd": cmd,
            "sync_script": (r["sync_script"] or "") if "sync_script" in r.keys() else "",
            "is_scanning": bool(r["is_scanning"]),
            "is_system": bool(r["is_system"]),
            "last_sync_date": r["last_sync_date"],
            "cron_expr": (r["cron_expr"] or "") if "cron_expr" in r.keys() else "",
            "cron_enabled": bool(r["cron_enabled"]) if "cron_enabled" in r.keys() else False,
        }

def admin_list_all():
    """返回全部条目列表（管理端列表用）。"""
    with db_conn() as conn:
        rows = conn.execute("SELECT * FROM mirrors ORDER BY is_system, key").fetchall()
    return [_row_to_dict(r) for r in rows]

def admin_get_one(key):
    with db_conn() as conn:
        r = conn.execute("SELECT * FROM mirrors WHERE key = ?", (key,)).fetchone()
    return _row_to_dict(r) if r else None

def admin_validate(payload, key=None):
    """校验新增/编辑表单，返回 (clean_data, error_msg)。"""
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    k = (payload.get("key") or "").strip()
    if not name:
        return None, "名称不能为空"
    if not url:
        return None, "相对路径(url)不能为空"
    # url 必须以 / 结尾且不含 .. 防穿越
    if not url.endswith("/"):
        return None, "相对路径必须以 / 结尾"
    if ".." in url.split("/"):
        return None, "相对路径不允许包含 .."
    # key：新增时必填且只能小写字母/数字/下划线/横线
    if not k:
        return None, "标识 key 不能为空"
    if not all(c.isalnum() or c in "-_" for c in k):
        return None, "key 只能包含字母、数字、下划线、横线"
    status = (payload.get("status") or "success").strip()
    if status not in ALLOWED_STATUS:
        return None, f"status 必须是 {ALLOWED_STATUS} 之一"
    # sync_cmd：可以是 list 或 JSON 字符串
    cmd = payload.get("sync_cmd", [])
    if isinstance(cmd, str):
        try:
            cmd = json.loads(cmd) if cmd.strip() else []
        except Exception:
            return None, "sync_cmd 不是合法的 JSON"
    if not isinstance(cmd, list):
        return None, "sync_cmd 必须是数组"
    if not all(isinstance(x, str) for x in cmd):
        return None, "sync_cmd 数组元素必须为字符串"
    # sync_cmd 首元素必须在白名单内，防止塞入 sh/rm 等任意命令远程执行
    if cmd and cmd[0] not in ALLOWED_SYNC_PROGRAMS:
        return None, f"sync_cmd 首元素必须是 {ALLOWED_SYNC_PROGRAMS} 之一"
    # sync_script：远程同步脚本绝对路径（如 /hpcfs/.../ncbi.py），可空
    sync_script = (payload.get("sync_script") or "").strip()
    if sync_script and (".." in sync_script or not sync_script.startswith("/")):
        return None, "sync_script 必须是绝对路径且不含 .."
    if sync_script and not sync_script.startswith(ALLOWED_SYNC_SCRIPT_PREFIXES):
        return None, f"sync_script 必须位于 {ALLOWED_SYNC_SCRIPT_PREFIXES} 之下"
    # DB cron 字段仅保留作元数据兼容；实际调度以 8.74 crontab 为准。
    cron_expr = ""
    cron_enabled = 0
    return {
        "key": k,
        "name": name,
        "description": (payload.get("description") or "").strip(),
        "url": url,
        "status": status,
        "sync_cmd": cmd,
        "sync_script": sync_script,
        "is_system": 1 if payload.get("is_system") else 0,
        "cron_expr": cron_expr,
        "cron_enabled": cron_enabled,
    }, None

def admin_create(payload):
    clean, err = admin_validate(payload)
    if err:
        return None, err
    conn = get_db_conn()
    try:
        # 检查 key 重复
        if conn.execute("SELECT 1 FROM mirrors WHERE key = ?", (clean["key"],)).fetchone():
            return None, f"key '{clean['key']}' 已存在"
        conn.execute("""
            INSERT INTO mirrors (key, name, description, url, status, sync_cmd, sync_script, is_system, is_scanning, total_size_gb, cron_expr, cron_enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, '获取中', ?, ?)
        """, (
            clean["key"], clean["name"], clean["description"], clean["url"],
            clean["status"], json.dumps(clean["sync_cmd"], ensure_ascii=False),
            clean.get("sync_script", ""), clean["is_system"],
            clean.get("cron_expr", ""), clean.get("cron_enabled", 0),
        ))
        conn.commit()
        r = conn.execute("SELECT * FROM mirrors WHERE key = ?", (clean["key"],)).fetchone()
        _sync_mem_from_row(r)
        audit_log("create", target=clean["key"], detail={"name": clean["name"], "url": clean["url"]})
        return _row_to_dict(r), None
    except Exception as e:
        return None, f"新增失败：{e}"
    finally:
        conn.close()

def admin_update(key, payload):
    existing = admin_get_one(key)
    if not existing:
        return None, "条目不存在"
    clean, err = admin_validate(payload, key=key)
    if err:
        return None, err
    # 编辑时禁止改 key
    if clean["key"] != key:
        return None, "不允许修改 key"
    # status / last_sync_date 仅由同步报告回写，编辑表单不可改，避免覆盖真实状态
    clean["status"] = existing.get("status") or clean["status"]
    conn = get_db_conn()
    try:
        conn.execute("""
            UPDATE mirrors SET
                name = ?, description = ?, url = ?, status = ?,
                sync_cmd = ?, sync_script = ?, is_system = ?, cron_expr = ?, cron_enabled = ?, updated_at = datetime('now','localtime')
            WHERE key = ?
        """, (
            clean["name"], clean["description"], clean["url"], clean["status"],
            json.dumps(clean["sync_cmd"], ensure_ascii=False),
            clean.get("sync_script", ""), clean["is_system"],
            clean.get("cron_expr", ""), clean.get("cron_enabled", 0), key,
        ))
        conn.commit()
        r = conn.execute("SELECT * FROM mirrors WHERE key = ?", (key,)).fetchone()
        _sync_mem_from_row(r)
        audit_log("update", target=key, detail={"name": clean["name"], "status": clean["status"]})
        return _row_to_dict(r), None
    except Exception as e:
        return None, f"更新失败：{e}"
    finally:
        conn.close()

def admin_delete(key):
    existing = admin_get_one(key)
    if not existing:
        return False, "条目不存在"
    conn = get_db_conn()
    try:
        conn.execute("DELETE FROM mirrors WHERE key = ?", (key,))
        conn.commit()
        with lock:
            sync_data.pop(key, None)
        audit_log("delete", target=key, detail={"name": existing.get("name")})
        return True, None
    except Exception as e:
        return False, f"删除失败：{e}"
    finally:
        conn.close()

# ====================== 管理端鉴权 ======================
def _ad_authenticate(username, password):
    """用 AD 域校验账号密码，并确认用户属于 hpcadmin 组。
    返回 (ok: bool, error_or_userdn: str)。
    ok=True 时第二个值是用户 DN。

    组校验同时用两种方式，覆盖主组(primaryGroup)与附属组：
      A. memberOf 属性匹配（附属组）
      B. 查目标组的 member 列表是否含用户 DN（主组也命中）
    """
    if not _HAS_LDAP3:
        return False, "服务器未安装 ldap3 库，无法接入 AD"
    if not (AD_LDAP_HOST and AD_BASE_DN and AD_ADMIN_GROUP_DN):
        return False, "AD 域认证未配置"
    if not username or not password:
        return False, "账号或密码不能为空"

    # 防注入：用户名只允许字母数字 . _ -
    import re
    if not re.match(r'^[A-Za-z0-9._-]+$', username):
        return False, "账号格式非法"

    port = AD_LDAP_PORT or (636 if AD_LDAP_USE_SSL else 389)
    server = Server(AD_LDAP_HOST, port=port, use_ssl=AD_LDAP_USE_SSL,
                    connect_timeout=8, get_info=ALL)
    domain = AD_BASE_DN.replace("DC=", "").replace("dc=", "").replace(",", ".").strip()
    upn = f"{username}@{domain}"
    netbios = domain.split(".")[0].upper()

    # 依次尝试两种绑定串，任一成功即用该连接继续
    conn = None
    user_dn = None
    member_of = []
    primary_gid = None
    for bind_user in (upn, f"{netbios}\\{username}"):
        try:
            c = Connection(server, user=bind_user, password=password,
                           auto_bind=True, read_only=True, receive_timeout=8)
            if not c.bound:
                continue
            c.search(
                search_base=AD_BASE_DN,
                search_filter=f'(sAMAccountName={username})',
                search_scope=SUBTREE,
                attributes=['distinguishedName', 'memberOf', 'primaryGroupID'],
                size_limit=1,
            )
            if c.entries:
                conn = c
                user_dn = str(c.entries[0].distinguishedName)
                raw = c.entries[0].memberOf.value
                if isinstance(raw, list):
                    member_of = [str(x).lower() for x in raw]
                elif raw:
                    member_of = [str(raw).lower()]
                primary_gid = c.entries[0].primaryGroupID.value
                break
            c.unbind()
        except Exception:
            continue

    if not user_dn:
        return False, "账号或密码错误"

    grp_cfg = AD_ADMIN_GROUP_DN.strip()
    grp_lower = grp_cfg.lower()
    in_group = False

    # 方式 A：memberOf 匹配（完整 DN 精确匹配 / 纯组名匹配 CN 部分）
    if "=" in grp_lower:
        in_group = grp_lower in member_of
    else:
        in_group = any(
            g.split(",")[0].split("=", 1)[-1].strip() == grp_lower
            for g in member_of
        )

    # 方式 B：查目标组的 member 列表是否含用户 DN
    if not in_group and conn:
        try:
            grp_filter = (f'(distinguishedName={grp_cfg})'
                          if "=" in grp_cfg
                          else f'(&(objectClass=group)(cn={grp_cfg}))')
            conn.search(
                search_base=AD_BASE_DN,
                search_filter=grp_filter,
                search_scope=SUBTREE,
                attributes=['member', 'primaryGroupToken'],
                size_limit=1,
            )
            if conn.entries:
                raw = conn.entries[0].member.value
                members = raw if isinstance(raw, list) else ([raw] if raw else [])
                in_group = any(
                    str(m).lower() == user_dn.lower() for m in members
                )
                # 方式 C：主组比对——用户的 primaryGroupID == 组的 primaryGroupToken
                if not in_group:
                    grp_token = conn.entries[0].primaryGroupToken.value
                    if grp_token is not None and primary_gid is not None:
                        in_group = int(grp_token) == int(primary_gid)
        except Exception:
            pass

    try:
        conn.unbind()
    except Exception:
        pass

    if not in_group:
        return False, f"无权访问：仅 {grp_cfg} 组成员可登录"

    return True, user_dn

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"error": "未登录", "need_login": True}), 401
        # 登录态 TTL 校验，过期自动失效
        login_at = session.get("admin_login_at")
        if login_at and (time.time() - login_at) > ADMIN_SESSION_TTL:
            session.clear()
            return jsonify({"error": "登录已过期", "need_login": True}), 401
        return f(*args, **kwargs)
    return wrapper

# ====================== 登录限速 + CSRF 防护 ======================
_login_fails = {}               # {(ip, username): [失败时间戳]}
_login_fails_lock = threading.Lock()
LOGIN_FAIL_LIMIT = 5
LOGIN_FAIL_WINDOW = 300         # 5 分钟

def _login_fail_key(ip, user):
    return (ip or "unknown", (user or "").lower())

def _check_login_limited(ip, user):
    """是否已超限；返回 (limited, retry_after_sec)。"""
    key = _login_fail_key(ip, user)
    now = time.time()
    with _login_fails_lock:
        times = [t for t in _login_fails.get(key, []) if now - t < LOGIN_FAIL_WINDOW]
        if times:
            _login_fails[key] = times
        else:
            _login_fails.pop(key, None)   # 窗口外无失败记录，清 key 防 dict 无界增长
        if len(times) >= LOGIN_FAIL_LIMIT:
            return True, max(1, int(LOGIN_FAIL_WINDOW - (now - times[0])))
    return False, 0

def _record_login_fail(ip, user):
    key = _login_fail_key(ip, user)
    now = time.time()
    with _login_fails_lock:
        times = [t for t in _login_fails.get(key, []) if now - t < LOGIN_FAIL_WINDOW]
        times.append(now)
        _login_fails[key] = times

def _clear_login_fails(ip, user):
    with _login_fails_lock:
        _login_fails.pop(_login_fail_key(ip, user), None)

# CSRF：所有 /api/ 写操作（已登录会话）须带 X-CSRF-Token（=登录下发的 session csrf_token）
@app.before_request
def _csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    path = request.path
    if not path.startswith("/api/"):
        return None                 # 非 /api/ 路由不在保护范围
    if path == "/api/admin/login":
        return None                 # 登录前无 token
    if not session.get("admin_logged_in"):
        return None                 # 未登录/令牌上报路由(无 session)交由 @login_required 或令牌校验处理
    tok = request.headers.get("X-CSRF-Token", "")
    sess_tok = session.get("csrf_token", "")
    if not sess_tok or not secrets.compare_digest(tok, sess_tok):
        return jsonify({"error": "CSRF 校验失败，请刷新页面后重试"}), 403
    return None

# ====================== 扩展表：审计日志 + 设置 ======================
def init_extra_tables():
    """首次启动自动建表，不动现有 mirrors 表。"""
    conn = get_db_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            action     TEXT,
            target     TEXT,
            detail     TEXT,
            operator   TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS settings (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        CREATE TABLE IF NOT EXISTS sync_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            key            TEXT NOT NULL,
            name           TEXT,
            status         TEXT,
            trigger_type   TEXT,
            trigger_by     TEXT,
            started_at     TEXT,
            finished_at    TEXT,
            duration_sec   INTEGER,
            output_summary TEXT,
            node           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sync_history_key ON sync_history(key);
        CREATE INDEX IF NOT EXISTS idx_sync_history_started ON sync_history(started_at);
        CREATE TABLE IF NOT EXISTS ansible_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       TEXT,
            playbook     TEXT,
            lim          TEXT,
            status       TEXT,
            started_at   TEXT,
            finished_at  TEXT,
            rc           INTEGER,
            output       TEXT,
            triggered_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ansible_runs_started ON ansible_runs(started_at);
        CREATE TABLE IF NOT EXISTS agent_sessions (
            id         TEXT PRIMARY KEY,
            agent      TEXT,
            title      TEXT,
            user       TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS agent_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT,
            agent      TEXT,
            skills     TEXT,
            steps      INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_updated ON agent_sessions(updated_at);
        CREATE TABLE IF NOT EXISTS resume_queue (
            key         TEXT PRIMARY KEY,
            reason      TEXT,
            tier        TEXT,
            not_before  TEXT NOT NULL,
            attempts    INTEGER DEFAULT 0,
            last_error  TEXT,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            updated_at  TEXT DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_resume_queue_due ON resume_queue(not_before);
        CREATE TABLE IF NOT EXISTS daily_reports (
            report_date   TEXT PRIMARY KEY,
            title         TEXT,
            content       TEXT,
            facts_json    TEXT,
            status        TEXT,
            generated_at  TEXT,
            generated_by  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_daily_reports_gen ON daily_reports(generated_at);
        """)
        # 迁移：给旧表补 node 列（CREATE TABLE IF NOT EXISTS 不会改已存在表结构）
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sync_history)").fetchall()}
        if "node" not in cols:
            conn.execute("ALTER TABLE sync_history ADD COLUMN node TEXT")
        # resume_queue：若误建成 due_at，迁到 not_before
        rq_cols = {r["name"] for r in conn.execute("PRAGMA table_info(resume_queue)").fetchall()}
        if "due_at" in rq_cols and "not_before" not in rq_cols:
            conn.execute("ALTER TABLE resume_queue RENAME COLUMN due_at TO not_before")
        conn.commit()
    finally:
        conn.close()

def audit_log(action, target="", detail=None, operator=None):
    """记录一条管理操作审计日志。失败不影响主流程。
    operator 默认从 session 取当前登录用户(本地'admin'/域账号)，无请求上下文时回退 'admin'。"""
    if operator is None:
        try:
            operator = session.get("admin_user") or "admin"
        except Exception:
            operator = "admin"
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log (action, target, detail, operator) VALUES (?,?,?,?)",
                (action, target or "", json.dumps(detail, ensure_ascii=False) if detail is not None else "", operator),
            )
            conn.commit()
    except Exception as e:
        print(f"⚠️  审计日志写入失败：{e}")

# 默认设置项（首次启动初始化）
DEFAULT_SETTINGS = {
    # —— 告警阈值 ——
    "disk_alert_threshold": "85",      # 磁盘使用率告警阈值 %
    "cpu_alert_threshold": "90",       # CPU 使用率告警阈值 %
    "mem_alert_threshold": "90",       # 内存使用率告警阈值 %
    "sync_fail_alert": "1",            # 同步失败是否告警
    "notify_sync_success": "1",        # 同步成功是否推送通知
    "notify_ai_rewrite": "1",          # 通知文案是否由 AI 撰写
    "alert_cooldown": "300",           # 同一告警冷却时间（秒），避免刷屏
    # -- 上下游延迟分级（ms），前端 upstreams 页从 settings 读 --
    "upstream_latency_warn": "500",    # 延迟>=此值标记"慢"
    "upstream_latency_crit": "1500",   # 延迟>=此值标记"差"
    # —— 钉钉通知 ——
    "notify_dingtalk": "0",
    "notify_dingtalk_webhook": "",     # https://oapi.dingtalk.com/robot/send?access_token=...
    "notify_dingtalk_secret": "",      # 加签安全设置密钥（可选）
    "notify_dingtalk_keyword": "",     # 自定义关键词安全时必填，会自动拼进消息正文
    "notify_dingtalk_at": "",          # @ 的手机号，逗号分隔（可选）
    # —— 邮件通知（SMTP）——
    "notify_email": "0",
    "notify_email_addr": "",           # 收件地址，多个用逗号分隔
    "smtp_server": "",                 # SMTP 服务器，如 smtp.qq.com
    "smtp_port": "465",                # 端口：465(SSL) / 587(STARTTLS)
    "smtp_user": "",                   # 发件账号
    "smtp_password": "",               # 发件授权码
    "smtp_sender_name": "镜像站监控",  # 发件人显示名
}

def get_all_settings():
    """读取全部设置，缺项用默认值补齐。"""
    conn = get_db_conn()
    try:
        rows = conn.execute("SELECT k, v FROM settings").fetchall()
    finally:
        conn.close()
    merged = dict(DEFAULT_SETTINGS)
    for r in rows:
        merged[r["k"]] = r["v"]
    return merged

def set_setting(k, v):
    conn = get_db_conn()
    try:
        # 本机 Python sqlite3 链接 3.7.17，不支持 UPSERT(ON CONFLICT)，用 REPLACE 兼容
        conn.execute(
            "INSERT OR REPLACE INTO settings (k, v) VALUES (?, ?)",
            (k, str(v)),
        )
        conn.commit()
    finally:
        conn.close()

init_extra_tables()

# 启动时立即加载
load_from_db()


def _is_timeout_message(text):
    """判断输出是否为超时类（wrapper/ai_dispatch/旧触发超时）。"""
    t = (text or "")
    if not t:
        return False
    low = t.lower()
    return ("超时" in t) or ("timeout" in low)


def _is_resume_worthy_message(text):
    """超时 / 孤儿愈合 → 应自动续跑，而不是停在终态给用户看。"""
    t = text or ""
    if _is_timeout_message(t):
        return True
    if "orphan syncing healed" in t:
        return True
    return False


# 超时/中断后自动续跑：SQLite 落盘队列 + 与 wrapper 统一的 Admission 消峰。
AUTO_RESUME_ON_TIMEOUT = os.environ.get("AUTO_RESUME_ON_TIMEOUT", "1") == "1"
AUTO_RESUME_COOLDOWN_SEC = int(os.environ.get("AUTO_RESUME_COOLDOWN_SEC", "600"))
# 与 mirror_tiers / wrapper 默认对齐（可用同名或 MIRROR_MAX_* 覆盖）
AUTO_RESUME_MAX_PARALLEL = int(os.environ.get(
    "AUTO_RESUME_MAX_PARALLEL", str(_MIRROR_MAX_PARALLEL)))
AUTO_RESUME_MAX_HUGE = int(os.environ.get(
    "AUTO_RESUME_MAX_HUGE", str(_MIRROR_MAX_HUGE)))
AUTO_RESUME_MAX_LARGE = int(os.environ.get(
    "AUTO_RESUME_MAX_LARGE", str(_MIRROR_MAX_LARGE)))
# 同一 key 因 parallel-cap / still-running 推迟时的最小间隔（扫描退避，降日志噪声）
AUTO_RESUME_SCAN_BACKOFF_SEC = int(os.environ.get("AUTO_RESUME_SCAN_BACKOFF_SEC", "120"))
# 退避指数上限与告警阈值：attempts 过高说明闭环卡住，推一次告警后拉长等待
AUTO_RESUME_BACKOFF_MAX_SEC = int(os.environ.get("AUTO_RESUME_BACKOFF_MAX_SEC", "3600"))
AUTO_RESUME_ALERT_ATTEMPTS = int(os.environ.get("AUTO_RESUME_ALERT_ATTEMPTS", "12"))
# parallel-cap 排队超过此时长 → 闭环探针 degraded（默认 6h）
RESUME_STARVE_SEC = int(os.environ.get("RESUME_STARVE_SEC", str(6 * 3600)))
_resume_inflight = set()
_resume_last_at = {}       # key -> monotonic 上次实际触发
_resume_lock = threading.Lock()
_resume_log_last = {}      # key -> (reason, monotonic) 降噪
_resume_alerted = set()    # 已告警过的卡住 key，成功后清除
_daemon_beats = {}         # name -> epoch 心跳，供 healthz / 闭环探针
_daemon_beats_lock = threading.Lock()
_loop_last_report_at = 0.0 # 最近一次 /api/report 成功 epoch
_loop_last_report_key = ""


def _local_sync_lock_state(key):
    """探测控制锁（共享盘 + legacy）。Flask 与 wrapper 同机时优先本地探测。"""
    if _HAS_MIRROR_ADMISSION:
        running, detail = _shared_is_key_running(key)
        if running:
            return "running", detail
        return "idle", detail
    lockfile = f"{SYNC_FLOCK_DIR}/{key}.lock"
    if not os.path.isdir(SYNC_FLOCK_DIR):
        return "unknown", "无本地锁目录"
    try:
        fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return "running", "本地锁被占用"
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            return "idle", "本地锁空闲"
        finally:
            os.close(fd)
    except Exception as e:
        return "unknown", f"本地锁探测异常: {e}"


def _held_lock_keys():
    """当前持锁的镜像 key 集合。"""
    if _HAS_MIRROR_ADMISSION:
        try:
            return set(_shared_held_keys())
        except Exception:
            pass
    held = set()
    try:
        if not os.path.isdir(SYNC_FLOCK_DIR):
            return held
        for name in os.listdir(SYNC_FLOCK_DIR):
            if not name.endswith(".lock") or ".script." in name:
                continue
            key = name[:-5]
            st, _ = _local_sync_lock_state(key)
            if st == "running":
                held.add(key)
    except Exception:
        pass
    return held


def _resume_mirror_tier(key):
    """按体积分档：huge / large / medium / small（与 mirror_tiers 对齐）。"""
    key = (key or "").strip()
    with lock:
        size = (sync_data.get(key) or {}).get("total_size_gb") or ""
    if _HAS_MIRROR_ADMISSION:
        tier, _, _ = _shared_classify_tier(key, size)
        return tier
    gb = _parse_size_to_gb_simple(size)
    if gb is None:
        return "medium"
    if gb >= 5 * 1024:
        return "huge"
    if gb >= 500:
        return "large"
    if gb >= 30:
        return "medium"
    return "small"


def _parse_size_to_gb_simple(size_str):
    s = (size_str or "").strip().upper().replace(",", "")
    if not s or s in ("-", "未知", "N/A", "获取中"):
        return None
    m = re.match(r"^([\d.]+)\s*([KMGT])", s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    return n * {"K": 1 / (1024 ** 2), "M": 1 / 1024, "G": 1, "T": 1024}.get(m.group(2), 1)


def _tier_syncing_counts():
    """当前 syncing（含 inflight + 持锁）按档计数。"""
    counts = {"huge": 0, "large": 0, "medium": 0, "small": 0}
    with lock:
        keys = [k for k, v in sync_data.items() if v.get("status") == "syncing"]
    with _resume_lock:
        keys = list(set(keys) | set(_resume_inflight))
    for k in _held_lock_keys():
        if k not in keys:
            keys.append(k)
    for k in keys:
        t = _resume_mirror_tier(k)
        counts[t] = counts.get(t, 0) + 1
    return counts


def _can_start_resume_tier(tier, key=""):
    """分档消峰：与 wrapper/ai_dispatch 共用 Admission 计数。"""
    if _HAS_MIRROR_ADMISSION and key:
        with lock:
            size = (sync_data.get(key) or {}).get("total_size_gb") or ""

        def _size_lookup(k):
            with lock:
                return (sync_data.get(k) or {}).get("total_size_gb") or ""

        ok, reason, _, _ = _shared_can_admit(
            key, size_str=size, size_lookup=_size_lookup
        )
        if not ok:
            return False, reason
        # 另计 resume_inflight（尚未持锁但已触发）
        with _resume_lock:
            inflight = set(_resume_inflight)
        inflight.discard(key)
        if inflight:
            c = _tier_syncing_counts()
            total = sum(c.values())
            if total >= AUTO_RESUME_MAX_PARALLEL:
                return False, f"总并发已满({total}/{AUTO_RESUME_MAX_PARALLEL})"
        return True, ""
    c = _tier_syncing_counts()
    total = sum(c.values())
    if total >= AUTO_RESUME_MAX_PARALLEL:
        return False, f"总并发已满({total}/{AUTO_RESUME_MAX_PARALLEL})"
    if tier == "huge" and c["huge"] >= AUTO_RESUME_MAX_HUGE:
        return False, f"巨库并发已满({c['huge']}/{AUTO_RESUME_MAX_HUGE})"
    if tier == "large" and (c["huge"] + c["large"]) >= (AUTO_RESUME_MAX_HUGE + AUTO_RESUME_MAX_LARGE):
        return False, f"大库档位已满(huge+large={c['huge']+c['large']})"
    return True, ""


def _daemon_beat(name):
    """后台线程心跳：healthz / 闭环探针用来判断 daemon 是否还活着。"""
    with _daemon_beats_lock:
        _daemon_beats[name] = time.time()


def _resume_log(key, reason_or_msg, detail=""):
    """同类 reason 3 分钟内只打一条（按 reason 去重，不含变动的 not_before）。"""
    now = time.monotonic()
    tag = (reason_or_msg or "")[:80]
    with _resume_lock:
        prev = _resume_log_last.get(key)
        if prev and prev[0] == tag and (now - prev[1]) < 180:
            return
        _resume_log_last[key] = (tag, now)
    extra = f" {detail}" if detail else ""
    _log_sync(f"[{key}] {reason_or_msg}{extra}")


# 退避类 reason：必须把 not_before 推后，避免一直到期被 daemon 反复 pop
_RESUME_BACKOFF_REASONS = frozenset({
    "parallel-cap", "still-running", "scan-incomplete", "wait-report",
    "cooldown", "lock-unknown", "trigger-fail", "exception",
    "report-timeout", "orphan-heal", "admission-skip",
})


def _resume_queue_keys():
    """当前 resume_queue 中的全部 key（扫描侧用来避免重复入队）。"""
    try:
        with db_conn() as conn:
            rows = conn.execute("SELECT key FROM resume_queue").fetchall()
        return {r["key"] for r in rows}
    except Exception:
        return set()


def _resume_attempts(key):
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT attempts FROM resume_queue WHERE key=?", (key,)
            ).fetchone()
        return int(row["attempts"] or 0) if row else 0
    except Exception:
        return 0


def _bump_resume_attempts(key, n=1):
    """排队空转（parallel-cap 等）也累加 attempts，驱动退避与卡住告警。"""
    key = (key or "").strip()
    if not key:
        return 0
    try:
        with db_conn() as conn:
            conn.execute(
                """UPDATE resume_queue SET attempts=COALESCE(attempts,0)+?,
                   updated_at=datetime('now','localtime') WHERE key=?""",
                (int(n), key),
            )
            conn.commit()
        return _resume_attempts(key)
    except Exception:
        return _resume_attempts(key)


def _resume_backoff_delay(base_sec, attempts=0):
    """指数退避：base * 2^min(attempts,5)，封顶 AUTO_RESUME_BACKOFF_MAX_SEC。"""
    base = max(int(base_sec), AUTO_RESUME_SCAN_BACKOFF_SEC)
    exp = min(max(int(attempts), 0), 5)
    return min(AUTO_RESUME_BACKOFF_MAX_SEC, base * (2 ** exp))


def _schedule_auto_resume(key, delay_sec=45, reason=""):
    """写入 SQLite resume_queue（落盘）。
    - 退避类 reason：推后 not_before（消峰 / 降噪）
    - 其它紧急入队：保留更早的 not_before
    - 已在队列且不需推迟时：静默刷新，避免 scan 刷屏打断闭环
    """
    if not AUTO_RESUME_ON_TIMEOUT:
        return
    key = (key or "").strip()
    if not key or key not in sync_data:
        return
    with lock:
        script = (sync_data.get(key) or {}).get("sync_script") or ""
    if not script:
        return
    delay_sec = max(5, int(delay_sec))
    reason = (reason or "timeout/incomplete")[:120]
    backoff = reason in _RESUME_BACKOFF_REASONS or reason.startswith("heal-")
    # 扫描退避：parallel-cap / still-running 等至少间隔 SCAN_BACKOFF；
    # 再按 attempts 指数拉长。parallel-cap / admission-skip 只做短固定退避。
    if reason in ("parallel-cap", "admission-skip"):
        delay_sec = max(int(delay_sec), AUTO_RESUME_SCAN_BACKOFF_SEC)
        delay_sec = min(delay_sec, 180)
    elif backoff:
        delay_sec = _resume_backoff_delay(delay_sec, _resume_attempts(key))
    due = datetime.now() + timedelta(seconds=delay_sec)
    due_s = due.strftime("%Y-%m-%d %H:%M:%S")
    tier = _resume_mirror_tier(key)
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT not_before FROM resume_queue WHERE key=?", (key,)
            ).fetchone()
            existing = (row["not_before"] if row else None) or ""
            changed_due = False
            inserted = False
            if existing and not backoff and existing <= due_s:
                # 紧急入队且已有更早到期：只刷新 reason/tier
                conn.execute(
                    """UPDATE resume_queue SET reason=?, tier=?, updated_at=datetime('now','localtime')
                       WHERE key=?""",
                    (reason, tier, key),
                )
                due_s = existing
            elif existing and backoff and existing >= due_s:
                # 退避但已有更晚到期：不提前，只刷新 reason/tier（静默）
                conn.execute(
                    """UPDATE resume_queue SET reason=?, tier=?, updated_at=datetime('now','localtime')
                       WHERE key=?""",
                    (reason, tier, key),
                )
                due_s = existing
            else:
                # 本机 Python sqlite3 链接 3.7.17，不支持 UPSERT(ON CONFLICT)：
                # 先 UPDATE 保留 attempts/created_at，无行时再 INSERT。
                cur = conn.execute(
                    """UPDATE resume_queue
                       SET reason=?, tier=?, not_before=?, updated_at=datetime('now','localtime')
                       WHERE key=?""",
                    (reason, tier, due_s, key),
                )
                if cur.rowcount == 0:
                    conn.execute(
                        """INSERT INTO resume_queue(key, reason, tier, not_before, attempts, updated_at)
                           VALUES(?,?,?,?,0,datetime('now','localtime'))""",
                        (key, reason, tier, due_s),
                    )
                    inserted = True
                else:
                    changed_due = True
            conn.commit()
        # 仅新入队或真正推迟 not_before 时打日志；静默刷新不刷屏
        if inserted or changed_due:
            _resume_log(
                key,
                f"续跑入队（{reason}）",
                f"{delay_sec}s / {tier} / not_before={due_s}",
            )
    except Exception as e:
        print(f"⚠️  resume_queue 写入失败 [{key}]：{e}", flush=True)


def _resume_queue_done(key, drop=True):
    """任务结束：清 inflight；成功则删队列项并清除卡住告警标记。"""
    key = (key or "").strip()
    with _resume_lock:
        _resume_inflight.discard(key)
        if drop:
            _resume_alerted.discard(key)
    if not drop:
        return
    try:
        with db_conn() as conn:
            conn.execute("DELETE FROM resume_queue WHERE key=?", (key,))
            conn.commit()
    except Exception:
        pass


def _pop_due_resume_keys(limit=8):
    """取出已到期的续跑任务：小库优先，巨库靠后（消峰）。"""
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with db_conn() as conn:
            rows = conn.execute(
                """SELECT key, reason, tier, not_before FROM resume_queue
                   WHERE not_before <= ?
                   ORDER BY CASE tier
                     WHEN 'small' THEN 0 WHEN 'medium' THEN 1
                     WHEN 'large' THEN 2 WHEN 'huge' THEN 3 ELSE 4 END,
                     not_before ASC
                   LIMIT ?""",
                (now_s, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"⚠️  resume_queue 读取失败：{e}", flush=True)
        return []


def _persist_mirror_status(key, status):
    """把运行态 status 落到 mirrors 表（save_to_db 刻意不写 status，触发/对齐路径须自写）。"""
    key = (key or "").strip()
    if not key or status not in ALLOWED_STATUS:
        return
    try:
        with db_conn() as conn:
            conn.execute(
                """UPDATE mirrors SET status=?, updated_at=datetime('now','localtime')
                   WHERE key=?""",
                (status, key),
            )
            conn.commit()
    except Exception as e:
        print(f"⚠️  落库 status 失败 [{key}]：{e}", flush=True)


def _align_status_if_remote_running(key):
    """若远端 flock 仍占用，把 fail/pending/timeout 纠正为 syncing（监控态，非失败）。

    内存已是 syncing 时仍校验 DB：trigger 路径曾只改内存不落库，会造成 API=syncing /
    DB=pending 漂移；重启后 reload 会把错误 pending 盖回内存。
    """
    state, detail = _remote_sync_lock_state(key)
    if state != "running":
        return state, detail
    with lock:
        cur = (sync_data.get(key) or {}).get("status")
        if cur != "syncing":
            sync_data[key]["status"] = "syncing"
            mem_changed = True
        else:
            mem_changed = False
    db_status = None
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT status FROM mirrors WHERE key=?", (key,)).fetchone()
            db_status = (row["status"] if row else None) or ""
    except Exception:
        db_status = ""
    if mem_changed or db_status != "syncing":
        _persist_mirror_status(key, "syncing")
        invalidate_status_cache()
        if mem_changed:
            _resume_log(key, f"监控：远端仍在跑（{detail}），状态 {cur} → syncing")
        elif db_status and db_status != "syncing":
            _resume_log(key, f"监控：补齐 DB status {db_status} → syncing（{detail}）")
    return state, detail


def _maybe_alert_stuck_resume(key, attempts, detail=""):
    """续跑 attempts 过高：闭环卡住，推一次失败告警（同 key 成功前不重复）。"""
    if attempts < AUTO_RESUME_ALERT_ATTEMPTS:
        return
    with _resume_lock:
        if key in _resume_alerted:
            return
        _resume_alerted.add(key)
    _resume_log(key, f"续跑卡住告警（attempts={attempts}）", detail)
    _notify_async(
        notify_sync_fail,
        key,
        f"自动续跑排队已重试 {attempts} 次仍未完成（{detail or '见 resume_queue'}）",
    )


def _try_auto_resume(key, reason=""):
    """监控远端锁：仍在跑 → 保持 syncing；空闲且档位有空 → wrapper 续跑。"""
    key = (key or "").strip()
    if not key or key not in sync_data:
        _resume_queue_done(key)
        return
    with lock:
        item = sync_data.get(key) or {}
        st = item.get("status")
        script = (item.get("sync_script") or "").strip()
    if not script:
        _resume_queue_done(key)
        return
    if st == "success":
        _resume_queue_done(key)
        return

    attempts = _resume_attempts(key)
    state, detail = _align_status_if_remote_running(key)
    if state == "running":
        # 远端仍在跑：涨 attempts 拉长退避，避免每 3 分钟空转刷屏
        attempts = _bump_resume_attempts(key)
        _resume_queue_done(key, drop=False)
        _resume_log(key, f"续跑暂缓：仍在跑（{detail}）", f"attempts={attempts}")
        _maybe_alert_stuck_resume(key, attempts, detail)
        _schedule_auto_resume(key, delay_sec=180, reason="still-running")
        return
    if state == "unknown":
        attempts = _bump_resume_attempts(key)
        _schedule_auto_resume(key, delay_sec=90, reason="lock-unknown")
        return

    now = time.monotonic()
    with _resume_lock:
        last = _resume_last_at.get(key, 0)
        if now - last < AUTO_RESUME_COOLDOWN_SEC:
            remain = int(AUTO_RESUME_COOLDOWN_SEC - (now - last))
            _schedule_auto_resume(key, delay_sec=remain + 5, reason="cooldown")
            return

    tier = _resume_mirror_tier(key)
    ok_tier, tier_msg = _can_start_resume_tier(tier, key=key)
    if not ok_tier:
        # 纯容量等待不涨 attempts（避免 2^n 退避把队列拖死）；固定短间隔重试
        attempts = _resume_attempts(key)
        _resume_log(key, f"续跑暂缓：{tier_msg}", f"attempts={attempts}")
        _maybe_alert_stuck_resume(key, attempts, tier_msg)
        _schedule_auto_resume(key, delay_sec=60, reason="parallel-cap")
        return

    with lock:
        st = (sync_data.get(key) or {}).get("status")
    if st == "syncing":
        # 锁已确认 idle：回写丢失 / flock-skip → 快愈为 pending，本轮继续触发
        db_st = ""
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT status FROM mirrors WHERE key=?", (key,)
                ).fetchone()
            db_st = ((row["status"] if row else "") or "").strip().lower()
        except Exception:
            db_st = ""
        if db_st in ("success", "fail", "pending"):
            with lock:
                if key in sync_data:
                    sync_data[key]["status"] = db_st
            invalidate_status_cache()
            _resume_log(key, f"idle+syncing：DB 已是 {db_st}，对齐内存")
            if db_st == "success":
                _resume_queue_done(key)
                return
            if db_st == "fail":
                _resume_queue_done(key)
                return
            # pending：继续向下触发
        else:
            with lock:
                if key in sync_data:
                    sync_data[key]["status"] = "pending"
            _persist_mirror_status(key, "pending")
            invalidate_status_cache()
            _resume_log(key, "idle+syncing → pending（快愈后续跑）")


    with _resume_lock:
        _resume_last_at[key] = time.monotonic()
        _resume_inflight.add(key)
    try:
        with db_conn() as conn:
            conn.execute(
                """UPDATE resume_queue SET attempts=attempts+1,
                   updated_at=datetime('now','localtime') WHERE key=?""",
                (key,),
            )
            conn.commit()
    except Exception:
        pass
    _resume_log(key, f"监控：远端空闲，自动续跑 → wrapper（{tier}）")
    ret = trigger_sync_via_wrapper(key, "schedule", "auto-resume", executor="wrapper")
    if not ret.get("ok"):
        with _resume_lock:
            _resume_inflight.discard(key)
        err = ret.get("error") or "trigger-fail"
        try:
            with db_conn() as conn:
                conn.execute(
                    """UPDATE resume_queue SET last_error=?, updated_at=datetime('now','localtime')
                       WHERE key=?""",
                    (str(err)[:500], key),
                )
                conn.commit()
        except Exception:
            pass
        _maybe_alert_stuck_resume(key, attempts + 1, str(err)[:200])
        _schedule_auto_resume(key, delay_sec=180, reason="trigger-fail")


def _auto_resume_daemon():
    """后台：消费 SQLite resume_queue；周期对齐锁态 + 轻量扫描。"""
    _daemon_beat("auto_resume")
    time.sleep(45)
    while True:
        _daemon_beat("auto_resume")
        try:
            try:
                _reconcile_running_locks()
            except Exception as e:
                print(f"⚠️  锁态对齐失败：{e}", flush=True)
            due = _pop_due_resume_keys(limit=8)
            for row in due:
                key = row.get("key")
                try:
                    _try_auto_resume(key, reason=row.get("reason") or "")
                except Exception as e:
                    _log_sync(f"[{key}] 自动续跑异常：{e}")
                    _schedule_auto_resume(key, delay_sec=180, reason="exception")
            try:
                _scan_incomplete_for_resume()
            except Exception as e:
                print(f"⚠️  续跑扫描失败：{e}", flush=True)
        except Exception as e:
            print(f"⚠️  auto_resume_daemon：{e}", flush=True)
        time.sleep(45)


def _reconcile_running_locks():
    """对齐运行态：锁占用 → syncing。

    1) 非 success 条目：原逻辑，纠正 fail/pending 误报
    2) 本机持锁但 DB 仍是 success：cron 直拉尚未回写，必须刷成 syncing 才能计入档位
    """
    with lock:
        keys = [
            k for k, v in sync_data.items()
            if v.get("status") in ("fail", "pending", "timeout", "syncing")
            and (v.get("sync_script") or "").strip()
        ]
    for key in keys:
        try:
            _align_status_if_remote_running(key)
        except Exception:
            continue
    # cron 刚拉起：status 仍是上次 success，靠本地 flock 补齐
    for key in _held_lock_keys():
        try:
            with lock:
                st = (sync_data.get(key) or {}).get("status")
            if st == "success" or st == "none" or not st:
                _align_status_if_remote_running(key)
        except Exception:
            continue


def _normalize_report_statuses(raw_status, output=""):
    """上报归一：超时不是给用户看的终态。
    返回 (mirror_status, history_status)。"""
    st = (raw_status or "").strip().lower()
    if st == "timeout" or (st == "fail" and _is_timeout_message(output)):
        return "pending", "timeout"
    return st, st


def _recover_orphaned_syncing():
    # 启动/周期恢复：仅将确认未运行的"孤儿 syncing"重置。
    # 若 8.74 上同镜像的 flock 仍被持有，说明同步还在跑，保留 syncing 等待后续回写；
    # 若 SSH/远端探测失败，则维持现状，避免重启时误伤真实运行任务。
    #
    # 重要：孤儿 ≠ 真实同步失败。锁空闲但无 /api/report 时，更可能是回写丢失或任务被
    # 外部杀掉；标成 fail 会让首页刷「同步失败」。改为 pending，并排队自动续跑。
    #
    # 注意：必须直接 UPDATE mirrors.status。save_to_db/save_persisted_data 刻意不回写
    # status（权威源是 /api/report）；若只改内存，reload_status_from_db 会把 DB 里残留的
    # syncing 再刷回来，导致前端长期卡在「同步中」且无法再次触发。
    with lock:
        stuck = [k for k, v in sync_data.items() if v.get("status") == "syncing"]
    orphaned = []
    for key in stuck:
        state, detail = _remote_sync_lock_state(key)
        if state == "running":
            _log_sync(f"[{key}] 孤儿检测：保留 syncing（{detail}）")
            continue
        if state == "idle":
            # DB 若已是终态（report 已提交但内存未刷），只对齐内存，禁止降成 pending 再续跑
            db_st = ""
            try:
                with db_conn() as conn:
                    row = conn.execute(
                        "SELECT status FROM mirrors WHERE key=?", (key,)
                    ).fetchone()
                db_st = ((row["status"] if row else "") or "").strip().lower()
            except Exception:
                db_st = ""
            if db_st in ("success", "fail", "pending"):
                with lock:
                    if key in sync_data:
                        sync_data[key]["status"] = db_st
                invalidate_status_cache()
                _log_sync(f"[{key}] 孤儿检测：DB 已是 {db_st}，仅对齐内存，不降级续跑")
                continue
            if db_st == "timeout":
                with lock:
                    if key in sync_data:
                        sync_data[key]["status"] = "pending"
                orphaned.append(key)
                _log_sync(f"[{key}] 孤儿检测：DB timeout → pending，排队续跑")
                continue
            with lock:
                if sync_data.get(key, {}).get("status") == "syncing":
                    sync_data[key]["status"] = "pending"
                    orphaned.append(key)
            _log_sync(f"[{key}] 孤儿检测：确认无远端锁，孤儿 syncing -> pending，排队续跑")
            continue
        _log_sync(f"[{key}] 孤儿检测：无法确认是否仍在运行，保留 syncing（{detail}）")
    if orphaned:
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with db_conn() as conn:
                for key in orphaned:
                    conn.execute(
                        """UPDATE mirrors SET status=?, updated_at=datetime('now','localtime')
                           WHERE key=?""",
                        ("pending", key),
                    )
                    name = sync_data.get(key, {}).get("name", key)
                    conn.execute(
                        """INSERT INTO sync_history
                           (key, name, status, trigger_type, trigger_by, started_at, finished_at,
                            duration_sec, output_summary, node)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (key, name, "pending", "heal", "orphan-detect", now_str, now_str, 0,
                         "orphan syncing healed -> pending (auto-resume queued)",
                         "heal-local"),
                    )
                conn.commit()
        except Exception as e:
            _log_sync(f"孤儿 syncing 落库失败：{e}")
        invalidate_status_cache()
        for key in orphaned:
            _schedule_auto_resume(key, delay_sec=60, reason="orphan-heal")


def _heal_incomplete_statuses():
    """把 timeout /「超时类 fail」纠成 pending（或远端在跑则 syncing），并排队自动续跑。"""
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT key, status FROM mirrors WHERE status IN ('fail','timeout')"
            ).fetchall()
        changed = []
        for r in rows:
            key = r["key"]
            cur = r["status"]
            # 远端仍在跑：直接对齐为 syncing，不要改成 pending 再触发
            try:
                state, _ = _align_status_if_remote_running(key)
                if state == "running":
                    changed.append(f"{key}:syncing")
                    continue
            except Exception:
                pass
            with db_conn() as conn:
                hist = conn.execute(
                    "SELECT status, output_summary FROM sync_history WHERE key=? "
                    "ORDER BY id DESC LIMIT 1",
                    (key,),
                ).fetchone()
            out = (hist["output_summary"] if hist else "") or ""
            hist_st = (hist["status"] if hist else "") or ""
            should = (
                cur == "timeout"
                or hist_st == "timeout"
                or _is_resume_worthy_message(out)
            )
            if not should:
                continue
            with lock:
                if key in sync_data:
                    sync_data[key]["status"] = "pending"
            with db_conn() as conn:
                conn.execute(
                    """UPDATE mirrors SET status=?, updated_at=datetime('now','localtime')
                       WHERE key=?""",
                    ("pending", key),
                )
                conn.commit()
            changed.append(key)
            _schedule_auto_resume(key, delay_sec=30, reason=f"heal-{cur}")
        if changed:
            invalidate_status_cache()
            _log_sync(f"未完成状态纠偏：{', '.join(changed)}")
    except Exception as e:
        print(f"⚠️  未完成状态纠偏失败：{e}", flush=True)


def _scan_incomplete_for_resume():
    """周期扫描：仅对「超时 / 孤儿愈合」产生的 pending 排队续跑，不唤醒从未同步的占位项。

    已在 resume_queue 中的 key 直接跳过——避免每轮把 parallel-cap / still-running
    的 not_before / reason 刷成 scan-incomplete，打断分档消峰闭环。
    """
    queued = _resume_queue_keys()
    with lock:
        cands = [
            k for k, v in sync_data.items()
            if v.get("status") == "pending"
            and (v.get("sync_script") or "").strip()
            and k not in queued
        ]
    if not cands:
        return
    for key in cands:
        try:
            with db_conn() as conn:
                hist = conn.execute(
                    "SELECT status, output_summary FROM sync_history "
                    "WHERE key=? ORDER BY id DESC LIMIT 1",
                    (key,),
                ).fetchone()
            if not hist:
                continue
            out = hist["output_summary"] or ""
            st = hist["status"] or ""
            # 只续跑：history=timeout，或文案含超时/孤儿/auto-resume
            if st == "timeout" or _is_resume_worthy_message(out):
                _schedule_auto_resume(key, delay_sec=30, reason="scan-incomplete")
        except Exception:
            continue


# 兼容旧名
def _relabel_timeout_fails():
    _heal_incomplete_statuses()
# ==================================================================

def is_safe_path(basedir, path):
    try:
        base_real = os.path.realpath(basedir)
        path_real = os.path.realpath(path)
        # 用 commonpath 严格判断包含关系，避免 startstring 前缀绕过(/foo-x vs /foo)
        return os.path.commonpath([base_real, path_real]) == base_real
    except (ValueError, OSError):
        return False

def get_last_modified_date(directory_path):
    try:
        if not os.path.isdir(directory_path):
            return None
        timestamp = os.path.getmtime(directory_path)
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None

def scan_all_sizes_background():
    """轻量回填：只 stat 目录 mtime 补 last_sync_date，不再本机 du（NFS 上 du 大目录会卡死）。
    镜像大小改由 8.74 脚本 scan_sizes.sh 每天扫一次写回 db，本机定时 reload。"""
    backfill_last_sync_date()


def backfill_single_sync_date(key):
    """同步完成后回填单个镜像的 last_sync_date（不 du，避免 NFS 卡死）。"""
    try:
        with lock:
            item = sync_data.get(key)
        if not item or item.get("last_sync_date"):
            return
        base = BASE_SYSTEM_PATH if item.get("is_system") else BASE_MIRROR_PATH
        path = os.path.join(base, item["url"])
        mdate = get_last_modified_date(path)
        if mdate:
            with lock:
                if key in sync_data and not sync_data[key].get("last_sync_date"):
                    sync_data[key]["last_sync_date"] = mdate
    except Exception as e:
        print(f"⚠️  回填 last_sync_date 失败 [{key}]：{e}", flush=True)

def backfill_last_sync_date():
    """轻量回填：只 stat 目录 mtime（不打 du），快速补全空的 last_sync_date。
    避免 /api/status 在启动初期显示空白，又不拖慢请求。"""
    try:
        with lock:
            keys = list(sync_data.keys())
        for k in keys:
            with lock:
                item = sync_data.get(k)
                if not item or item.get("last_sync_date"):
                    continue
            base = BASE_SYSTEM_PATH if item.get("is_system") else BASE_MIRROR_PATH
            path = os.path.join(base, item["url"])
            mdate = get_last_modified_date(path)
            if mdate:
                with lock:
                    if not sync_data[k].get("last_sync_date"):
                        sync_data[k]["last_sync_date"] = mdate
        save_persisted_data()
    except Exception as e:
        print(f"⚠️  回填 last_sync_date 失败：{e}")

def reload_sizes_from_db():
    """从 db 读取 total_size_gb 刷进内存（8.74 脚本写 db 后本机读）。"""
    try:
        with db_conn() as conn:
            rows = conn.execute("SELECT key, total_size_gb FROM mirrors").fetchall()
        with lock:
            for r in rows:
                k = r["key"]
                if k in sync_data:
                    sync_data[k]["total_size_gb"] = r["total_size_gb"] or "获取中"
    except Exception as e:
        print(f"⚠️  reload 大小失败：{e}")

def reload_status_from_db():
    """从 db 读取 status / last_sync_date 刷进内存，让本机内存自我修正与 db 的偏差。

    背景：status / last_sync_date 的唯一权威源是 /api/report（8.74 wrapper 跑完
    同步后回写 db 并刷本机内存）。但本机内存一旦因任何原因与 db 漂移（启动恢复
    把 syncing 留着、某次上报走了别的路径、或第二个 Flask 实例的陈旧内存覆盖等），
    就再无机会自动对齐，前端会长期停留在旧 fail 状态。本函数周期性用 db 的权威值
    覆盖内存，使偏差在下一周期自愈，无需重启。

    约束：
      - 只读 db 刷内存，绝不回写 db（避免陈旧内存反向覆盖 db 的权威值）。
      - 只重载终态字段 status / last_sync_date；不碰 is_scanning / 'syncing'
        运行态（重载会与正在跑的同步任务冲突，syncing 留给 _recover_orphaned_syncing
        与 /api/report 处理）。
    """
    try:
        with db_conn() as conn:
            rows = conn.execute("SELECT key, status, last_sync_date FROM mirrors").fetchall()
        with lock:
            for r in rows:
                k = r["key"]
                item = sync_data.get(k)
                if not item:
                    continue
                # 正在同步的运行态不重载，等 /api/report 回写终态
                if item.get("status") == "syncing":
                    continue
                item["status"] = r["status"] or "success"
                item["last_sync_date"] = r["last_sync_date"]
    except Exception as e:
        print(f"⚠️  reload 状态失败：{e}")


def size_scan_daemon():
    """启动后回填 last_sync_date，然后定时从 db reload 大小与状态。
    实际 du 由 8.74 脚本跑（NFS 本地盘快），本机不 du。
    reload_status_from_db 让本机内存周期性对齐 db 权威状态，自愈漂移。
    周期调用 _recover_orphaned_syncing：远端锁已空闲但仍卡在 syncing 的条目可自愈，
    无需重启进程（须在 reload_status 之前落库，避免 DB 旧 syncing 盖回内存）。"""
    backfill_last_sync_date()
    _daemon_beat("size_scan")
    time.sleep(120)
    while True:
        _daemon_beat("size_scan")
        try:
            _recover_orphaned_syncing()
        except Exception as e:
            print(f"⚠️  孤儿 syncing 恢复失败：{e}", flush=True)
        try:
            _heal_incomplete_statuses()
        except Exception as e:
            print(f"⚠️  未完成状态纠偏失败：{e}", flush=True)
        reload_sizes_from_db()
        reload_status_from_db()
        time.sleep(600)

# ====================== 定时同步调度 ======================
# cron_expr 支持：daily（每天 03:00）、weekly（每周日 03:00）、或标准 5 段 cron（分 时 日 月 周）
# 字段支持单值、列表、范围、步进（如 1,3,5 / 1-5 / */15 / 1-10/2）。
def _parse_cron(expr):
    """解析 cron 表达式，返回 (minute, hour, day, month, weekday) 或 None。"""
    expr = (expr or "").strip().lower()
    if not expr:
        return None
    presets = {"daily": "0 3 * * *", "weekly": "0 3 * * 0", "hourly": "0 * * * *"}
    expr = presets.get(expr, expr)
    parts = expr.split()
    if len(parts) != 5:
        return None

    def parse_field(field, lo, hi, is_dow=False):
        field = (field or "").strip()
        if field == "*":
            return None
        values = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                return None
            step = 1
            if "/" in part:
                base, step_str = part.split("/", 1)
                try:
                    step = int(step_str)
                except ValueError:
                    return None
                if step <= 0:
                    return None
            else:
                base = part
            if base == "*":
                start, end = lo, hi
            elif "-" in base:
                a, b = base.split("-", 1)
                try:
                    start, end = int(a), int(b)
                except ValueError:
                    return None
            else:
                try:
                    start = end = int(base)
                except ValueError:
                    return None
            if is_dow:
                if start == 7:
                    start = 0
                if end == 7:
                    end = 0
                if start == 0 and end == 0 and base != "*":
                    values.add(0)
                    continue
            if not (lo <= start <= hi and lo <= end <= hi and start <= end):
                return None
            values.update(range(start, end + 1, step))
        return values

    minute = parse_field(parts[0], 0, 59)
    hour = parse_field(parts[1], 0, 23)
    day = parse_field(parts[2], 1, 31)
    month = parse_field(parts[3], 1, 12)
    weekday = parse_field(parts[4], 0, 6, is_dow=True)
    if any(part != "*" and parsed is None for part, parsed in zip(parts, (minute, hour, day, month, weekday))):
        return None
    return (minute, hour, day, month, weekday)

def _cron_match(cron, dt):
    """判断 datetime dt 是否匹配 cron（精确到分钟）。"""
    m, h, d, mo, w = cron
    if m is not None and dt.minute not in m: return False
    if h is not None and dt.hour not in h: return False
    if d is not None and dt.day not in d: return False
    if mo is not None and dt.month not in mo: return False
    if w is not None and ((dt.weekday() + 1) % 7) not in w: return False  # 周日=0
    return True

def _next_cron_run(cron, now=None):
    """给定解析后的 cron (m,h,d,mo,w)，算出 now 之后下一次匹配时间。"""
    if now is None:
        now = datetime.now()
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = t + timedelta(days=366)
    while t <= limit:
        if _cron_match(cron, t):
            return t
        t += timedelta(minutes=1)
    return None

# 8.74 crontab 缓存：{脚本 basename: cron表达式字符串}
# 后台定期刷新，/api/status 用它给每个镜像算下次同步时间
_crontab_cache = {}            # {script_basename: "m h dom mon dow"}
_crontab_cache_lock = threading.Lock()
_crontab_cache_at = 0          # 上次刷新的 epoch

def _refresh_crontab_cache():
    """从 8.74 拉 crontab，解析成 {脚本名: cron表达式} 映射。"""
    global _crontab_cache_at
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=8",
             SYNC_EXEC_HOST, "crontab -l"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=15
        )
        if result.returncode != 0:
            return
        mapping = {}
        for line in (result.stdout or "").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            fields = s.split(None, 5)
            if len(fields) < 6:
                continue
            m, h, dom, mon, dow, cmd = fields
            # 提取同步脚本名。多数任务是 `wrapper.py|ai_dispatch.py <真实脚本>`，
            # 故优先识别调度器调用、取其后第一个参数作为真实脚本；
            # 否则正则会匹配到调度器自身，所有任务都落到同一个 key。
            import re as _re
            base = None
            m_disp = _re.search(r'\b(?:wrapper|ai_dispatch)\.py\s+(\S+)', cmd)
            if m_disp:
                arg = m_disp.group(1)
                if arg.endswith(".py") or arg.endswith(".sh"):
                    base = arg.split("/")[-1]
                elif arg == "bandersnatch":
                    # pypi 用 bandersnatch 命令而非脚本，映射到 pypi_sync.py
                    base = "pypi_sync.py"
            else:
                m_py = _re.search(r'([A-Za-z0-9_./-]+\.py)\b', cmd)
                m_sh = _re.search(r'([A-Za-z0-9_./-]+\.sh)\b', cmd)
                if m_py and m_py.group(1).split("/")[-1] not in ("wrapper.py", "ai_dispatch.py"):
                    base = m_py.group(1).split("/")[-1]
                elif m_sh:
                    base = m_sh.group(1).split("/")[-1]
                elif "bandersnatch" in cmd:
                    base = "pypi_sync.py"
            if not base:
                continue
            mapping[base] = f"{m} {h} {dom} {mon} {dow}"
        with _crontab_cache_lock:
            _crontab_cache.clear()
            _crontab_cache.update(mapping)
            _crontab_cache_at = time.time()
    except Exception as e:
        print(f"⚠️  刷新 crontab 缓存失败：{e}")

def crontab_cache_daemon():
    """后台线程：每 5 分钟刷新一次 8.74 crontab 缓存。"""
    _daemon_beat("crontab_cache")
    time.sleep(30)  # 启动后等 DB 加载
    while True:
        _daemon_beat("crontab_cache")
        _refresh_crontab_cache()
        time.sleep(300)

def _next_sync_for_script(script_path):
    """根据镜像的 sync_script 路径，从 crontab 缓存查下次同步时间。
    返回 (cron_expr, next_dt) 或 (None, None)。"""
    if not script_path:
        return None, None
    base = script_path.split("/")[-1]
    with _crontab_cache_lock:
        expr = _crontab_cache.get(base)
    if not expr:
        return None, None
    cron = _parse_cron(expr)
    if not cron:
        return expr, None
    return expr, _next_cron_run(cron)

# 记录每个镜像上次触发定时的分钟，避免同一分钟内重复触发
_last_cron_trigger = {}

# 进程内正在同步的 key 集合：防止 admin/agent 对同一镜像重复触发（admin 路径绕过 wrapper，
# 无 flock，靠此守卫避免并发 lftp/rsync 互删临时文件导致卡死不回写）
_running_syncs = set()

def _remote_sync_lock_state(key):
    """探测同步锁是否仍被持有。
    返回: ("running"|"idle"|"unknown", detail)

    Flask 与 wrapper 同机时优先本地 flock；本地不可用再 ssh 到 SYNC_EXEC_HOST。
    """
    local_st, local_detail = _local_sync_lock_state(key)
    if local_st in ("running", "idle"):
        return local_st, local_detail
    lockfile = f"{SYNC_FLOCK_DIR}/{key}.lock"
    remote_cmd = (
        f"mkdir -p {shlex.quote(SYNC_FLOCK_DIR)} && "
        f"flock -n -E 75 {shlex.quote(lockfile)} true"
    )
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=10", SYNC_EXEC_HOST, remote_cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "unknown", "锁探测超时"
    except Exception as e:
        return "unknown", f"锁探测异常: {e}"
    if result.returncode == 75:
        return "running", "远端锁被占用"
    if result.returncode == 0:
        return "idle", "远端锁空闲"
    out = (result.stdout or "").strip()
    return "unknown", f"锁探测返回码 {result.returncode}: {out[-200:]}"


def _manual_sync_remote_cmd(key, executor="wrapper"):
    """生成与 8.74 主链路一致的手动触发命令。
    executor: wrapper（本机）| ansible（ai_dispatch 分发到 bnode）。
    """
    item = sync_data.get(key) or {}
    script = (item.get("sync_script") or "").strip()
    cmd = item.get("sync_cmd") or []
    executor = (executor or "wrapper").strip()
    if executor not in ("wrapper", "ansible"):
        executor = "wrapper"
    if script:
        target = "bandersnatch" if os.path.basename(script) == "pypi_sync.py" else script
        dispatcher = SYNC_WRAPPER_PATH if executor == "wrapper" else SYNC_AI_DISPATCH_PATH
        remote_cmd = f"python3 {shlex.quote(dispatcher)} {shlex.quote(target)}"
        label = "wrapper" if executor == "wrapper" else "ai_dispatch"
        return remote_cmd, f"{label} {target}"
    if cmd:
        if executor == "ansible":
            return None, None  # 无 sync_script 时无法走 ansible 分发
        lockfile = f"{SYNC_FLOCK_DIR}/{key}.lock"
        remote_cmd = (
            f"mkdir -p {shlex.quote(SYNC_FLOCK_DIR)} && "
            f"flock -n -E 75 {shlex.quote(lockfile)} " +
            " ".join(shlex.quote(x) for x in cmd)
        )
        return remote_cmd, f"命令 {cmd}"
    return None, None


def trigger_sync_via_wrapper(key, trigger_type, trigger_by, executor="wrapper"):
    """统一触发一轮同步，但不在 8.13 本地直接写结果。
    真实状态由 8.74 wrapper/ai_dispatch -> /api/report 回写；本地只负责起跑和防重入。
    触发侧不再用「等待时长」判定成败：远端后台启动后立即返回，终态只看镜像 status。
    """
    if key not in sync_data:
        return {"ok": False, "error": f"无效镜像 key: {key}"}
    with lock:
        item = sync_data.get(key) or {}
        if item.get("is_scanning"):
            return {"ok": False, "error": f"{key} 正在扫描中，请稍后再试"}
        if item.get("status") == "syncing":
            return {"ok": False, "error": f"{key} 正在同步中，请稍后再试"}
        if key in _running_syncs:
            return {"ok": False, "error": f"{key} 已有同步触发在执行"}
        remote_cmd, desc = _manual_sync_remote_cmd(key, executor=executor)
        if not remote_cmd:
            return {"ok": False, "error": "未配置 sync_script，或该镜像无法用 ansible 分发"}
        _running_syncs.add(key)
        sync_data[key]["status"] = "syncing"
    # save_to_db 不写 status；此处必须落库，否则 DB 长期 pending、重启后漂移
    _persist_mirror_status(key, "syncing")
    invalidate_status_cache()
    _set_sync_trigger(key, trigger_type, trigger_by)
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 远端 nohup 后台启动：SSH 只负责提交，不等待整次同步结束
    log_file = f"/tmp/mirror-trigger-{key}.log"
    bg_cmd = (
        f"nohup {remote_cmd} >{shlex.quote(log_file)} 2>&1 & "
        f"echo TRIGGERED:$!"
    )
    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=15", SYNC_EXEC_HOST, bg_cmd]

    def _runner():
        try:
            _log_sync(f"[{key}] 触发同步开始 @ {started_at} -> {SYNC_EXEC_HOST} ({desc})")
            result = subprocess.run(
                ssh_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=45,  # 仅等待「提交成功」回执，不覆盖整次同步
            )
            out = (result.stdout or "").strip()
            if out:
                _log_sync(f"[{key}] 触发输出:\n{out[-2000:]}")
            if result.returncode == 0 and "TRIGGERED:" in out:
                _log_sync(f"[{key}] 已提交到 8.74 后台执行，终态等待 /api/report（status=syncing）")
                return
            if result.returncode == 0:
                # 无 TRIGGERED 标记也当作已提交（兼容旧 shell）
                _log_sync(f"[{key}] SSH 返回 0，保持 syncing，等待 /api/report")
                return
            if result.returncode == 75:
                trig = _peek_sync_trigger(key)
                _clear_sync_trigger(key)
                with lock:
                    if key in sync_data:
                        sync_data[key]["status"] = "syncing"
                _persist_mirror_status(key, "syncing")
                _log_sync(f"[{key}] 触发跳过：8.74 上已有同步在跑（flock 占用）")
                if trig and trig.get("type") == "schedule":
                    record_sync_history(key, "skip", started_at, "另一链路正在同步，跳过")
                return
            # 提交失败才标 fail；其余情况维持 syncing，由状态/回写决定
            with lock:
                if key in sync_data:
                    sync_data[key]["status"] = "fail"
            _persist_mirror_status(key, "fail")
            invalidate_status_cache()
            msg = f"触发失败，返回码 {result.returncode}\n{out[-800:]}"
            _log_sync(f"[{key}] 触发失败 ✗ (返回码 {result.returncode})")
            record_sync_history(key, "fail", started_at, msg)
            notify_sync_fail(key, out[-500:])
            save_persisted_data()
        except subprocess.TimeoutExpired:
            # 提交回执超时：不改失败。远端可能已起，终态只看镜像 status / report。
            with lock:
                if key in sync_data:
                    sync_data[key]["status"] = "syncing"
            _persist_mirror_status(key, "syncing")
            invalidate_status_cache()
            _log_sync(f"[{key}] 触发提交等待超时，保持 syncing，以镜像状态为准")
        except Exception as e:
            with lock:
                if key in sync_data:
                    sync_data[key]["status"] = "fail"
            _persist_mirror_status(key, "fail")
            invalidate_status_cache()
            _log_sync(f"[{key}] 触发异常: {e}")
            record_sync_history(key, "fail", started_at, f"触发异常: {e}")
            notify_sync_fail(key, f"触发异常: {e}")
            save_persisted_data()
        finally:
            with lock:
                _running_syncs.discard(key)

    threading.Thread(target=_runner, daemon=True).start()
    return {"ok": True, "message": f"已触发 {key} ({item.get('name', key)}) 同步",
            "executor": executor if (item.get("sync_script") or "").strip() else "wrapper"}

def cron_sync_daemon():
    """每分钟检查一次，对启用了 cron 的镜像按计划触发同步。"""
    if not ENABLE_DB_CRON_DAEMON:
        _log_sync("[DB-CRON] 已禁用 cron_sync_daemon（如需启用请设置 ENABLE_DB_CRON_DAEMON=1）")
        return
    time.sleep(60)  # 启动后等 1 分钟，让 DB 加载完成
    while True:
        try:
            now = datetime.now()
            cur_min = now.strftime("%Y-%m-%d %H:%M")
            with lock:
                keys = list(sync_data.keys())
            for k in keys:
                with lock:
                    item = sync_data.get(k)
                    if not item or not item.get("cron_enabled") or item.get("is_scanning"):
                        continue
                    if item.get("status") == "syncing":
                        continue
                    expr = item.get("cron_expr")
                cron = _parse_cron(expr)
                if not cron:
                    continue
                if _last_cron_trigger.get(k) == cur_min:
                    continue
                if _cron_match(cron, now):
                    _last_cron_trigger[k] = cur_min
                    _log_sync(f"[{k}] DB 定时同步触发 (cron: {expr})")
                    ret = trigger_sync_via_wrapper(k, "schedule", "db-cron")
                    if not ret.get("ok"):
                        _log_sync(f"[{k}] DB 定时触发失败: {ret.get('error')}")
        except Exception as e:
            print(f"⚠️  定时调度异常：{e}")
        time.sleep(60)

@app.route('/db/<key>/<path:filename>')
def serve_file(key, filename):
    if key not in sync_data:
        return jsonify({"error": "无效key"}), 404
    if sync_data[key].get("is_system"):
        base_dir = os.path.join(BASE_SYSTEM_PATH, sync_data[key]['url'])
    else:
        base_dir = os.path.join(BASE_MIRROR_PATH, sync_data[key]['url'])
    file_path = os.path.join(base_dir, filename)
    if not is_safe_path(base_dir, file_path):
        return jsonify({"error": "非法路径"}), 403
    if not os.path.isfile(file_path):
        return jsonify({"error": "文件不存在"}), 404
    return send_from_directory(base_dir, filename, as_attachment=True)

# /api/status 结果缓存：Flask 单进程开发服务器下，避免每个并发请求都重算
# 44 个镜像的 cron 下次同步时间并抢全局锁，导致并发串行排队卡顿。
# next_sync 精度到分钟，15 秒缓存对前端无感知。
_STATUS_CACHE = {"data": None, "at": 0}
_STATUS_CACHE_TTL = 15

def invalidate_status_cache():
    """失效 /api/status 响应缓存。

    /api/status 带最多 15s TTL 缓存以避免并发抢锁。但 /api/report 刷新内存后
    若不清缓存，前端在 TTL 窗口内仍会拿到旧值（实测最多滞后 15s）。任何改动
    sync_data 且需前端即时可见的路径都应调用本函数。"""
    with lock:
        _STATUS_CACHE["data"] = None
        _STATUS_CACHE["at"] = 0

@app.route('/api/status', methods=['GET'])
def api_status():
    now = time.time()
    with lock:
        cached = _STATUS_CACHE["data"]
        if cached is not None and (now - _STATUS_CACHE["at"]) < _STATUS_CACHE_TTL:
            return cached
    # 纯内存读取，避免对 NFS 共享存储逐个 stat 导致请求变慢（3-4s）。
    # last_sync_date 由后台扫描 / 同步任务回填，API 不再实时打 NFS。
    result = {}
    with lock:
        for k, v in sync_data.items():
            item = v.copy()
            # 用 8.74 crontab 缓存匹配该镜像的 sync_script，算下次同步时间
            expr, nxt = _next_sync_for_script(item.get("sync_script"))
            item["cron_expr"] = expr or ""
            item["cron_enabled"] = bool(expr)
            item["next_sync"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""
            # 公网脱敏：删内部实现字段（sync_cmd 含上游源地址，sync_script 含内部脚本路径）
            for _f in ("sync_cmd", "sync_script", "is_scanning"):
                item.pop(_f, None)
            # 超时不是用户可见终态：展示为「同步中」（后台在监控/续跑）
            if item.get("status") == "timeout":
                item["status"] = "syncing"
            result[k] = item
    resp = jsonify(result)
    with lock:
        # 写缓存也纳入 lock，与 invalidate_status_cache 互斥，避免失效后被旧值回填
        _STATUS_CACHE["data"] = resp
        _STATUS_CACHE["at"] = now
    return resp

@app.route('/api/sync', methods=['POST'])
@login_required
def api_sync():
    try:
        data = request.get_json()
    except Exception:
        return jsonify({"error": "请求格式错误"}), 400
    key = data.get("key")
    if key not in sync_data:
        return jsonify({"error": "无效key"}), 400
    audit_log("sync", target=key, detail={"cmd": sync_data[key].get("sync_cmd", [])})
    ret = trigger_sync_via_wrapper(key, "manual", "admin")
    code = 200 if ret.get("ok") else 400
    return jsonify({"message": ret.get("message"), "error": ret.get("error")}), code

@app.route('/api/log/<key>', methods=['GET'])
@login_required
def api_log(key):
    if key not in sync_data:
        return jsonify({"error": "无效key"}), 400
    return jsonify({
        "last_sync_date": sync_data[key].get("last_sync_date"),
        "size": sync_data[key].get("total_size_gb")
    })

@app.route('/api/permission', methods=['GET'])
@login_required
def get_permission():
    path = request.args.get('path')
    if not path:
        return jsonify({"error": "缺少path参数"}), 400
    full_path = os.path.join(BASE_MIRROR_PATH, path.strip('/'))
    if not is_safe_path(BASE_MIRROR_PATH, full_path):
        return jsonify({"error": "非法路径"}), 403
    if not os.path.exists(full_path):
        return jsonify({"error": "路径不存在"}), 404
    try:
        st = os.stat(full_path)
        return jsonify({
            "owner": pwd.getpwuid(st.st_uid).pw_name,
            "group": grp.getgrgid(st.st_gid).gr_name,
            "mode": stat.filemode(st.st_mode),
            "mode_octal": oct(st.st_mode & 0o777)
        })
    except Exception:
        return jsonify({"error": "权限获取失败"}), 500

@app.route('/api/update_sizes', methods=['POST'])
def update_sizes():
    # 内部接口：app-02.py 扫描上报用，需带本地令牌校验，防公网伪造大小数据
    token = request.headers.get("X-Reload-Token", "")
    if not RELOAD_TOKEN or not secrets.compare_digest(token, RELOAD_TOKEN):
        return jsonify({"error": "未授权"}), 401
    try:
        data = request.get_json()
        with lock:
            for dir_path, info in data.items():
                for key, val in sync_data.items():
                    base = BASE_SYSTEM_PATH if val.get('is_system') else BASE_MIRROR_PATH
                    full = os.path.join(base, val['url']).rstrip('/')
                    target = dir_path.rstrip('/')
                    if full == target:
                        val['total_size_gb'] = info['size']
                        val['last_sync_date'] = info['last_scanned']
                        break
        save_persisted_data()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": f"更新大小失败：{e}"}), 500

# ====================== 管理端 API ======================
@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    try:
        data = request.get_json() or {}
    except Exception:
        return jsonify({"error": "请求格式错误"}), 400
    username = (data.get("username") or "").strip()
    password = data.get("password", "")
    ip = (request.headers.get("X-Forwarded-For", "") or "").split(",")[0].strip() or request.remote_addr or "unknown"

    # 0) 登录限速：同 IP+用户名 5 分钟失败 5 次则锁定
    limited, retry = _check_login_limited(ip, username)
    if limited:
        return jsonify({"error": f"登录尝试过多，请 {retry} 秒后再试"}), 429

    # 1) 先试本地 admin 账号（用户名 admin 且本地密码匹配，常量时间比较）
    if username == "admin" and ADMIN_PASSWORD and secrets.compare_digest(
            password.encode("utf-8", "ignore"), ADMIN_PASSWORD.encode("utf-8", "ignore")):
        _clear_login_fails(ip, username)
        session.permanent = True
        app.permanent_session_lifetime = ADMIN_SESSION_TTL
        session["admin_logged_in"] = True
        session["admin_login_at"] = time.time()
        session["admin_user"] = "admin"
        session["csrf_token"] = secrets.token_hex(16)
        audit_log("login", target="admin", detail={"via": "local", "ok": True})
        return jsonify({"message": "登录成功", "user": "admin", "csrf_token": session["csrf_token"]}), 200

    # 2) 再试 AD 域认证
    if AD_LDAP_HOST and AD_BASE_DN and AD_ADMIN_GROUP_DN:
        ok, info = _ad_authenticate(username, password)
        if ok:
            _clear_login_fails(ip, username)
            session.permanent = True
            app.permanent_session_lifetime = ADMIN_SESSION_TTL
            session["admin_logged_in"] = True
            session["admin_login_at"] = time.time()
            session["admin_user"] = username
            session["csrf_token"] = secrets.token_hex(16)
            audit_log("login", target=username, detail={"via": "ad", "dn": info})
            return jsonify({"message": "登录成功", "user": username, "csrf_token": session["csrf_token"]}), 200
        _record_login_fail(ip, username)
        audit_log("login_failed", target=username or "unknown",
                  detail={"via": "ad", "reason": info})
        return jsonify({"error": info}), 401

    # 3) 既非本地 admin，AD 也未配置或失败
    _record_login_fail(ip, username)
    audit_log("login_failed", target=username or "unknown",
              detail={"via": "none", "ok": False})
    return jsonify({"error": "账号或密码错误"}), 401

@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.clear()
    return jsonify({"message": "已退出"}), 200

# ====================== AI AGENT（自动巡检：检索镜像 + 诊断失败 + 自动重试） ======================
# 网关后端为 GLM，不支持 Anthropic tool use，故采用上下文注入：
# 后端自动收集镜像状态/失败历史/日志喂给模型，模型直接产出诊断结论。
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "http://172.16.8.245:4002")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")
ANTHROPIC_AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "none")
# 技能 Agent 专用网关：直连 new-api 的 OpenAI 格式端点（cc0 代理的 Anthropic 格式
# 转换会丢弃 MiniMax 的 tool_use 块，故不走 ANTHROPIC_BASE_URL）
AGENT_SKILL_BASE_URL = os.environ.get("AGENT_SKILL_BASE_URL", "http://172.16.25.252:3030")
AGENT_SKILL_API_KEY = os.environ.get("AGENT_SKILL_API_KEY", ANTHROPIC_AUTH_TOKEN)
AGENT_SKILL_MODEL = os.environ.get("AGENT_SKILL_MODEL", "") or ANTHROPIC_MODEL
AGENT_AUTO_RETRY = os.environ.get("AGENT_AUTO_RETRY", "1") == "1"  # 巡检时是否自动重试失败镜像
AGENT_INSPECT_INTERVAL = int(os.environ.get("AGENT_INSPECT_INTERVAL", "1800"))  # 定时巡检秒数，默认 30 分钟
# 同一镜像自动重试最短间隔：避免巡检刚把 fail 标回又立刻批量触发，公网长期卡在「同步中」
AGENT_RETRY_COOLDOWN_SEC = int(os.environ.get("AGENT_RETRY_COOLDOWN_SEC", str(6 * 3600)))

import urllib.request as _urlreq
import urllib.error as _urlerr

# 最近一次巡检结果缓存（供前端轮询/定时线程刷新）
_last_inspect = {"data": None, "at": 0}
_inspect_lock = threading.Lock()

def _agent_list_mirrors():
    """返回所有镜像的概览（key/名称/状态/大小/最后同步/是否系统源）。"""
    items = admin_list_all()
    return [{"key": m["key"], "name": m["name"], "status": m["status"],
             "size": m["total_size_gb"], "last_sync": m["last_sync_date"],
             "is_system": m["is_system"], "cron": m["cron_expr"] or None,
             "description": m["description"]} for m in items]

def _agent_sync_history(key="", status="", limit=20):
    """查同步历史记录。status 可为 success/fail/skip。"""
    try:
        limit = max(1, min(int(limit), 50))
    except (ValueError, TypeError):
        limit = 20
    where, params = [], []
    if key:
        where.append("key = ?"); params.append(key)
    if status:
        where.append("status = ?"); params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_db_conn()
    try:
        rows = conn.execute(
            f"SELECT key, name, status, trigger_type, started_at, finished_at, duration_sec, output_summary "
            f"FROM sync_history {where_sql} ORDER BY id DESC LIMIT ?", params + [limit]
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]

def _agent_sync_logs(keyword="", n=200):
    """拉同步进程日志（app.log + app-02.log），可按关键词过滤。"""
    try:
        n = max(10, min(int(n), 500))
    except (ValueError, TypeError):
        n = 200
    out = {}
    for tag, fname in (("app", "app.log"), ("app-02", "app-02.log")):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
        lines, err = _tail_file(path, n, keyword)
        out[tag] = {"lines": lines, "count": len(lines), "error": err}
    return out

def _agent_trigger_sync(key, executor="auto"):
    """触发某镜像同步。executor: auto|wrapper|ansible。"""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "缺少 key"}
    reason = ""
    chosen = executor
    if executor in ("", "auto", None):
        rec = _recommend_sync_executor(key)
        chosen = rec.get("executor") or "wrapper"
        reason = rec.get("reason") or ""
    elif executor not in ("wrapper", "ansible"):
        return {"ok": False, "error": "executor 仅支持 auto/wrapper/ansible"}
    audit_log("sync", target=key, detail={"via": "ai-agent", "executor": chosen, "reason": reason})
    ret = trigger_sync_via_wrapper(key, "manual", "ai-agent", executor=chosen)
    if isinstance(ret, dict):
        ret["chosen_executor"] = chosen
        if reason:
            ret["reason"] = reason
    return ret

# 大体积/长耗时镜像：倾向 Ansible 分发到 bnode，避免占满调度机
_ANSIBLE_PREFERRED_KEYS = {
    "pdb", "uniprot", "alphafold", "ncbi", "pypi", "conda", "cp2k",
    "gromacs", "lammps", "openfoam", "rosetta", "chembl",
}


def _parse_size_to_gb(size_str):
    """把 '120G'/'1.5T'/'800M' 等粗略转成 GB。"""
    s = (size_str or "").strip().upper().replace(",", "")
    if not s or s in ("-", "未知", "N/A", "获取中"):
        return None
    import re as _re
    m = _re.match(r"^([\d.]+)\s*([KMGT]?I?B?)$", s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "G")[0]
    mul = {"K": 1 / (1024 ** 2), "M": 1 / 1024, "G": 1, "T": 1024}.get(unit, 1)
    return n * mul


def _recommend_sync_executor(key):
    """根据镜像规模/历史，推荐 wrapper 或 ansible。"""
    key = (key or "").strip()
    item = sync_data.get(key) or {}
    script = (item.get("sync_script") or "").strip()
    if not script:
        return {"executor": "wrapper", "reason": "无 sync_script，只能本机/命令方式",
                "key": key, "can_ansible": False}
    reasons = []
    score_ansible = 0
    kl = key.lower()
    if kl in _ANSIBLE_PREFERRED_KEYS or any(x in kl for x in ("pdb", "uniprot", "ncbi", "pypi", "conda")):
        score_ansible += 3
        reasons.append("属于大库/重负载镜像名单")
    gb = _parse_size_to_gb(item.get("total_size_gb") or "")
    if gb is not None:
        if gb >= 100:
            score_ansible += 3
            reasons.append(f"体积约 {gb:.0f}G ≥100G")
        elif gb >= 30:
            score_ansible += 2
            reasons.append(f"体积约 {gb:.0f}G ≥30G")
        elif gb < 5:
            score_ansible -= 2
            reasons.append(f"体积约 {gb:.1f}G 较小，适合本机")
    try:
        hist = _agent_sync_history(key=key, limit=5)
        for h in hist:
            st = (h.get("status") or "")
            out = (h.get("output_summary") or h.get("output") or "")
            if st == "fail" and any(w in out for w in ("超时", "timeout", "Timeout", "killed")):
                score_ansible += 2
                reasons.append("近期有超时类失败")
                break
            dur = h.get("duration_sec")
            try:
                if dur is not None and float(dur) > 7200:
                    score_ansible += 2
                    reasons.append("近期单次耗时>2小时")
                    break
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    executor = "ansible" if score_ansible >= 2 else "wrapper"
    if not reasons:
        reasons.append("默认本机 wrapper（规模不大）" if executor == "wrapper"
                       else "综合评估倾向 Ansible 分发")
    return {
        "key": key,
        "executor": executor,
        "score_ansible": score_ansible,
        "reason": "；".join(reasons),
        "can_ansible": True,
        "sync_script": script,
        "size": item.get("total_size_gb") or "",
        # 镜像同步永远只要 1 个节点；ansible 分发由 ai_dispatch 按体积选机
        "nodes": 1,
        "dispatch": "ai_dispatch_single" if executor == "ansible" else "wrapper_local",
    }

# AI 可调用的工具定义（Anthropic tool use 格式）
def _agent_build_context(failed_keys):
    """收集巡检上下文：镜像概览 + 失败镜像的历史与日志，拼成文本喂给 AI。"""
    mirrors = _agent_list_mirrors()
    lines = ["【镜像站当前状态】共 %d 个镜像。" % len(mirrors)]
    for m in mirrors:
        flag = " ⚠️失败" if m["status"] == "fail" else (" 🔄同步中" if m["status"] == "syncing" else "")
        cron = (" 定时:%s" % m["cron"]) if m.get("cron") else ""
        lines.append("- %s(%s): 状态=%s 大小=%s 最后同步=%s%s%s"
                     % (m["key"], m["name"], m["status"], m["size"], m["last_sync"], cron, flag))
    # 失败镜像的详细历史 + 日志
    if failed_keys:
        lines.append("\n【失败镜像详情】")
        for key in failed_keys:
            hist = _agent_sync_history(key=key, status="fail", limit=3)
            lines.append("\n## %s 最近失败记录:" % key)
            for h in hist:
                lines.append("  - %s 触发:%s 耗时:%ss 摘要:%s"
                             % (h.get("started_at"), h.get("trigger_type"), h.get("duration_sec"),
                                (h.get("output_summary") or "")[:300]))
            logs = _agent_sync_logs(keyword=key, n=60)
            for tag, body in logs.items():
                if body.get("lines"):
                    lines.append("  [%s 日志尾部]" % tag)
                    lines.extend("    " + l for l in body["lines"][-30:])
    return "\n".join(lines)

def _agent_gateway_call(payload):
    """POST Anthropic 兼容网关 /v1/messages，返回解析后的完整 JSON 对象。"""
    url = ANTHROPIC_BASE_URL.rstrip("/") + "/v1/messages"
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    if ANTHROPIC_AUTH_TOKEN and ANTHROPIC_AUTH_TOKEN != "none":
        headers["x-api-key"] = ANTHROPIC_AUTH_TOKEN
        headers["Authorization"] = f"Bearer {ANTHROPIC_AUTH_TOKEN}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = _urlreq.Request(url, data=data, headers=headers, method="POST")
    try:
        with _urlreq.urlopen(req, timeout=90) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except _urlerr.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI 网关 HTTP {e.code}: {body[:500]}")
    except Exception as e:
        raise RuntimeError(f"调用 AI 网关失败: {e}")
    if obj.get("type") == "error":
        raise RuntimeError(obj.get("error", {}).get("message", "AI 网关返回错误"))
    return obj

def _agent_openai_call(messages, tools=None, max_tokens=2200, timeout=120):
    """POST OpenAI 兼容端点 /v1/chat/completions，返回 choices[0].message 对象。
    技能 Agent 专用：MiniMax 在 OpenAI 格式下 tool_calls 稳定，Anthropic 格式转换会丢。"""
    payload = {"model": AGENT_SKILL_MODEL, "max_tokens": max_tokens, "messages": messages}
    if tools:
        payload["tools"] = tools
    headers = {"content-type": "application/json"}
    if AGENT_SKILL_API_KEY and AGENT_SKILL_API_KEY != "none":
        headers["Authorization"] = f"Bearer {AGENT_SKILL_API_KEY}"
    req = _urlreq.Request(AGENT_SKILL_BASE_URL.rstrip("/") + "/v1/chat/completions",
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers=headers, method="POST")
    try:
        with _urlreq.urlopen(req, timeout=timeout) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except _urlerr.HTTPError as e:
        raise RuntimeError(f"技能网关 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}")
    except Exception as e:
        raise RuntimeError(f"调用技能网关失败: {e}")
    try:
        return obj["choices"][0]["message"]
    except (KeyError, IndexError):
        raise RuntimeError(f"技能网关返回异常: {json.dumps(obj, ensure_ascii=False)[:300]}")

# 部分模型（如 MiniMax）会输出思考块（尖括号 think 标签），对展示无意义需剔除。
# 正则用 chr 拼接构造，避免源码中出现字面标签被外部工具误处理。
# 注意：MiniMax 在 tool use 场景常把结论写进未闭合的思考块，因此先剔完整块，
# 若剔完为空则退化为只去开标签保留内容，避免把正文一起删掉。
_THINK_RE = re.compile(chr(60) + r"think" + chr(62) + r".*?" + chr(60) + r"/think" + chr(62), re.S)
_THINK_OPEN_RE = re.compile(chr(60) + r"think" + chr(62))
_THINK_CLOSE_RE = re.compile(chr(60) + r"/think" + chr(62))

def _strip_think(text):
    """剔除模型输出中的思考块；未闭合时只去标签保留正文，孤立闭标签也清掉。"""
    t = text or ""
    stripped = _THINK_RE.sub("", t).strip()
    if stripped:
        return _THINK_CLOSE_RE.sub("", stripped).strip()
    return _THINK_CLOSE_RE.sub("", _THINK_OPEN_RE.sub("", t)).strip()

def _agent_call_claude(system_prompt, user_text, max_tokens=2048):
    """单轮文本调用（无 tool use，供定时巡检诊断用）。返回文本或抛错。"""
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_text}],
    }
    obj = _agent_gateway_call(payload)
    parts = []
    for block in obj.get("content", []):
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return _strip_think("\n".join(parts))

def _last_failure_was_timeout(key):
    """该 key 最近一次终态是否为超时。超时不重试（交由 cron/wrapper 长链路），
    避免 agent 30min 循环重试大库(PDB/AlphaFold/UniProt 等)反复超时。"""
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT status, output_summary FROM sync_history WHERE key=? "
                "AND status IN ('fail','timeout') "
                "ORDER BY id DESC LIMIT 1", (key,)
            ).fetchone()
        if not row:
            return False
        if row["status"] == "timeout":
            return True
        return _is_timeout_message(row["output_summary"] or "")
    except Exception:
        return False


def _agent_auto_retry_blocked(key):
    """自动重试门禁：已在同步 / 远端锁占用 / 冷却期 / 超时 / 孤儿愈合 → 跳过。
    返回 (blocked: bool, reason: str)。"""
    with lock:
        st = (sync_data.get(key) or {}).get("status")
    if st == "syncing":
        return True, "已在同步中"
    if st == "timeout":
        return True, "状态为超时，交由监控续跑，跳过自动重试"
    # 远端 flock 仍占用：纠正为 syncing，禁止 Agent 再 TRIGGER（避免 ansible 多节点踩踏）
    try:
        state, detail = _align_status_if_remote_running(key)
        if state == "running":
            return True, f"远端仍在同步（{detail}），跳过重试"
    except Exception:
        pass
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT status, started_at, finished_at, output_summary, node "
                "FROM sync_history WHERE key=? ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
        if not row:
            return False, ""
        out = (row["output_summary"] or "")
        node = (row["node"] or "")
        if "orphan syncing healed" in out or node == "heal-local":
            return True, "孤儿愈合标记，跳过自动重试"
        if row["status"] == "timeout" or _last_failure_was_timeout(key):
            return True, "上次为超时，跳过重试(交由监控续跑)"
        ts = (row["finished_at"] or row["started_at"] or "")[:19]
        try:
            age = (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).total_seconds()
            if age >= 0 and age < AGENT_RETRY_COOLDOWN_SEC:
                mins = max(1, int((AGENT_RETRY_COOLDOWN_SEC - age) / 60))
                return True, f"冷却中，约 {mins} 分钟后可自动重试"
        except Exception:
            pass
    except Exception:
        pass
    return False, ""


def _agent_run_inspect(auto_retry=None):
    """执行一轮巡检：统计状态 → 自动重试失败镜像 → AI 诊断 → 返回结构化报告。
    auto_retry: None 用 AGENT_AUTO_RETRY 配置；True/False 强制覆盖。"""
    if auto_retry is None:
        auto_retry = AGENT_AUTO_RETRY
    mirrors = _agent_list_mirrors()
    counts = {"total": len(mirrors), "success": 0, "fail": 0, "timeout": 0, "syncing": 0, "pending": 0, "other": 0}
    failed = []
    for m in mirrors:
        s = m["status"]
        if s in counts:
            counts[s] += 1
        else:
            counts["other"] += 1
        # 仅真实 fail 进入自动重试池；timeout 交 cron
        if s == "fail":
            failed.append(m["key"])

    # 自动重试失败镜像（带冷却 / 超时 / 孤儿愈合门禁）
    actions = []
    retried = []
    if auto_retry and failed:
        for key in failed:
            blocked, reason = _agent_auto_retry_blocked(key)
            if blocked:
                actions.append({"type": "skip-cooldown", "key": key, "ok": False, "msg": reason})
                continue
            r = _agent_trigger_sync(key)
            actions.append({"type": "retry", "key": key, "ok": r.get("ok", False), "msg": r.get("error") or "已触发重试"})
            if r.get("ok"):
                retried.append(key)

    # AI 诊断（仅在仍有失败或曾失败时调用，避免无谓消耗）
    diagnosis = ""
    ai_ok = True
    if failed:
        try:
            ctx = _agent_build_context(failed)
            sys_prompt = (
                "你是 TIBMirrorMgr 镜像站的 AI 运维助手，正在自动巡检。下面是镜像站实时状态与失败镜像的同步历史、日志。"
                "请基于这些真实数据分析：1) 失败镜像的根因（结合返回码/报错/日志判断，如网络超时、上游不可达、磁盘满、命令错误等）；"
                "2) 每个失败镜像的修复建议或重试建议。用中文，按镜像分条，简洁专业，不要编造上下文里没有的信息。"
                + ("（注意：部分失败镜像已被自动触发重试）" if retried else "")
            )
            diagnosis = _agent_call_claude(sys_prompt, ctx, max_tokens=1800)
        except Exception as e:
            ai_ok = False
            diagnosis = f"AI 诊断失败：{e}\n\n【原始上下文】\n{_agent_build_context(failed)}"

    issues = [{"key": k, "status": "fail"} for k in failed]
    return {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "failed": failed,
        "issues": issues,
        "actions": actions,
        "retried": retried,
        "diagnosis": diagnosis,
        "ai_ok": ai_ok,
    }

@app.route('/api/admin/agent/inspect', methods=['POST'])
@login_required
def admin_agent_inspect():
    """触发一轮 AI 巡检。query ?retry=1/0 强制覆盖自动重试开关。返回巡检报告。"""
    auto_retry = request.args.get("retry")
    if auto_retry == "1":
        auto_retry = True
    elif auto_retry == "0":
        auto_retry = False
    else:
        auto_retry = None
    try:
        report = _agent_run_inspect(auto_retry=auto_retry)
        with _inspect_lock:
            _last_inspect["data"] = report
            _last_inspect["at"] = time.time()
        return jsonify(report)
    except Exception as e:
        return jsonify({"error": f"巡检失败: {e}"}), 500

@app.route('/api/admin/agent/inspect', methods=['GET'])
@login_required
def admin_agent_inspect_last():
    """获取最近一次巡检结果（不重新跑）。"""
    with _inspect_lock:
        data = _last_inspect.get("data")
    if not data:
        return jsonify({"empty": True, "message": "尚无巡检记录，点击“重新巡检”开始"})
    return jsonify(data)

@app.route('/api/admin/agent/sync/<key>', methods=['POST'])
@login_required
def admin_agent_sync(key):
    """手动触发某镜像同步（供巡检页对失败镜像一键重试）。"""
    r = _agent_trigger_sync(key)
    code = 200 if r.get("ok") else 400
    return jsonify(r), code

@app.route('/api/admin/agent/logs', methods=['GET'])
@login_required
def admin_agent_logs():
    """同步日志（app.log + app-02.log），供 AI AGENT 页展示。query: ?keyword=&n="""
    keyword = request.args.get("keyword") or ""
    try:
        n = max(10, min(int(request.args.get("n") or 200), 500))
    except ValueError:
        n = 200
    return jsonify(_agent_sync_logs(keyword=keyword, n=n))

# ====================== AI 技能 Agent（像真人运维一样自主排查处置） ======================
# 技能分两级：read 只读自主执行；write 会改变系统状态，执行后写审计日志。
AGENT_SKILL_MAX_STEPS = int(os.environ.get("AGENT_SKILL_MAX_STEPS", "10"))
AGENT_SKILL_BUDGET_SEC = int(os.environ.get("AGENT_SKILL_BUDGET_SEC", "180"))

def _probe_all_upstreams():
    """并发探测已纳管且启用的上游源，返回完整条目列表。"""
    sources = [s for s in get_managed_upstreams() if s.get("enabled", True)]
    if not sources:
        return []
    results = [None] * len(sources)
    links = _upstream_mirror_links()

    def _do(i, src):
        probe_url = (src.get("url") or "").strip()
        row = dict(src)
        row["mirrors"] = links.get((src.get("host") or "").lower(), [])
        # github 同时匹配 api.github.com
        if (src.get("host") or "").lower() == "github.com":
            extra = links.get("api.github.com", [])
            seen = {m["key"] for m in row["mirrors"]}
            for m in extra:
                if m["key"] not in seen:
                    row["mirrors"].append(m)
        if not probe_url:
            row.update({"latency_ms": None, "http_code": 0, "ok": False, "timeout": False, "probe_skipped": True})
        else:
            row.update(_probe_upstream(probe_url))
            row["probe_skipped"] = False
        results[i] = row

    threads = [threading.Thread(target=_do, args=(i, s)) for i, s in enumerate(sources)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    for i, r in enumerate(results):
        if r is None:
            results[i] = {**sources[i], "latency_ms": None, "http_code": 0,
                          "ok": False, "timeout": True, "mirrors": [], "probe_skipped": False}
    return results

# ---------- 技能实现（入参 dict，返回可 JSON 序列化结果） ----------
def _skill_list_mirrors(a):
    status = (a.get("status") or "").strip().lower()
    ms = _agent_list_mirrors()
    if status:
        ms = [m for m in ms if m["status"] == status]
    return {"count": len(ms), "mirrors": ms}

def _skill_mirror_detail(a):
    key = (a.get("key") or "").strip()
    if not key or key not in sync_data:
        return {"error": f"无效镜像 key: {key or '(空)'}，可先用 list_mirrors 查看全部 key"}
    m = next((x for x in _agent_list_mirrors() if x["key"] == key), None)
    return {"mirror": m, "recent_history": _agent_sync_history(key=key, limit=5)}

def _skill_sync_history(a):
    return {"items": _agent_sync_history(key=(a.get("key") or "").strip(),
                                         status=(a.get("status") or "").strip(),
                                         limit=a.get("limit", 10))}

def _skill_sync_logs(a):
    return _agent_sync_logs(keyword=(a.get("keyword") or "").strip(), n=a.get("n", 80))

def _skill_disk_usage(a):
    try:
        r = subprocess.run(["df", "-h"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=10)
        disks = []
        for line in r.stdout.strip().split("\n")[1:]:
            p = line.split()
            if len(p) < 6:
                continue
            try:
                pct = int(p[4].replace("%", ""))
            except Exception:
                pct = 0
            disks.append({"filesystem": p[0], "size": p[1], "used": p[2], "avail": p[3],
                          "use_percent": pct, "mount": p[-1]})
        return {"disks": disks}
    except Exception as e:
        return {"error": str(e)}

def _skill_check_upstreams(a):
    return {"upstreams": _probe_all_upstreams()}

def _skill_recent_alerts(a):
    alerts = []
    with lock:
        for k, v in sync_data.items():
            if v.get("status") == "fail":
                alerts.append({"level": "error", "type": "sync_fail", "key": k,
                               "name": v.get("name", k), "last_sync": v.get("last_sync_date")})
    for x in _skill_disk_usage({}).get("disks", []):
        if x["use_percent"] >= 85 and not x["mount"].startswith(("/dev", "/run", "/proc", "/sys", "/boot")):
            alerts.append({"level": "warn", "type": "disk_high", "mount": x["mount"],
                           "use_percent": x["use_percent"]})
    return {"count": len(alerts), "alerts": alerts[:20]}

def _skill_list_crontab(a):
    raw, err, status = _fetch_crontab_raw()
    if err:
        return {"error": err, "http_status": status}
    tasks = _parse_crontab_tasks(raw)
    slim = [{
        "id": t["id"], "schedule": t.get("schedule"),
        "minute": t["minute"], "hour": t["hour"], "dom": t["dom"],
        "month": t["month"], "dow": t["dow"],
        "mirror_key": t.get("mirror_key") or "",
        "mirror_name": t.get("mirror_name") or "",
        "sync_script": t.get("sync_script") or "",
        "executor": t.get("executor") or "other",
        "note": t.get("note") or "",
        "command": (t.get("command") or "")[:180],
    } for t in tasks]
    ex_filter = (a.get("executor") or "").strip()
    if ex_filter:
        slim = [t for t in slim if t["executor"] == ex_filter]
    key_filter = (a.get("key") or "").strip()
    if key_filter:
        slim = [t for t in slim if t["mirror_key"] == key_filter
                or key_filter in (t.get("sync_script") or "")]
    return {"count": len(slim), "tasks": slim}


def _skill_recommend_executor(a):
    key = (a.get("key") or "").strip()
    if not key:
        return {"error": "需要 key"}
    if key not in sync_data:
        return {"error": f"无效镜像 key: {key}"}
    return _recommend_sync_executor(key)


def _skill_upsert_crontab(a):
    """创建或更新某镜像的定时任务；executor=auto 时由规则推荐。"""
    key = (a.get("key") or "").strip()
    if not key or key not in sync_data:
        return {"error": f"无效镜像 key: {key or '(空)'}"}
    item = sync_data.get(key) or {}
    sync_script = (a.get("sync_script") or item.get("sync_script") or "").strip()
    if not sync_script:
        return {"error": f"镜像 {key} 未配置 sync_script，无法写 crontab"}

    executor = (a.get("executor") or "auto").strip() or "auto"
    reason = ""
    if executor == "auto":
        rec = _recommend_sync_executor(key)
        executor = rec["executor"]
        reason = rec.get("reason") or ""
    if executor not in ("wrapper", "ansible"):
        return {"error": "executor 仅支持 auto/wrapper/ansible"}

    minute = str(a.get("minute", "0")).strip()
    hour = str(a.get("hour", "2")).strip()
    dom = str(a.get("dom", "*")).strip()
    month = str(a.get("month", "*")).strip()
    dow = str(a.get("dow", "*")).strip()
    for name, val in (("minute", minute), ("hour", hour), ("dom", dom),
                      ("month", month), ("dow", dow)):
        _, e = _validate_cron_field(name, val)
        if e:
            return {"error": e}

    raw, err, status = _fetch_crontab_raw()
    if err:
        return {"error": err}
    tasks = _parse_crontab_tasks(raw)
    existing = next((t for t in tasks if t.get("mirror_key") == key), None)
    if not existing:
        base = sync_script.split("/")[-1]
        existing = next((t for t in tasks
                         if (t.get("sync_script") or "").split("/")[-1] == base), None)

    time_explicit = any(k in a for k in ("minute", "hour", "dom", "month", "dow"))
    try:
        if existing:
            if not time_explicit:
                minute, hour = existing["minute"], existing["hour"]
                dom, month, dow = existing["dom"], existing["month"], existing["dow"]
            cmd = _rebuild_cron_command(existing["command"], sync_script, executor=executor)
            new_line = f"{minute} {hour} {dom} {month} {dow} {cmd}"
            lines = (raw or "").splitlines()
            line_no = existing["line_no"]
            if line_no < 0 or line_no >= len(lines):
                return {"error": "行号失效，请重试"}
            lines[line_no] = new_line
            content = "\n".join(lines)
            if raw.endswith("\n"):
                content += "\n"
            ok, msg, st, backup = _write_crontab_raw(content, raw)
            if not ok:
                return {"error": msg}
            return {"ok": True, "action": "updated", "key": key, "executor": executor,
                    "reason": reason, "line": new_line, "message": msg}
        cmd = _rebuild_cron_command("", sync_script, executor=executor)
        note = f"{item.get('name') or key} ({executor}) by AI"
        lines = (raw or "").splitlines()
        block = [f"# {note}", f"{minute} {hour} {dom} {month} {dow} {cmd}"]
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
        content = "\n".join(lines)
        if not content.endswith("\n"):
            content += "\n"
        ok, msg, st, backup = _write_crontab_raw(content, raw)
        if not ok:
            return {"error": msg}
        return {"ok": True, "action": "created", "key": key, "executor": executor,
                "reason": reason, "line": block[-1], "message": msg}
    except ValueError as ve:
        return {"error": str(ve)}


def _skill_list_playbooks(a):
    r = _ansible_ssh(
        f"cd {ANSIBLE_DIR} && ls -1 playbooks/*.yml playbooks/*.yaml 2>/dev/null",
        timeout=15)
    names = []
    for line in (r.stdout or "").splitlines():
        fn = line.strip()
        if fn:
            names.append(os.path.basename(fn))
    return {"count": len(names), "playbooks": names,
            "hint": "镜像同步定时请用 upsert_crontab(executor=auto)；"
                    "run_sync.yml 由 ai_dispatch 内部调用，一般不必直接定时 playbook"}


def _skill_run_playbook(a):
    playbook = (a.get("playbook") or "").strip()
    lim = (a.get("limit") or "").strip()
    sync_script = (a.get("sync_script") or "").strip()
    key = (a.get("key") or "").strip()
    if key and not sync_script:
        sync_script = ((sync_data.get(key) or {}).get("sync_script") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", playbook):
        return {"error": "playbook 非法"}
    if not lim:
        lim = "auto" if _is_sync_playbook(playbook) else "bnode"
    if lim == "auto" and not _is_sync_playbook(playbook):
        lim = "bnode"
    if not _is_allowed_ansible_limit(lim):
        return {"error": "limit 非法，允许: auto / bnode1-30 / 分组(all,bnode,web,…)"}
    # 镜像同步：一律 ai_dispatch（flock + report）；显式 bnodeN 用 --node，禁止裸 ansible
    if _is_sync_playbook(playbook):
        if not sync_script and not key:
            return {"error": "run_sync.yml 需要 key 或 sync_script"}
        lim2, err, redirect = _resolve_run_sync_limit(lim, sync_script, key)
        if err:
            return {"error": err}
        preferred = lim2 if lim2 != "auto" else None
        if key and not preferred:
            ret = _agent_trigger_sync(key, executor="ansible")
            if isinstance(ret, dict):
                ret["playbook"] = playbook
                ret["limit"] = "auto"
                ret["routed"] = "ai_dispatch"
                ret.setdefault("message", "已交 ai_dispatch（单节点，按体积分配）")
            return ret
        if not sync_script and key:
            sync_script = ((sync_data.get(key) or {}).get("sync_script") or "").strip()
        ok, detail = _launch_ai_dispatch(sync_script, preferred_node=preferred)
        if not ok:
            return {"error": detail}
        detail = dict(detail)
        detail["playbook"] = playbook
        if key:
            detail["key"] = key
        return detail

    extra_vars = None
    if sync_script:
        try:
            sync_script = _normalize_sync_script_arg(sync_script)
        except ValueError as ve:
            return {"error": str(ve)}
        extra_vars = {"sync_script": sync_script}
    chk = _ansible_ssh(f"test -f {ANSIBLE_DIR}/playbooks/{playbook} && echo OK", timeout=10)
    if (chk.stdout or "").strip() != "OK":
        return {"error": f"playbook 不存在: {playbook}"}
    run_id = "ar-" + uuid.uuid4().hex[:12]
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    by = "ai-skill-agent"
    rec = {"run_id": run_id, "playbook": playbook, "lim": lim, "status": "running",
           "started_at": started, "finished_at": "", "rc": None, "output": "",
           "triggered_by": by, "sync_script": sync_script or ""}
    with _ansible_runs_lock:
        _ansible_runs[run_id] = rec
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO ansible_runs(run_id,playbook,lim,status,started_at,triggered_by,output) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, playbook, lim, "running", started, by, ""))
            conn.commit()
    except Exception as e:
        return {"error": f"写库失败: {e}"}
    threading.Thread(target=_run_ansible, args=(run_id, playbook, lim, extra_vars), daemon=True).start()
    return {"ok": True, "run_id": run_id, "playbook": playbook, "limit": lim,
            "sync_script": sync_script or None, "message": "已开始异步执行"}


def _skill_trigger_sync(a):
    return _agent_trigger_sync((a.get("key") or "").strip(),
                               executor=(a.get("executor") or "auto"))

# ---------- 技能注册表（Anthropic tool use 格式） ----------
_AGENT_SKILLS = [
    {"name": "list_mirrors", "kind": "read", "fn": _skill_list_mirrors,
     "description": "列出镜像站全部镜像概况（key/名称/状态/大小/最后同步/定时表达式）。可选按状态过滤 success/fail/syncing/pending。",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "description": "可选，按状态过滤"}}, "required": []}},
    {"name": "mirror_detail", "kind": "read", "fn": _skill_mirror_detail,
     "description": "查单个镜像的详情与最近 5 条同步历史（含失败原因摘要）。排查具体镜像时优先用它。",
     "input_schema": {"type": "object", "properties": {
         "key": {"type": "string", "description": "镜像 key，如 pdb"}}, "required": ["key"]}},
    {"name": "sync_history", "kind": "read", "fn": _skill_sync_history,
     "description": "查同步历史记录，可按镜像 key 和状态（success/fail/skip）过滤，含耗时与输出摘要。",
     "input_schema": {"type": "object", "properties": {
         "key": {"type": "string", "description": "可选，镜像 key"},
         "status": {"type": "string", "description": "可选，状态过滤"},
         "limit": {"type": "integer", "description": "条数，默认 10，最多 50"}}, "required": []}},
    {"name": "sync_logs", "kind": "read", "fn": _skill_sync_logs,
     "description": "读同步进程日志尾部（app.log），可按关键词（镜像 key/报错词）过滤。定位具体报错时用。",
     "input_schema": {"type": "object", "properties": {
         "keyword": {"type": "string", "description": "可选，过滤关键词"},
         "n": {"type": "integer", "description": "行数，默认 80，最多 500"}}, "required": []}},
    {"name": "disk_usage", "kind": "read", "fn": _skill_disk_usage,
     "description": "查服务器磁盘使用率（df 真实数据），判断是否磁盘满导致同步失败。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "check_upstreams", "kind": "read", "fn": _skill_check_upstreams,
     "description": "实时探测全部上游镜像源的可达性与延迟，判断是否上游故障。耗时约 3-15 秒。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "recent_alerts", "kind": "read", "fn": _skill_recent_alerts,
     "description": "获取当前告警（同步失败镜像 + 磁盘超阈值），快速掌握全局健康状况。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "list_crontab", "kind": "read", "fn": _skill_list_crontab,
     "description": "列出同步调度机 root crontab：时间、镜像、执行方式(wrapper本机/ansible分发)。可按 key 或 executor 过滤。",
     "input_schema": {"type": "object", "properties": {
         "key": {"type": "string", "description": "可选，镜像 key"},
         "executor": {"type": "string", "description": "可选，wrapper|ansible|other"}}, "required": []}},
    {"name": "recommend_executor", "kind": "read", "fn": _skill_recommend_executor,
     "description": "根据体积/名单/历史耗时，推荐本机 wrapper 或 Ansible(ai_dispatch→bnode)。改定时或触发同步前应先看推荐。",
     "input_schema": {"type": "object", "properties": {
         "key": {"type": "string", "description": "镜像 key"}}, "required": ["key"]}},
    {"name": "list_playbooks", "kind": "read", "fn": _skill_list_playbooks,
     "description": "列出 Ansible playbooks。镜像同步定时请用 upsert_crontab，不要直接把 run_sync.yml 写进 crontab。",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "trigger_sync", "kind": "write", "fn": _skill_trigger_sync,
     "description": "【写操作】立即触发同步。executor=auto(默认，规则选 wrapper/ansible)|wrapper|ansible。确认可恢复时再用。",
     "input_schema": {"type": "object", "properties": {
         "key": {"type": "string", "description": "镜像 key"},
         "executor": {"type": "string", "description": "auto|wrapper|ansible，默认 auto"}},
         "required": ["key"]}},
    {"name": "upsert_crontab", "kind": "write", "fn": _skill_upsert_crontab,
     "description": "【写操作】创建或更新镜像的 crontab。executor=auto(默认)时由规则在 wrapper/ansible 间选择并写回调度机。可指定 minute/hour/dom/month/dow；不传时间则更新保留原时间、新建默认 0 2 * * *。",
     "input_schema": {"type": "object", "properties": {
         "key": {"type": "string", "description": "镜像 key"},
         "executor": {"type": "string", "description": "auto|wrapper|ansible，默认 auto"},
         "minute": {"type": "string", "description": "分，默认新建为 0"},
         "hour": {"type": "string", "description": "时，默认新建为 2"},
         "dom": {"type": "string", "description": "日，默认 *"},
         "month": {"type": "string", "description": "月，默认 *"},
         "dow": {"type": "string", "description": "周，默认 *"},
         "sync_script": {"type": "string", "description": "可选，覆盖镜像默认脚本"}},
         "required": ["key"]}},
    {"name": "run_playbook", "kind": "write", "fn": _skill_run_playbook,
     "description": "【写操作】即时执行 Ansible playbook。run_sync.yml 强制走 ai_dispatch 只选 1 台（按体积），禁止 -l bnode 打满 30 台；镜像同步优先 trigger_sync。运维探测(ping.yml)可用整组。",
     "input_schema": {"type": "object", "properties": {
         "playbook": {"type": "string", "description": "如 ping.yml / run_sync.yml"},
         "limit": {"type": "string", "description": "auto|bnodeN|分组；run_sync 默认 auto"},
         "key": {"type": "string", "description": "可选，镜像 key"},
         "sync_script": {"type": "string", "description": "可选，同步脚本路径"}},
         "required": ["playbook"]}},
]
_AGENT_SKILL_BY_NAME = {s["name"]: s for s in _AGENT_SKILLS}

_AGENT_COMMON_RULES = (
    "规则："
    "1) 只依据技能返回的真实数据分析，严禁编造数字；"
    "2) 写操作须先查清再执行，并在结论中说明做了什么；"
    "3) 每轮要么调工具、要么给最终结论；最终用中文 Markdown：先结论后依据。"
)

# 多 Agent：各自裁剪技能集 + 人设，前端按 id 切换
_AGENT_PROFILES = {
    "inspector": {
        "id": "inspector",
        "name": "巡检",
        "icon": "heart",
        "blurb": "健康检查 · 失败根因 · 磁盘/上游",
        "skills": [
            "list_mirrors", "mirror_detail", "sync_history", "sync_logs",
            "disk_usage", "check_upstreams", "recent_alerts", "trigger_sync",
        ],
        "presets": [
            "最近有哪些镜像失败？分析根因",
            "检查磁盘和上游是否正常",
            "整体健康状况，有问题直接处理",
        ],
        "system": (
            "你是镜像站「巡检 Agent」。专注健康检查与故障诊断。"
            "优先 recent_alerts / disk_usage / check_upstreams / sync_history；"
            "可恢复失败可用 trigger_sync(executor=auto)；超大库超时勿盲目重试。"
            + _AGENT_COMMON_RULES
        ),
    },
    "scheduler": {
        "id": "scheduler",
        "name": "调度",
        "icon": "timer",
        "blurb": "crontab · wrapper/Ansible 选型",
        "skills": [
            "list_mirrors", "mirror_detail", "list_crontab", "recommend_executor",
            "upsert_crontab", "trigger_sync",
        ],
        "presets": [
            "查看当前 crontab，大库改 Ansible，小库保持本机",
            "给 conda/pypi 用推荐执行方式写每天凌晨定时",
            "推荐 pdb 该用本机还是 Ansible",
        ],
        "system": (
            "你是镜像站「调度 Agent」。负责 crontab 与执行器选型。"
            "小镜像/短任务→wrapper；大库/高耗时→ansible(ai_dispatch→bnode)。"
            "改定时前 recommend_executor，或 upsert_crontab(executor=auto)；"
            "不要把 ansible-playbook run_sync.yml 直接写进 crontab。"
            + _AGENT_COMMON_RULES
        ),
    },
    "ansible": {
        "id": "ansible",
        "name": "Ansible",
        "icon": "term",
        "blurb": "playbook 即时执行 · 分发同步",
        "skills": [
            "list_playbooks", "list_mirrors", "recommend_executor",
            "run_playbook", "trigger_sync", "mirror_detail",
        ],
        "presets": [
            "列出可用 playbook",
            "用 Ansible 触发一次 conda 同步",
            "在 bnode 上跑 ping.yml 探测连通",
        ],
        "system": (
            "你是镜像站「Ansible Agent」。负责 playbook 即时执行与分发同步。"
            "运维探测用 run_playbook(ping.yml)，可 -l bnode 整组；"
            "镜像同步必须用 trigger_sync(executor=ansible/auto)，由 ai_dispatch 按体积分档只选 1 台节点；"
            "严禁 run_playbook(run_sync.yml, limit=bnode) 打满 30 台（共享 NFS 会踩踏）。"
            "日常定时不归你管，让用户去调度 Agent。"
            + _AGENT_COMMON_RULES
        ),
    },
    "ops": {
        "id": "ops",
        "name": "综合",
        "icon": "spark",
        "blurb": "全技能编排 · 复杂跨域问题",
        "skills": [s["name"] for s in _AGENT_SKILLS],
        "presets": [
            "巡检并优化失败大库的定时执行方式",
            "磁盘紧张时哪些同步该停或错峰",
        ],
        "system": (
            "你是镜像站「综合运维 Agent」，可调用全部技能跨域编排。"
            "调度选型：小→wrapper，大→ansible；定时用 upsert_crontab；"
            "运维 playbook 用 run_playbook。一次对话最多 trigger_sync×3、"
            "upsert_crontab×5、run_playbook×2。"
            + _AGENT_COMMON_RULES
        ),
    },
}


def _resolve_agent_profile(agent_id):
    aid = (agent_id or "ops").strip().lower()
    return _AGENT_PROFILES.get(aid) or _AGENT_PROFILES["ops"]


def _skill_watch_hint(skill_name, args, out):
    """从技能结果推导前端可挂载的实时监控卡片（ansible run / 同步日志）。"""
    if not isinstance(out, dict) or out.get("error"):
        return None
    args = args or {}
    if skill_name == "run_playbook" and out.get("run_id"):
        return {
            "kind": "ansible",
            "run_id": out["run_id"],
            "playbook": out.get("playbook") or args.get("playbook") or "",
            "limit": out.get("limit") or args.get("limit") or "bnode",
            "title": "Ansible · %s" % (out.get("playbook") or args.get("playbook") or out["run_id"]),
        }
    if skill_name == "trigger_sync" and (out.get("ok") or out.get("message")):
        key = (args.get("key") or out.get("key") or "").strip()
        if key:
            return {
                "kind": "sync",
                "key": key,
                "executor": out.get("chosen_executor") or args.get("executor") or "auto",
                "title": "同步 · %s" % key,
            }
    if skill_name == "sync_logs":
        kw = (args.get("keyword") or "").strip()
        return {
            "kind": "logs",
            "keyword": kw,
            "title": "日志%s" % ((" · " + kw) if kw else ""),
        }
    return None


def _agent_skill_loop_gen(question, max_steps=None, budget_sec=None, agent_id="ops"):
    """技能循环事件流：status / skill / skill_result / token / done / error。"""
    profile = _resolve_agent_profile(agent_id)
    skill_names = set(profile["skills"])
    skills = [s for s in _AGENT_SKILLS if s["name"] in skill_names]
    max_steps = max_steps or AGENT_SKILL_MAX_STEPS
    budget_sec = budget_sec or AGENT_SKILL_BUDGET_SEC
    started = time.time()
    messages = [{"role": "system", "content": profile["system"]},
                {"role": "user", "content": question}]
    trace = []
    tools_payload = [{"type": "function", "function": {
        "name": s["name"], "description": s["description"],
        "parameters": s["input_schema"]}} for s in skills]
    truncated = False
    answer = ""

    yield {"type": "status", "text": "%s 开始分析…" % profile["name"],
           "agent": profile["id"], "agent_name": profile["name"]}

    try:
        for _ in range(max_steps):
            if time.time() - started > budget_sec:
                truncated = True
                break
            yield {"type": "status", "text": "思考中…"}
            msg = _agent_openai_call(messages, tools=tools_payload)
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                answer = _strip_think(msg.get("content") or "")
                if answer:
                    break
                messages.append({"role": "user", "content": "请继续：调用技能或直接给出结论。"})
                continue
            messages.append({"role": "assistant",
                             "content": msg.get("content") or "",
                             "tool_calls": tool_calls})
            for tc in tool_calls:
                fn = tc.get("function") or {}
                cid = tc.get("id") or f"call_{len(trace)}"
                if len(trace) >= max_steps:
                    messages.append({"role": "tool", "tool_call_id": cid,
                                     "content": '{"error": "已达步数上限，请直接给出结论"}'})
                    truncated = True
                    continue
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (ValueError, TypeError):
                    args = {}
                yield {"type": "skill", "step": len(trace) + 1, "skill": name, "args": args}
                skill = _AGENT_SKILL_BY_NAME.get(name)
                entry = {"step": len(trace) + 1, "skill": name, "args": args,
                         "agent": profile["id"]}
                if not skill or name not in skill_names:
                    out = {"error": "当前 Agent「%s」未装备技能: %s" % (profile["name"], name)}
                else:
                    try:
                        out = skill["fn"](args)
                        if skill["kind"] == "write":
                            audit_log("agent_skill", target=name,
                                      detail={"args": args, "by": "ai-skill-agent",
                                              "agent": profile["id"]})
                    except Exception as e:
                        out = {"error": "技能执行异常: %s" % e}
                entry["result_brief"] = json.dumps(out, ensure_ascii=False, default=str)[:200]
                watch = _skill_watch_hint(name, args, out if isinstance(out, dict) else {})
                if watch:
                    entry["watch"] = watch
                trace.append(entry)
                # 前端对话栏可据此挂载实时日志监控卡片
                preview = out
                if isinstance(out, dict):
                    preview = {k: v for k, v in out.items()
                               if k not in ("lines",) and not (isinstance(v, str) and len(v) > 1200)}
                    for tag in ("app", "app-02"):
                        if tag in preview and isinstance(preview[tag], dict):
                            lines = preview[tag].get("lines") or []
                            preview[tag] = {
                                "count": preview[tag].get("count", len(lines)),
                                "error": preview[tag].get("error"),
                                "tail": lines[-8:] if lines else [],
                            }
                yield {
                    "type": "skill_result",
                    "step": entry["step"],
                    "skill": name,
                    "args": args,
                    "ok": not (isinstance(out, dict) and out.get("error")),
                    "result": preview if isinstance(preview, dict) else {"raw": str(preview)[:800]},
                    "watch": watch,
                }
                messages.append({"role": "tool", "tool_call_id": cid,
                                 "content": json.dumps(out, ensure_ascii=False, default=str)[:8000]})
            if truncated:
                break

        if not answer:
            truncated = True
            yield {"type": "status", "text": "汇总结论…"}
            messages.append({"role": "user",
                             "content": "已达到本次排查的步数上限，请直接根据以上已收集的信息给出结论。"})
            msg = _agent_openai_call(messages, tools=None, max_tokens=2000)
            answer = _strip_think(msg.get("content") or "") or "（模型未返回结论）"

        yield {"type": "status", "text": "生成回复…"}
        for ch in answer:
            yield {"type": "token", "t": ch}
        yield {
            "type": "done",
            "steps": len(trace),
            "trace": trace,
            "truncated": truncated,
            "agent": profile["id"],
            "agent_name": profile["name"],
            "answer": answer,
        }
    except Exception as e:
        yield {"type": "error", "error": str(e)}


def _agent_skill_loop(question, max_steps=None, budget_sec=None, agent_id="ops"):
    """非流式包装：收集生成器结果。"""
    result = {"answer": "", "steps": 0, "trace": [], "truncated": False,
              "agent": agent_id, "agent_name": ""}
    for ev in _agent_skill_loop_gen(question, max_steps=max_steps,
                                    budget_sec=budget_sec, agent_id=agent_id):
        t = ev.get("type")
        if t == "done":
            result.update({
                "answer": ev.get("answer") or "",
                "steps": ev.get("steps") or 0,
                "trace": ev.get("trace") or [],
                "truncated": bool(ev.get("truncated")),
                "agent": ev.get("agent") or agent_id,
                "agent_name": ev.get("agent_name") or "",
            })
        elif t == "error":
            raise RuntimeError(ev.get("error") or "AI 失败")
        elif t == "status" and ev.get("agent_name"):
            result["agent"] = ev.get("agent") or result["agent"]
            result["agent_name"] = ev.get("agent_name") or result["agent_name"]
    return result

def _agent_chat_ensure_session(session_id, agent_id, title, user):
    """确保会话存在；返回 session_id。"""
    sid = (session_id or "").strip() or ("as-" + uuid.uuid4().hex[:16])
    title = (title or "新对话").strip()[:80] or "新对话"
    agent_id = (agent_id or "ops").strip()
    user = (user or "admin").strip()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as conn:
        row = conn.execute("SELECT id FROM agent_sessions WHERE id=?", (sid,)).fetchone()
        if row:
            conn.execute(
                "UPDATE agent_sessions SET agent=?, updated_at=? WHERE id=?",
                (agent_id, now, sid))
        else:
            conn.execute(
                "INSERT INTO agent_sessions(id,agent,title,user,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (sid, agent_id, title, user, now, now))
        conn.commit()
    return sid


def _agent_chat_add_message(session_id, role, content, agent="", skills=None, steps=0):
    if not session_id or not role:
        return
    content = content or ""
    skills_json = json.dumps(skills or [], ensure_ascii=False) if skills is not None else None
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO agent_messages(session_id,role,content,agent,skills,steps,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (session_id, role, content, agent or "", skills_json, int(steps or 0), now))
        conn.execute(
            "UPDATE agent_sessions SET updated_at=? WHERE id=?", (now, session_id))
        # 首条用户消息刷新标题
        if role == "user":
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_messages WHERE session_id=? AND role='user'",
                (session_id,)).fetchone()
            if row and int(row["c"] or 0) <= 1:
                title = content.strip().replace("\n", " ")[:40] or "新对话"
                conn.execute("UPDATE agent_sessions SET title=? WHERE id=?", (title, session_id))
        conn.commit()


def _agent_chat_save_turn(session_id, agent_id, question, answer, trace=None, user="admin"):
    """保存一轮问答到数据库，返回 session_id。skills 含 name + 可选 watch，便于对话栏恢复监控。"""
    title = (question or "").strip().replace("\n", " ")[:40] or "新对话"
    sid = _agent_chat_ensure_session(session_id, agent_id, title, user)
    skills = []
    for t in (trace or []):
        name = t.get("skill")
        if not name:
            continue
        item = {"name": name}
        if t.get("watch"):
            item["watch"] = t["watch"]
        if t.get("args"):
            # 只保留短参数，避免 skills 列膨胀
            brief = {}
            for k, v in (t["args"] or {}).items():
                if v is None or v == "":
                    continue
                s = str(v)
                brief[k] = s if len(s) <= 80 else s[:77] + "..."
                if len(brief) >= 4:
                    break
            if brief:
                item["args"] = brief
        skills.append(item)
    _agent_chat_add_message(sid, "user", question, agent=agent_id)
    _agent_chat_add_message(sid, "assistant", answer or "", agent=agent_id,
                            skills=skills, steps=len(trace or []))
    return sid


@app.route('/api/admin/agent/skills', methods=['GET'])
@login_required
def admin_agent_skills():
    """技能 + Agent 清单（供前端多 Agent UI）。"""
    agent_id = (request.args.get("agent") or "").strip().lower()
    if agent_id and agent_id in _AGENT_PROFILES:
        prof = _AGENT_PROFILES[agent_id]
        allow = set(prof["skills"])
        skills = [{"name": s["name"], "description": s["description"], "kind": s["kind"]}
                  for s in _AGENT_SKILLS if s["name"] in allow]
        return jsonify({"agent": prof["id"], "skills": skills})
    agents = []
    for pid, p in _AGENT_PROFILES.items():
        agents.append({
            "id": p["id"], "name": p["name"], "icon": p["icon"], "blurb": p["blurb"],
            "presets": p["presets"],
            "skills": [{"name": s["name"], "description": s["description"], "kind": s["kind"]}
                       for s in _AGENT_SKILLS if s["name"] in set(p["skills"])],
        })
    return jsonify({
        "agents": agents,
        "skills": [{"name": s["name"], "description": s["description"], "kind": s["kind"]}
                   for s in _AGENT_SKILLS],
    })


@app.route('/api/admin/agent/sessions', methods=['GET'])
@login_required
def admin_agent_sessions():
    """列最近对话会话。"""
    limit = max(1, min(int(request.args.get("limit") or 50), 200))
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, agent, title, user, created_at, updated_at FROM agent_sessions "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM agent_messages WHERE session_id=?",
                (d["id"],)).fetchone()
            d["message_count"] = int(cnt["c"] or 0) if cnt else 0
            items.append(d)
    return jsonify({"sessions": items, "count": len(items)})


@app.route('/api/admin/agent/sessions/<sid>', methods=['GET'])
@login_required
def admin_agent_session_detail(sid):
    """取某会话全部消息。"""
    with db_conn() as conn:
        s = conn.execute(
            "SELECT id, agent, title, user, created_at, updated_at FROM agent_sessions WHERE id=?",
            (sid,)).fetchone()
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        msgs = conn.execute(
            "SELECT id, role, content, agent, skills, steps, created_at FROM agent_messages "
            "WHERE session_id=? ORDER BY id ASC", (sid,)).fetchall()
    messages = []
    for m in msgs:
        d = dict(m)
        if d.get("skills"):
            try:
                raw = json.loads(d["skills"])
            except Exception:
                raw = []
        else:
            raw = []
        # 兼容旧数据：["name", ...] 与新数据：[{name, watch?, args?}, ...]
        names, watches = [], []
        for item in raw or []:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict) and item.get("name"):
                names.append(item["name"])
                if item.get("watch"):
                    watches.append(item["watch"])
        d["skills"] = names
        d["watches"] = watches
        messages.append(d)
    return jsonify({"session": dict(s), "messages": messages})


@app.route('/api/admin/agent/sessions/<sid>', methods=['DELETE'])
@login_required
def admin_agent_session_delete(sid):
    with db_conn() as conn:
        conn.execute("DELETE FROM agent_messages WHERE session_id=?", (sid,))
        conn.execute("DELETE FROM agent_sessions WHERE id=?", (sid,))
        conn.commit()
    audit_log("agent_session_delete", target=sid)
    return jsonify({"message": "已删除"})


@app.route('/api/admin/agent/ask', methods=['POST'])
@login_required
def admin_agent_ask():
    """AI 技能对话：body {question, agent?, session_id?}。"""
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    agent_id = (data.get("agent") or "ops").strip().lower()
    session_id = (data.get("session_id") or "").strip()
    if not q:
        return jsonify({"error": "请输入问题"}), 400
    if len(q) > 500:
        return jsonify({"error": "问题过长（最多 500 字）"}), 400
    if agent_id not in _AGENT_PROFILES:
        return jsonify({"error": f"未知 agent: {agent_id}"}), 400
    try:
        r = _agent_skill_loop(q, agent_id=agent_id)
        user = session.get("admin_user") or session.get("user") or "admin"
        try:
            sid = _agent_chat_save_turn(session_id, agent_id, q, r.get("answer") or "",
                                        trace=r.get("trace"), user=user)
            r["session_id"] = sid
        except Exception as e:
            _log_sync(f"[AI-CHAT] 保存对话失败: {e}")
        audit_log("agent_ask", target=agent_id, detail={"q": q[:200], "steps": r.get("steps")})
        return jsonify(r)
    except Exception as e:
        return jsonify({"error": f"AI 对话失败: {e}"}), 500


@app.route('/api/admin/agent/ask/stream', methods=['POST'])
@login_required
def admin_agent_ask_stream():
    """SSE 流式对话：先推送 skill/status，再逐字推送 token，最后 done；问答写入数据库。"""
    data = request.get_json(silent=True) or {}
    q = (data.get("question") or "").strip()
    agent_id = (data.get("agent") or "ops").strip().lower()
    session_id = (data.get("session_id") or "").strip()
    if not q:
        return jsonify({"error": "请输入问题"}), 400
    if len(q) > 500:
        return jsonify({"error": "问题过长（最多 500 字）"}), 400
    if agent_id not in _AGENT_PROFILES:
        return jsonify({"error": f"未知 agent: {agent_id}"}), 400
    user = session.get("admin_user") or session.get("user") or "admin"

    def generate():
        final_steps = 0
        answer = ""
        trace = []
        try:
            for ev in _agent_skill_loop_gen(q, agent_id=agent_id):
                if ev.get("type") == "done":
                    final_steps = ev.get("steps") or 0
                    answer = ev.get("answer") or ""
                    trace = ev.get("trace") or []
                    try:
                        sid = _agent_chat_save_turn(session_id, agent_id, q, answer,
                                                    trace=trace, user=user)
                        ev = dict(ev)
                        ev["session_id"] = sid
                    except Exception as e:
                        _log_sync(f"[AI-CHAT] 保存对话失败: {e}")
                yield "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n"
            audit_log("agent_ask", target=agent_id,
                      detail={"q": q[:200], "steps": final_steps, "stream": True})
        except Exception as e:
            yield "data: " + json.dumps({"type": "error", "error": str(e)},
                                        ensure_ascii=False) + "\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _agent_inspect_daemon():
    """定时巡检守护线程：每隔 AGENT_INSPECT_INTERVAL 秒自动跑一轮，结果存缓存。"""
    _daemon_beat("agent_inspect")
    time.sleep(60)  # 启动后等 1 分钟再首次巡检，避开启动期
    while True:
        _daemon_beat("agent_inspect")
        try:
            report = _agent_run_inspect()
            with _inspect_lock:
                _last_inspect["data"] = report
                _last_inspect["at"] = time.time()
            _log_sync(f"[AI-AGENT] 定时巡检完成：{report['counts']['total']} 镜像，失败 {report['counts']['fail']}，重试 {len(report.get('retried', []))}")
        except Exception as e:
            _log_sync(f"[AI-AGENT] 定时巡检异常：{e}")
        time.sleep(max(300, AGENT_INSPECT_INTERVAL))

@app.route('/api/admin/check', methods=['GET'])
def admin_check():
    """前端探测当前是否已登录。已登录则附带 CSRF token 供前端写请求使用。"""
    logged_in = bool(session.get("admin_logged_in"))
    login_at = session.get("admin_login_at")
    if logged_in and login_at and (time.time() - login_at) > ADMIN_SESSION_TTL:
        session.clear()
        logged_in = False
    resp = {"logged_in": logged_in}
    if logged_in:
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_hex(16)
        resp["csrf_token"] = session["csrf_token"]
        user = session.get("admin_user") or "admin"
        resp["user"] = user
        resp["login_via"] = "本地账号" if user == "admin" else "AD 域认证"
    return jsonify(resp)

@app.route('/api/admin/mirrors', methods=['GET'])
@login_required
def admin_mirrors_list():
    return jsonify(admin_list_all())

def _health_score(status, last_sync_date, now=None):
    """镜像健康分（后端统一口径，前端只渲染）：
    fail=0 / syncing=60 / pending=40 / 无同步时间=50；按距今天数新鲜度递减。
    返回 (score, days)。"""
    now = now or datetime.now()
    days = None
    if last_sync_date:
        try:
            ls = datetime.strptime(str(last_sync_date)[:19], "%Y-%m-%d %H:%M:%S")
            days = (now - ls).days
        except Exception:
            days = None
    if status == "fail":
        return 0, days
    if status == "timeout":
        # 超时 ≠ 数据损坏：大库常被 wrapper 墙钟杀掉，下次 cron 会续；健康分介于 pending 与 fail
        return 35, days
    if status == "syncing":
        return 60, days
    if status == "pending":
        return 40, days
    if days is None:
        return 50, days
    if days <= 1:
        return 100, days
    if days <= 3:
        return 85, days
    if days <= 7:
        return 70, days
    if days <= 30:
        return 50, days
    return 25, days

@app.route('/api/admin/mirrors/health', methods=['GET'])
@login_required
def admin_mirrors_health():
    """各镜像健康分（后端统一计算）。前端镜像健康度页直接渲染，不再前端硬编码。"""
    rows = admin_list_all()
    now = datetime.now()
    items, stats = [], {"total": 0, "healthy": 0, "fail": 0, "timeout": 0, "syncing": 0, "pending": 0, "stale": 0}
    for r in rows:
        score, days = _health_score(r.get("status"), r.get("last_sync_date"), now)
        stats["total"] += 1
        st = r.get("status")
        if st == "fail":
            stats["fail"] += 1
        elif st == "timeout":
            stats["timeout"] += 1
        elif st == "syncing":
            stats["syncing"] += 1
        elif st == "pending":
            stats["pending"] += 1
        else:
            stats["healthy"] += 1
            if score < 70:
                stats["stale"] += 1
        items.append({
            "key": r.get("key"), "name": r.get("name") or r.get("key"),
            "status": st, "last_sync_date": r.get("last_sync_date"),
            "total_size_gb": r.get("total_size_gb"), "days": days, "score": score,
        })
    items.sort(key=lambda x: x["score"])
    return jsonify({"items": items, "stats": stats})

@app.route('/api/admin/reload-sizes', methods=['POST', 'GET'])
def admin_reload_sizes():
    """8.74 扫描脚本写完 db 后调此接口，把 db 里的 total_size_gb 刷进内存 sync_data。
    无需登录但需本地令牌校验：调用方须在 X-Reload-Token header 带上 RELOAD_TOKEN。"""
    token = request.headers.get("X-Reload-Token", "")
    if not RELOAD_TOKEN or not secrets.compare_digest(token, RELOAD_TOKEN):
        return jsonify({"error": "未授权"}), 401
    try:
        with db_conn() as conn:
            rows = conn.execute("SELECT key, total_size_gb FROM mirrors").fetchall()
        with lock:
            for r in rows:
                k = r["key"]
                if k in sync_data:
                    sync_data[k]["total_size_gb"] = r["total_size_gb"] or "获取中"
        return jsonify({"message": "已刷新大小", "count": len(rows)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/report', methods=['POST'])
def api_report():
    """同步执行节点（8.74 / Ansible 分发的各节点）跑完同步后统一回写入口。
    带 X-Reload-Token 校验，防公网伪造。payload:
      {key, status(success|fail|timeout|skip), size(可选), node(可选), started_at(可选),
       finished_at(可选), output(可选)}
    作用：更新 mirrors 的 status/total_size_gb/last_sync_date + 刷内存 + 写 sync_history。
    这是状态权威源——控制面只接收上报，不自己算大小/状态。
    skip=Admission 并发满跳过：不改 mirrors.status，只记 history 并入 resume_queue。
    若 status=fail 但 output 含「超时」，mirrors 记 pending 并自动续跑；history 记 timeout 供审计。
    前端不应长期停在「同步超时」。"""
    global _loop_last_report_at, _loop_last_report_key
    token = request.headers.get("X-Reload-Token", "")
    if not RELOAD_TOKEN or not secrets.compare_digest(token, RELOAD_TOKEN):
        return jsonify({"error": "未授权"}), 401
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    raw_status = (data.get("status") or "").strip().lower()
    if not key or key not in sync_data:
        return jsonify({"error": "未知镜像 key"}), 400
    if raw_status not in ("success", "fail", "timeout", "skip"):
        return jsonify({"error": "status 必须为 success/fail/timeout/skip"}), 400
    size = (data.get("size") or "").strip()
    node = (data.get("node") or "").strip()
    started_at = (data.get("started_at") or "").strip()
    finished_at = (data.get("finished_at") or "").strip()
    output = (data.get("output") or "")[:2000]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not started_at:
        started_at = now_str
    if not finished_at:
        finished_at = now_str

    # Admission 跳过：保留原 status，只写 history + 入队，避免首页刷「待同步」
    if raw_status == "skip":
        try:
            dur = None
            try:
                dur = int((datetime.now() - datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")).total_seconds())
                if dur < 0:
                    dur = None
            except Exception:
                pass
            name = sync_data.get(key, {}).get("name", key)
            trig = _pop_sync_trigger(key, "schedule", node or "8.74")
            with db_conn() as conn:
                conn.execute(
                    """INSERT INTO sync_history
                       (key, name, status, trigger_type, trigger_by, started_at, finished_at, duration_sec, output_summary, node)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (key, name, "skip", trig["type"], trig["by"], started_at, finished_at, dur, output, node),
                )
                conn.commit()
            invalidate_status_cache()
            _loop_last_report_at = time.time()
            _loop_last_report_key = key
            _resume_queue_done(key, drop=False)
            _schedule_auto_resume(key, delay_sec=60, reason="admission-skip")
            cur = (sync_data.get(key) or {}).get("status") or "unknown"
            _log_sync(f"[{key}] 回写：skip（保留 status={cur}）| 节点 {node or '8.74'}")
            return jsonify({"status": "ok", "normalized": "skip", "history_status": "skip", "kept": cur}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    status, hist_status = _normalize_report_statuses(raw_status, output)
    try:
        dur = None
        try:
            dur = int((datetime.now() - datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")).total_seconds())
            if dur < 0:
                dur = None
        except Exception:
            pass
        name = sync_data.get(key, {}).get("name", key)
        trig = _pop_sync_trigger(key, "schedule", node or "8.74")
        # 1) 落库：mirrors 表用展示状态；history 保留 timeout 审计
        with db_conn() as conn:
            conn.execute(
                """UPDATE mirrors SET status=?, total_size_gb=COALESCE(NULLIF(?, ''), total_size_gb),
                   last_sync_date=?, updated_at=datetime('now','localtime') WHERE key=?""",
                (status, size, finished_at, key),
            )
            conn.execute(
                """INSERT INTO sync_history
                   (key, name, status, trigger_type, trigger_by, started_at, finished_at, duration_sec, output_summary, node)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (key, name, hist_status, trig["type"], trig["by"], started_at, finished_at, dur, output, node),
            )
            conn.commit()
        # 2) 刷内存 sync_data（用户端 /api/status 读内存，即时生效）
        with lock:
            if key in sync_data:
                sync_data[key]["status"] = status
                if size:
                    sync_data[key]["total_size_gb"] = size
                sync_data[key]["last_sync_date"] = finished_at
        # 失效 /api/status 缓存，避免前端在 15s TTL 窗口内仍返回旧状态
        invalidate_status_cache()
        # 闭环探针：记录最近一次权威回写
        _loop_last_report_at = time.time()
        _loop_last_report_key = key
        # 3) 仅真实 fail 告警；超时 → 排队监控续跑（不告警、不展示超时终态）
        if hist_status == "timeout" or (status == "pending" and _is_timeout_message(output)):
            _resume_queue_done(key, drop=False)  # 清 inflight，队列由下面入队
            _schedule_auto_resume(key, delay_sec=45, reason="report-timeout")
        elif status == "fail":
            _resume_queue_done(key, drop=True)
            _notify_async(notify_sync_fail, key, output)
        elif status == "success":
            _resume_queue_done(key, drop=True)
            _notify_async(notify_sync_success, key, output)
        else:
            _resume_queue_done(key, drop=False)
        _log_sync(f"[{key}] 回写：mirror={status} hist={hist_status} | 大小 {size or '未上报'} | 节点 {node or '8.74'}")
        return jsonify({"status": "ok", "normalized": status, "history_status": hist_status}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/mirrors', methods=['POST'])
@login_required
def admin_mirrors_create():
    payload = request.get_json(silent=True) or {}
    item, err = admin_create(payload)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"message": "新增成功", "data": item}), 201

@app.route('/api/admin/mirrors/<key>', methods=['PUT'])
@login_required
def admin_mirrors_update(key):
    payload = request.get_json(silent=True) or {}
    item, err = admin_update(key, payload)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"message": "更新成功", "data": item}), 200

@app.route('/api/admin/mirrors/<key>', methods=['DELETE'])
@login_required
def admin_mirrors_delete(key):
    ok, err = admin_delete(key)
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"message": "删除成功"}), 200

# ====================== 扩展 API：dashboard / disks / sync / files ======================
# _parse_size_to_gb 定义见上文（agent / dashboard 共用）

@app.route('/api/admin/dashboard', methods=['GET'])
@login_required
def admin_dashboard():
    """首页汇总：镜像数、总容量、状态分布、扫描中数量 + AI 洞察。"""
    fail_keys = []
    timeout_keys = []
    with lock:
        items = [(k, v) for k, v in sync_data.items()]
    total_gb = 0.0
    status_count = {"success": 0, "pending": 0, "syncing": 0, "fail": 0, "timeout": 0}
    scanning = 0
    sys_count = 0
    for k, it in items:
        gb = _parse_size_to_gb(it.get("total_size_gb"))
        if gb is not None:
            total_gb += gb
        st = it.get("status", "success")
        if st in status_count:
            status_count[st] += 1
        if it.get("is_scanning"):
            scanning += 1
        if it.get("is_system"):
            sys_count += 1
        if st == "fail":
            fail_keys.append({"key": k, "name": it.get("name") or k})
        elif st == "timeout":
            timeout_keys.append({"key": k, "name": it.get("name") or k})

    # AI 洞察：最近巡检 + 最近对话
    ai = {"inspect": None, "sessions": [], "tips": []}
    try:
        with _inspect_lock:
            insp = _last_inspect.get("data")
            insp_at = _last_inspect.get("at") or 0
        if insp:
            ai["inspect"] = {
                "at": insp.get("at"),
                "fail": (insp.get("counts") or {}).get("fail", 0),
                "total": (insp.get("counts") or {}).get("total", 0),
                "failed": (insp.get("failed") or [])[:5],
                "has_diagnosis": bool(insp.get("diagnosis")),
                "age_sec": int(time.time() - insp_at) if insp_at else None,
            }
    except Exception:
        pass
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id, agent, title, updated_at FROM agent_sessions "
                "ORDER BY updated_at DESC LIMIT 5").fetchall()
            ai["sessions"] = [dict(r) for r in rows]
    except Exception:
        pass
    if status_count["fail"] > 0:
        ai["tips"].append("有 %d 个镜像同步失败，可让巡检 Agent 分析根因" % status_count["fail"])
    if status_count["timeout"] > 0:
        ai["tips"].append("有 %d 个镜像处于续跑队列（原超时，系统监控中）" % status_count["timeout"])
    if status_count["syncing"] > 0:
        ai["tips"].append("当前 %d 个任务同步中" % status_count["syncing"])
    if not ai["tips"]:
        ai["tips"].append("运行正常，可用综合 Agent 优化定时或 Ansible 分发")

    loop = _build_loop_status()
    if loop.get("resume_queue_count"):
        ai["tips"].insert(
            0,
            "续跑队列 %d 项（闭环排队中）" % loop["resume_queue_count"],
        )
    if loop.get("status") == "degraded":
        ai["tips"].insert(0, "闭环探针 degraded：%s" % ("；".join(loop.get("issues") or [])[:160] or "见 /api/admin/loop"))

    return jsonify({
        "mirror_count": len(items),
        "db_mirror_count": len(items) - sys_count,
        "system_mirror_count": sys_count,
        "total_size_gb": round(total_gb, 1),
        "status_distribution": status_count,
        "scanning_count": scanning,
        "fail_count": status_count["fail"],
        "timeout_count": status_count["timeout"],
        "fail_keys": fail_keys[:8],
        "timeout_keys": timeout_keys[:8],
        "ai": ai,
        "loop": loop,
    })


def _build_loop_status():
    """同步闭环快照：daemon 心跳 + resume_queue + 最近 /api/report + crontab 缓存。

    闭环定义：trigger → 8.74 执行 → /api/report 回写 →（超时则）resume_queue → 再 trigger。
    """
    now = time.time()
    # traffic ~2s；auto_resume ~45s；crontab_cache ~300s；size_scan ~600s；agent 按配置
    expect = {
        "traffic": 15,
        "auto_resume": 180,
        "crontab_cache": 900,
        "size_scan": 1500,
        "agent_inspect": max(900, int(globals().get("AGENT_INSPECT_INTERVAL", 1800)) + 300),
        "daily_report": 7200,
    }
    with _daemon_beats_lock:
        beats = dict(_daemon_beats)
    daemons = {}
    issues = []
    for name, max_age in expect.items():
        ts = beats.get(name) or 0
        age = (now - ts) if ts else None
        ok = bool(ts) and age is not None and age <= max_age
        daemons[name] = {
            "ok": ok,
            "age_sec": int(age) if age is not None else None,
            "max_age_sec": max_age,
        }
        if not ok:
            issues.append(f"daemon:{name}")

    queue = []
    try:
        with db_conn() as conn:
            rows = conn.execute(
                """SELECT key, reason, tier, not_before, attempts, last_error,
                          created_at, updated_at
                   FROM resume_queue ORDER BY not_before ASC LIMIT 40"""
            ).fetchall()
        queue = [dict(r) for r in rows]
    except Exception as e:
        issues.append(f"resume_queue:{e}")

    def _queue_age_sec(q):
        for field in ("created_at", "updated_at"):
            raw = (q.get(field) or "").strip()
            if not raw:
                continue
            try:
                return (datetime.now() - datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")).total_seconds()
            except Exception:
                continue
        return None

    stuck = [q for q in queue if int(q.get("attempts") or 0) >= AUTO_RESUME_ALERT_ATTEMPTS]
    starved = []
    for q in queue:
        reason = (q.get("reason") or "")
        if reason not in ("parallel-cap", "scan-incomplete", "cooldown"):
            continue
        age = _queue_age_sec(q)
        if age is not None and age >= RESUME_STARVE_SEC:
            starved.append(q)
        elif int(q.get("attempts") or 0) >= AUTO_RESUME_ALERT_ATTEMPTS:
            starved.append(q)
    if stuck:
        issues.append("resume_stuck:%d" % len(stuck))
    if starved:
        issues.append("resume_starved:%d" % len(starved))

    with _crontab_cache_lock:
        cron_age = (now - _crontab_cache_at) if _crontab_cache_at else None
        cron_n = len(_crontab_cache)
    if cron_age is None or cron_age > 900:
        issues.append("crontab_cache_stale")

    report_age = (now - _loop_last_report_at) if _loop_last_report_at else None
    with lock:
        syncing_n = sum(1 for v in sync_data.values() if v.get("status") == "syncing")
        pending_script_n = sum(
            1 for v in sync_data.values()
            if v.get("status") == "pending" and (v.get("sync_script") or "").strip()
        )

    # 本机持锁但内存仍非 syncing：记账漂移，探针提示
    held = _held_lock_keys()
    ghost_running = []
    with lock:
        for k in held:
            st = (sync_data.get(k) or {}).get("status")
            if st and st != "syncing":
                ghost_running.append(k)
    if ghost_running:
        issues.append("lock_status_drift:%d" % len(ghost_running))

    status = "ok" if not issues else "degraded"
    return {
        "status": status,
        "issues": issues,
        "daemons": daemons,
        "resume_queue_count": len(queue),
        "resume_queue": queue[:20],
        "resume_stuck": stuck[:10],
        "resume_starved": starved[:10],
        "held_locks": sorted(held)[:30],
        "lock_status_drift": ghost_running[:10],
        "syncing_count": syncing_n,
        "pending_with_script": pending_script_n,
        "crontab_cache": {"age_sec": int(cron_age) if cron_age is not None else None, "entries": cron_n},
        "last_report": {
            "key": _loop_last_report_key or None,
            "age_sec": int(report_age) if report_age is not None else None,
        },
        "caps": {
            "max_parallel": AUTO_RESUME_MAX_PARALLEL,
            "max_huge": AUTO_RESUME_MAX_HUGE,
            "max_large": AUTO_RESUME_MAX_LARGE,
            "tier_counts": _tier_syncing_counts(),
            "starve_sec": RESUME_STARVE_SEC,
        },
    }


@app.route('/api/admin/loop', methods=['GET'])
@login_required
def admin_loop():
    """同步闭环探针：daemon / resume_queue / 最近上报 / crontab 缓存健康。"""
    return jsonify(_build_loop_status())

@app.route('/api/admin/disks', methods=['GET'])
@login_required
def admin_disks():
    """磁盘使用率（df -h 真实数据）。

    ?path=/hpcfs/fpublic 时对该路径做 df（Lustre/配额下比整盘 /hpcfs 更准），
    返回项的 mount 字段用请求路径，便于前端展示。
    """
    path = (request.args.get("path") or "").strip()
    disks = []
    try:
        if path:
            if not path.startswith("/") or ".." in path:
                return jsonify({"error": "path 必须是绝对路径且不含 .."}), 400
            cmd = ["df", "-hP", path]
        else:
            cmd = ["df", "-hP"]
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10
        )
        if result.returncode != 0 and path:
            err = (result.stderr or result.stdout or "").strip() or "df 失败"
            return jsonify({"error": f"读取磁盘失败：{err}"}), 500
        lines = result.stdout.strip().split("\n")[1:]
        for line in lines:
            parts = line.split()
            if len(parts) < 6:
                continue
            # -hP：固定 6 列；无 -P 时挂载点可能含空格，取最后一列
            filesystem, size, used, avail, use_pct, mount = parts[0], parts[1], parts[2], parts[3], parts[4], parts[-1]
            try:
                pct = int(use_pct.replace("%", ""))
            except Exception:
                pct = 0
            disks.append({
                "filesystem": filesystem,
                "size": size,
                "used": used,
                "avail": avail,
                "use_percent": pct,
                "mount": path or mount,
                "real_mount": mount,
                "path": path or mount,
            })
    except Exception as e:
        return jsonify({"error": f"读取磁盘失败：{e}"}), 500
    return jsonify(disks)

@app.route('/api/admin/sync/status', methods=['GET'])
@login_required
def admin_sync_status():
    """当前扫描中 / 同步中的任务列表。"""
    with lock:
        items = []
        for k, v in sync_data.items():
            if v.get("is_scanning") or v.get("status") == "syncing":
                items.append({
                    "key": k,
                    "name": v.get("name"),
                    "status": v.get("status"),
                    "is_scanning": bool(v.get("is_scanning")),
                    "last_sync_date": v.get("last_sync_date"),
                })
    return jsonify(items)

# ========== 远程 crontab 定时任务 ==========
_DOW = ["周一","周二","周三","周四","周五","周六","周日"]

def _cron_field_to_text(field, kind):
    """把单个 cron 字段转成中文描述。kind: minute/hour/dom/month/dow"""
    if field == "*":
        return None  # 通配，无需特别说明
    # */N 步进
    if field.startswith("*/"):
        n = field[2:]
        return {"minute":f"每{n}分钟","hour":f"每{n}小时"}.get(kind)
    # 列表 a,b,c
    if "," in field:
        vals = field.split(",")
        if kind == "dow":
            try: return "每周" + "/".join(_DOW[int(v)-1] for v in vals if v.isdigit())
            except: return None
        return None
    # 范围 a-b
    if "-" in field:
        a, b = field.split("-", 1)
        if kind == "hour": return f"{a}点至{b}点"
        return None
    # 单值
    if kind == "minute": return f"{field}分"
    if kind == "hour": return f"{field}点"
    if kind == "dom": return f"{field}号"
    if kind == "month": return f"{field}月"
    if kind == "dow":
        if field.isdigit():
            idx = int(field)
            if 0 <= idx <= 7: return _DOW[idx-1] if idx != 7 else _DOW[6]
        return field
    return None

def _describe_cron(m, h, dom, mon, dow):
    """生成人类可读的执行时间描述。"""
    parts = []
    # 周几
    dow_t = _cron_field_to_text(dow, "dow")
    # 时间
    if h == "*" and m == "*":
        time_t = "每小时"
    elif h != "*" and m != "*":
        time_t = f"{h}:{m.zfill(2)}"
    elif h != "*" and m == "*":
        time_t = f"每小时{h}点"
    elif h == "*" and m != "*":
        time_t = f"每小时{m}分"
    else:
        time_t = f"{h}:{m.zfill(2)}"
    # 日期/月份
    dom_t = _cron_field_to_text(dom, "dom")
    mon_t = _cron_field_to_text(mon, "month")
    date_str = ""
    if mon_t: date_str += mon_t
    if dom_t: date_str += dom_t
    # 组合
    sched = []
    if dow_t: sched.append(dow_t)
    if date_str: sched.append(date_str)
    if not sched: sched.append("每天")
    return "".join(sched) + " " + time_t

def _detect_cron_executor(cmd):
    """从 cron 命令识别执行器：wrapper | ansible | other。"""
    c = cmd or ""
    if "ai_dispatch.py" in c:
        return "ansible"
    if "wrapper.py" in c:
        return "wrapper"
    return "other"

def _extract_sync_script_from_cmd(cmd):
    """从 cron 命令提取同步脚本绝对路径或 basename。返回 (path_or_name, basename)。"""
    import re as _re
    # wrapper / ai_dispatch 形态优先
    m_disp = _re.search(r'\b(?:wrapper|ai_dispatch)\.py\s+(\S+)', cmd or "")
    if m_disp:
        arg = m_disp.group(1)
        if arg.endswith(".py") or arg.endswith(".sh"):
            return arg, arg.split("/")[-1]
        if arg == "bandersnatch":
            return "bandersnatch", "pypi_sync.py"
    m_py = _re.search(r'(/[A-Za-z0-9_./-]+\.py)\b', cmd or "")
    if m_py:
        p = m_py.group(1)
        base = p.split("/")[-1]
        if base not in ("wrapper.py", "ai_dispatch.py"):
            return p, base
    m_py2 = _re.search(r'([A-Za-z0-9_.-]+\.py)\b', cmd or "")
    if m_py2 and m_py2.group(1) not in ("wrapper.py", "ai_dispatch.py"):
        b = m_py2.group(1)
        return b, b
    m_sh = _re.search(r'(/?[A-Za-z0-9_./-]+\.sh)\b', cmd or "")
    if m_sh:
        p = m_sh.group(1)
        return p, p.split("/")[-1]
    if "bandersnatch" in (cmd or ""):
        return "bandersnatch", "pypi_sync.py"
    return "", ""

def _mirrors_by_script_basename():
    """{script_basename: {key, name, sync_script}}"""
    mapping = {}
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT key, name, sync_script FROM mirrors WHERE sync_script IS NOT NULL AND sync_script != ''"
            ).fetchall()
        for r in rows:
            script = (r["sync_script"] or "").strip()
            if not script:
                continue
            base = script.split("/")[-1]
            mapping[base] = {"key": r["key"], "name": r["name"], "sync_script": script}
            # pypi 特例
            if "pypi" in (r["key"] or "").lower() or base == "pypi_sync.py":
                mapping["pypi_sync.py"] = {"key": r["key"], "name": r["name"], "sync_script": script}
    except Exception:
        pass
    return mapping

def _parse_crontab_tasks(raw):
    """解析 crontab 文本为任务列表，含 line_no / sync_script / mirror_*。"""
    import re as _re
    mirror_map = _mirrors_by_script_basename()
    tasks = []
    current_note = None
    lines = (raw or "").splitlines()
    for line_no, line in enumerate(lines):
        s = line.strip()
        if not s:
            current_note = None
            continue
        if s.startswith("#"):
            clean = s.lstrip("# ").strip()
            if clean and not set(clean) <= set("=-*"):
                current_note = clean
            continue
        fields = s.split(None, 5)
        if len(fields) < 6:
            continue
        m, h, dom, mon, dow, cmd = fields
        script_path, script_base = _extract_sync_script_from_cmd(cmd)
        mir = mirror_map.get(script_base) or {}
        # 标题：优先镜像名，其次脚本名
        title = mir.get("name") or script_base or (cmd.split()[0] if cmd.split() else cmd)
        if not mir and not script_base:
            m_py = _re.search(r'([A-Za-z0-9_./-]+\.py)\b', cmd)
            m_sh = _re.search(r'([A-Za-z0-9_./-]+\.sh)\b', cmd)
            if m_py:
                title = m_py.group(1).split("/")[-1]
            elif m_sh:
                title = m_sh.group(1).split("/")[-1]
        sync_script = mir.get("sync_script") or script_path or ""
        executor = _detect_cron_executor(cmd)
        tasks.append({
            "id": len(tasks),
            "line_no": line_no,
            "minute": m, "hour": h, "dom": dom, "month": mon, "dow": dow,
            "schedule": _describe_cron(m, h, dom, mon, dow),
            "command": cmd,
            "title": title,
            "note": current_note,
            "sync_script": sync_script,
            "mirror_key": mir.get("key") or "",
            "mirror_name": mir.get("name") or "",
            "executor": executor,
        })
        current_note = None
    return tasks

def _fetch_crontab_raw():
    """拉取远程 crontab 原文。返回 (raw, error_msg, http_status)。"""
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new",
             "-o", "ConnectTimeout=10", SYNC_EXEC_HOST, "crontab -l"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        return None, "连接 8.74 超时", 504
    except Exception as e:
        return None, f"SSH 执行失败: {e}", 500
    if result.returncode != 0:
        err = (result.stderr or "").strip() or "未知错误"
        if "no crontab" in err.lower():
            return "", None, 200
        return None, err, 502
    return result.stdout or "", None, 200

def _write_crontab_raw(content, old_raw=""):
    """写回远程 crontab。返回 (ok, error_or_message, status, backup)。"""
    if len(content) > 65536:
        return False, "内容过长（>64KB）", 400, old_raw
    for ln in content.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if "=" in s and not s[0].isdigit() and "@" not in s.split("=", 1)[0]:
            continue
        if len(s.split(None, 5)) < 6:
            return False, f"非法行（非有效 cron 表达式）: {ln}", 400, old_raw
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
             SYNC_EXEC_HOST, "crontab -"],
            input=content, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        return False, "写入 8.74 超时", 504, old_raw
    except Exception as e:
        return False, f"SSH 写入失败: {e}", 500, old_raw
    if result.returncode != 0:
        return False, (result.stderr or "").strip() or "写入失败", 502, old_raw
    try:
        _refresh_crontab_cache()
    except Exception:
        pass
    return True, "crontab 已更新", 200, old_raw

def _normalize_sync_script_arg(sync_script):
    """校验并规范化 sync_script（绝对路径或 bandersnatch）。"""
    sync_script = (sync_script or "").strip()
    if not sync_script:
        raise ValueError("sync_script 不能为空")
    if sync_script == "bandersnatch":
        return "bandersnatch"
    if ".." in sync_script or not sync_script.startswith("/"):
        raise ValueError("sync_script 必须是绝对路径且不含 ..")
    if not sync_script.startswith(ALLOWED_SYNC_SCRIPT_PREFIXES):
        raise ValueError(f"sync_script 必须位于 {ALLOWED_SYNC_SCRIPT_PREFIXES} 之下")
    return sync_script

def _rebuild_cron_command(old_cmd, sync_script, executor=None):
    """按 sync_script + 执行器重建命令。executor: wrapper | ansible。"""
    import re as _re
    sync_script = _normalize_sync_script_arg(sync_script)
    cur = _detect_cron_executor(old_cmd or "")
    if executor is None:
        executor = cur if cur in ("wrapper", "ansible") else "wrapper"
    if executor not in ("wrapper", "ansible"):
        raise ValueError("executor 仅支持 wrapper 或 ansible")
    dispatcher = SYNC_WRAPPER_PATH if executor == "wrapper" else SYNC_AI_DISPATCH_PATH
    # 已是同族调度器：只替换参数
    if executor == cur and _re.search(r'\b(?:wrapper|ai_dispatch)\.py\s+\S+', old_cmd or ""):
        return _re.sub(r'((?:wrapper|ai_dispatch)\.py\s+)\S+', r'\1' + sync_script, old_cmd, count=1)
    return f"/usr/bin/python3 {dispatcher} {sync_script}"

def _validate_cron_field(name, val):
    v = (val or "").strip()
    if not v:
        return None, f"{name} 不能为空"
    # 允许 *, */N, 数字, 列表, 范围 的宽松校验
    import re as _re
    if not _re.fullmatch(r'[\d*/,.-]+', v):
        return None, f"{name} 非法: {v}"
    return v, None

@app.route('/api/admin/crontab', methods=['GET'])
@login_required
def admin_crontab():
    """从 8.74 获取 root crontab 并解析为结构化任务列表。"""
    raw, err, status = _fetch_crontab_raw()
    if err:
        return jsonify({"error": err}), status
    tasks = _parse_crontab_tasks(raw)
    return jsonify({"tasks": tasks, "count": len(tasks), "raw": raw, "error": None})

@app.route('/api/admin/crontab', methods=['PUT'])
@login_required
def admin_crontab_put():
    """整体覆盖 172.16.8.74 的 root crontab。接收完整文本，逐行校验后写回。"""
    payload = request.get_json(silent=True) or {}
    content = (payload.get("raw") or "")
    old_raw, err, status = _fetch_crontab_raw()
    if err and status != 200:
        old_raw = ""
    ok, msg, st, backup = _write_crontab_raw(content, old_raw or "")
    if not ok:
        return jsonify({"error": msg, "backup": backup}), st
    audit_log("crontab_update", target="172.16.8.74",
              detail={"lines": len([l for l in content.splitlines() if l.strip()])})
    return jsonify({"message": msg, "backup": backup})


@app.route('/api/admin/crontab/task', methods=['PATCH'])
@login_required
def admin_crontab_task_patch():
    """更新单条定时任务：时间 / 镜像源 / 同步脚本 / 执行方式。"""
    payload = request.get_json(silent=True) or {}
    try:
        tid = int(payload.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "缺少有效任务 id"}), 400

    raw, err, status = _fetch_crontab_raw()
    if err:
        return jsonify({"error": err}), status
    tasks = _parse_crontab_tasks(raw)
    task = next((t for t in tasks if t["id"] == tid), None)
    if not task:
        return jsonify({"error": "任务不存在或已变化，请刷新后重试"}), 404

    m, e = _validate_cron_field("minute", payload.get("minute", task["minute"]))
    if e: return jsonify({"error": e}), 400
    h, e = _validate_cron_field("hour", payload.get("hour", task["hour"]))
    if e: return jsonify({"error": e}), 400
    dom, e = _validate_cron_field("dom", payload.get("dom", task["dom"]))
    if e: return jsonify({"error": e}), 400
    mon, e = _validate_cron_field("month", payload.get("month", task["month"]))
    if e: return jsonify({"error": e}), 400
    dow, e = _validate_cron_field("dow", payload.get("dow", task["dow"]))
    if e: return jsonify({"error": e}), 400

    mirror_key = (payload.get("mirror_key") or "").strip()
    sync_script = (payload.get("sync_script") or "").strip()
    if mirror_key:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT key, name, sync_script FROM mirrors WHERE key = ?", (mirror_key,)
            ).fetchone()
        if not row:
            return jsonify({"error": f"镜像源不存在: {mirror_key}"}), 400
        if not sync_script:
            sync_script = (row["sync_script"] or "").strip()
        if not sync_script:
            return jsonify({"error": f"镜像 {mirror_key} 未配置 sync_script"}), 400

    executor = (payload.get("executor") or "").strip() or None
    if executor and executor not in ("wrapper", "ansible"):
        return jsonify({"error": "executor 仅支持 wrapper 或 ansible"}), 400
    old_executor = task.get("executor") or "other"
    if executor is None and old_executor in ("wrapper", "ansible"):
        executor = old_executor

    # 脚本或执行器变化时重建命令
    cmd = task["command"]
    old_script = (task.get("sync_script") or "").strip()
    old_base = old_script.split("/")[-1] if old_script else ""
    new_base = sync_script.split("/")[-1] if sync_script else ""
    need_rebuild = bool(sync_script) and (
        new_base != old_base
        or (sync_script != old_script and sync_script.startswith("/"))
        or (executor and executor != old_executor)
    )
    if need_rebuild:
        try:
            cmd = _rebuild_cron_command(task["command"], sync_script, executor=executor)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
    elif (payload.get("command") or "").strip():
        cmd = (payload.get("command") or "").strip()

    new_line = f"{m} {h} {dom} {mon} {dow} {cmd}"
    lines = (raw or "").splitlines()
    line_no = task["line_no"]
    if line_no < 0 or line_no >= len(lines):
        return jsonify({"error": "行号失效，请刷新后重试"}), 409
    lines[line_no] = new_line
    content = "\n".join(lines)
    if raw.endswith("\n"):
        content += "\n"

    ok, msg, st, backup = _write_crontab_raw(content, raw)
    if not ok:
        return jsonify({"error": msg, "backup": backup}), st
    audit_log("crontab_task_update", target="172.16.8.74",
              detail={"id": tid, "line": new_line, "mirror_key": mirror_key,
                      "sync_script": sync_script, "executor": executor})
    return jsonify({"message": "任务已更新", "line": new_line})


@app.route('/api/admin/crontab/task', methods=['POST'])
@login_required
def admin_crontab_task_create():
    """新建一条定时同步任务（写入 8.74 crontab）。"""
    payload = request.get_json(silent=True) or {}
    m, e = _validate_cron_field("minute", payload.get("minute", "0"))
    if e: return jsonify({"error": e}), 400
    h, e = _validate_cron_field("hour", payload.get("hour", "2"))
    if e: return jsonify({"error": e}), 400
    dom, e = _validate_cron_field("dom", payload.get("dom", "*"))
    if e: return jsonify({"error": e}), 400
    mon, e = _validate_cron_field("month", payload.get("month", "*"))
    if e: return jsonify({"error": e}), 400
    dow, e = _validate_cron_field("dow", payload.get("dow", "*"))
    if e: return jsonify({"error": e}), 400

    executor = (payload.get("executor") or "wrapper").strip()
    if executor not in ("wrapper", "ansible"):
        return jsonify({"error": "executor 仅支持 wrapper 或 ansible"}), 400

    mirror_key = (payload.get("mirror_key") or "").strip()
    sync_script = (payload.get("sync_script") or "").strip()
    note = (payload.get("note") or "").strip()[:120]
    if mirror_key:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT key, name, sync_script FROM mirrors WHERE key = ?", (mirror_key,)
            ).fetchone()
        if not row:
            return jsonify({"error": f"镜像源不存在: {mirror_key}"}), 400
        if not sync_script:
            sync_script = (row["sync_script"] or "").strip()
        if not note:
            note = f"{row['name'] or mirror_key} ({executor})"
    if not sync_script:
        return jsonify({"error": "请指定 sync_script 或 mirror_key"}), 400

    try:
        cmd = _rebuild_cron_command("", sync_script, executor=executor)
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400

    raw, err, status = _fetch_crontab_raw()
    if err:
        return jsonify({"error": err}), status

    # 避免同脚本重复（同 basename）
    new_base = sync_script.split("/")[-1] if sync_script != "bandersnatch" else "pypi_sync.py"
    for t in _parse_crontab_tasks(raw):
        old_base = (t.get("sync_script") or "").split("/")[-1]
        if old_base and old_base == new_base and t.get("executor") == executor:
            return jsonify({"error": f"已存在相同脚本的 {executor} 定时任务，请先编辑或删除"}), 409

    lines = (raw or "").splitlines()
    block = []
    if note:
        block.append(f"# {note}")
    block.append(f"{m} {h} {dom} {mon} {dow} {cmd}")
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(block)
    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"

    ok, msg, st, backup = _write_crontab_raw(content, raw)
    if not ok:
        return jsonify({"error": msg, "backup": backup}), st
    audit_log("crontab_task_create", target="172.16.8.74",
              detail={"line": block[-1], "mirror_key": mirror_key,
                      "sync_script": sync_script, "executor": executor})
    return jsonify({"message": "任务已创建", "line": block[-1], "executor": executor})


@app.route('/api/admin/crontab/task', methods=['DELETE'])
@login_required
def admin_crontab_task_delete():
    """删除单条定时任务。"""
    payload = request.get_json(silent=True) or {}
    try:
        tid = int(payload.get("id"))
    except (TypeError, ValueError):
        return jsonify({"error": "缺少有效任务 id"}), 400

    raw, err, status = _fetch_crontab_raw()
    if err:
        return jsonify({"error": err}), status
    tasks = _parse_crontab_tasks(raw)
    task = next((t for t in tasks if t["id"] == tid), None)
    if not task:
        return jsonify({"error": "任务不存在或已变化，请刷新后重试"}), 404

    lines = (raw or "").splitlines()
    line_no = task["line_no"]
    if line_no < 0 or line_no >= len(lines):
        return jsonify({"error": "行号失效，请刷新后重试"}), 409
    # 若上一行是该任务注释，一并删除
    drop = {line_no}
    if line_no > 0:
        prev = lines[line_no - 1].strip()
        if prev.startswith("#"):
            clean = prev.lstrip("# ").strip()
            if clean and not set(clean) <= set("=-*"):
                drop.add(line_no - 1)
    new_lines = [ln for i, ln in enumerate(lines) if i not in drop]
    content = "\n".join(new_lines)
    if content and not content.endswith("\n"):
        content += "\n"

    ok, msg, st, backup = _write_crontab_raw(content, raw)
    if not ok:
        return jsonify({"error": msg, "backup": backup}), st
    audit_log("crontab_task_delete", target="172.16.8.74",
              detail={"id": tid, "command": task.get("command"), "mirror_key": task.get("mirror_key")})
    return jsonify({"message": "任务已删除"})


# ====================== Ansible 调度 ======================
# 8.74 为 Ansible 控制机，inventory/playbook/log 均在 {ANSIBLE_DIR} 下。
# admin 页选 playbook + 目标主机组 -> ssh 8.74 跑 ansible-playbook -> 异步记 ansible_runs。
def _ansible_ssh(remote_cmd, timeout=20):
    """ssh 到 8.74 跑一条命令，返回 CompletedProcess。"""
    return subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
         SYNC_EXEC_HOST, remote_cmd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=timeout
    )


@app.route('/api/admin/ansible/inventory', methods=['GET'])
@login_required
def admin_ansible_inventory():
    """用 ansible-inventory --list 拿 8.74 上的 inventory 结构（分组/主机）。"""
    try:
        r = _ansible_ssh(f"cd {ANSIBLE_DIR} && ANSIBLE_LOG_PATH=/dev/null ansible-inventory --list", timeout=30)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "连接 8.74 超时"}), 504
    except Exception as e:
        return jsonify({"error": f"SSH 失败: {e}"}), 500
    if r.returncode != 0:
        return jsonify({"error": (r.stderr or "").strip() or "ansible-inventory 执行失败"}), 502
    try:
        data = json.loads(r.stdout)
    except Exception:
        return jsonify({"error": "解析 inventory 失败", "raw": (r.stdout or "")[-2000:]}), 500
    groups = []
    all_hosts = set()
    for name, body in data.items():
        if name in ("_meta", "all", "ungrouped"):
            continue
        hosts = body.get("hosts") or []
        children = body.get("children") or []
        if not hosts and not children:
            continue
        groups.append({"name": name, "hosts": hosts, "children": children})
        all_hosts.update(hosts)
    return jsonify({"groups": groups, "hosts_total": len(all_hosts)})


@app.route('/api/admin/ansible/playbooks', methods=['GET'])
@login_required
def admin_ansible_playbooks():
    """列出 playbooks/ 下的 .yml/.yaml，读内容并提取 hosts 与 task 数。"""
    r = _ansible_ssh(
        f"ls -1 {ANSIBLE_DIR}/playbooks/*.yml {ANSIBLE_DIR}/playbooks/*.yaml 2>/dev/null",
        timeout=15)
    pbs = []
    for fn in (r.stdout or "").split():
        name = os.path.basename(fn)
        rc = _ansible_ssh(f"cat {shlex.quote(fn)}", timeout=15)
        content = rc.stdout or ""
        hosts, tasks = "", 0
        for line in content.splitlines():
            ls = line.strip()
            if ls.startswith("hosts:") and not hosts:
                hosts = ls.split(":", 1)[1].strip()
            if ls.startswith("- name:"):
                tasks += 1
        pbs.append({"name": name, "path": fn, "content": content,
                    "hosts": hosts, "task_count": tasks})
    return jsonify({"playbooks": pbs, "count": len(pbs)})


@app.route('/api/admin/ansible/log', methods=['GET'])
@login_required
def admin_ansible_log():
    """读 ansible.log 末尾 N 行。"""
    lines = max(1, min(int(request.args.get("lines") or 200), 1000))
    r = _ansible_ssh(f"tail -n {lines} {ANSIBLE_DIR}/logs/ansible.log 2>/dev/null", timeout=15)
    if r.returncode != 0 and not (r.stdout or "").strip():
        return jsonify({"log": "", "error": (r.stderr or "").strip() or "日志读取失败"})
    return jsonify({"log": r.stdout or ""})


def _run_ansible(run_id, playbook, lim, extra_vars=None):
    """后台线程：ssh 8.74 跑 ansible-playbook，写 ansible_runs。
    运行中增量写内存 output，供对话栏 / 详情页实时拉日志。"""
    extra = ""
    if extra_vars:
        for k, v in extra_vars.items():
            extra += f" -e {shlex.quote(f'{k}={v}')}"
    cmd = (f"cd {shlex.quote(ANSIBLE_DIR)} && "
           f"ansible-playbook playbooks/{shlex.quote(playbook)} -l {shlex.quote(lim)}{extra}")
    out, rc, status = "", None, "ok"
    try:
        proc = subprocess.Popen(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15",
             SYNC_EXEC_HOST, cmd],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, bufsize=1,
        )
        chunks = []
        deadline = time.time() + 1800
        while True:
            if time.time() > deadline:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                chunks.append("\n执行超时(>30分钟)\n")
                out = "".join(chunks).strip()
                rc, status = -1, "timeout"
                break
            line = proc.stdout.readline() if proc.stdout else ""
            if line:
                chunks.append(line)
                with _ansible_runs_lock:
                    if run_id in _ansible_runs:
                        _ansible_runs[run_id]["output"] = "".join(chunks)[-16000:]
                continue
            if proc.poll() is not None:
                rest = (proc.stdout.read() if proc.stdout else "") or ""
                if rest:
                    chunks.append(rest)
                out = "".join(chunks).strip()
                rc = proc.returncode
                status = "ok" if rc == 0 else "fail"
                break
            time.sleep(0.05)
    except Exception as e:
        out, rc, status = f"异常: {e}", -1, "fail"
    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _ansible_runs_lock:
        if run_id in _ansible_runs:
            _ansible_runs[run_id].update({"status": status, "finished_at": finished,
                                          "rc": rc, "output": out})
    try:
        with db_conn() as conn:
            conn.execute(
                "UPDATE ansible_runs SET status=?, finished_at=?, rc=?, output=? WHERE run_id=?",
                (status, finished, rc, out[-16000:], run_id))
            conn.commit()
    except Exception as e:
        _log_sync(f"[ansible:{run_id}] 写历史失败: {e}")
    _log_sync(f"[ansible:{run_id}] {playbook} -l {lim} -> {status} (rc={rc})")


@app.route('/api/admin/ansible/run', methods=['POST'])
@login_required
def admin_ansible_run():
    """触发执行一个 playbook（限 playbooks/ 下现成 .yml，限已知主机组）。
    可选 sync_script：传给 -e（如 run_sync.yml）。
    注意：run_sync.yml 禁止 -l bnode/all 等整组，自动改走 ai_dispatch 单节点。"""
    payload = request.get_json(silent=True) or {}
    playbook = (payload.get("playbook") or "").strip()
    lim = (payload.get("limit") or "").strip()
    sync_script = (payload.get("sync_script") or "").strip()
    key = (payload.get("key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", playbook):
        return jsonify({"error": "playbook 非法（仅允许字母数字._- 且为 .yml/.yaml）"}), 400
    if not lim:
        lim = "auto" if _is_sync_playbook(playbook) else "all"
    if not _is_allowed_ansible_limit(lim):
        return jsonify({"error": "limit 非法，允许 auto / bnode1-30 / 分组"}), 400

    # 镜像同步：一律 ai_dispatch（flock + report）；显式 bnodeN 用 --node，禁止裸 ansible
    if _is_sync_playbook(playbook):
        if not sync_script and key:
            sync_script = ((sync_data.get(key) or {}).get("sync_script") or "").strip()
        if not sync_script:
            return jsonify({"error": "run_sync.yml 需要提供 sync_script 或 key"}), 400
        lim2, err, redirect = _resolve_run_sync_limit(lim, sync_script, key)
        if err:
            return jsonify({"error": err}), 400
        preferred = lim2 if lim2 != "auto" else None
        if not key:
            base = os.path.basename(sync_script)
            for k, v in sync_data.items():
                if os.path.basename((v.get("sync_script") or "")) == base:
                    key = k
                    break
        # auto + 有 key：走统一触发（含 status=syncing / 防重入）
        if key and not preferred:
            ret = trigger_sync_via_wrapper(
                key, "manual",
                session.get("admin_user") or session.get("user") or "admin",
                executor="ansible")
            code = 200 if ret.get("ok") else 400
            ret = dict(ret)
            ret.update({"routed": "ai_dispatch", "limit": "auto",
                        "message": ret.get("message") or "已交 ai_dispatch 单节点执行（按体积分配）"})
            return jsonify(ret), code
        ok, detail = _launch_ai_dispatch(sync_script, preferred_node=preferred)
        if not ok:
            return jsonify({"error": detail}), 502
        detail = dict(detail)
        if key:
            detail["key"] = key
            # 指定节点时也标 syncing，避免 UI 无反馈；终态仍靠 /api/report
            with lock:
                if key in sync_data and sync_data[key].get("status") != "syncing":
                    sync_data[key]["status"] = "syncing"
            _persist_mirror_status(key, "syncing")
            invalidate_status_cache()
            _set_sync_trigger(key, "manual",
                              session.get("admin_user") or session.get("user") or "admin")
        audit_log("ansible_run", target=playbook,
                  detail={"limit": preferred or "auto", "routed": "ai_dispatch",
                          "sync_script": sync_script})
        return jsonify(detail)

    extra_vars = None
    if sync_script:
        try:
            sync_script = _normalize_sync_script_arg(sync_script)
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        extra_vars = {"sync_script": sync_script}
    chk = _ansible_ssh(f"test -f {ANSIBLE_DIR}/playbooks/{playbook} && echo OK", timeout=10)
    if (chk.stdout or "").strip() != "OK":
        return jsonify({"error": f"playbook 不存在: {playbook}"}), 404
    run_id = "ar-" + uuid.uuid4().hex[:12]
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    by = session.get("admin_user") or session.get("user") or "admin"
    rec = {"run_id": run_id, "playbook": playbook, "lim": lim, "status": "running",
           "started_at": started, "finished_at": "", "rc": None, "output": "",
           "triggered_by": by, "sync_script": sync_script or ""}
    with _ansible_runs_lock:
        _ansible_runs[run_id] = rec
    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO ansible_runs(run_id,playbook,lim,status,started_at,triggered_by,output) "
                "VALUES(?,?,?,?,?,?,?)",
                (run_id, playbook, lim, "running", started, by, ""))
            conn.commit()
    except Exception as e:
        _log_sync(f"[ansible:{run_id}] 写库失败: {e}")
    audit_log("ansible_run", target=playbook,
              detail={"limit": lim, "run_id": run_id, "sync_script": sync_script or None})
    threading.Thread(target=_run_ansible, args=(run_id, playbook, lim, extra_vars), daemon=True).start()
    return jsonify({"run_id": run_id, "message": "已开始执行", "limit": lim})


@app.route('/api/admin/ansible/runs', methods=['GET'])
@login_required
def admin_ansible_runs():
    """列最近 N 条执行历史（不含 output，详情另查）。"""
    limit = max(1, min(int(request.args.get("limit") or 50), 200))
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT run_id,playbook,lim,status,started_at,finished_at,rc,triggered_by "
            "FROM ansible_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        with _ansible_runs_lock:
            live = _ansible_runs.get(d["run_id"])
        if live and live.get("status") == "running":
            d["status"] = "running"
        items.append(d)
    return jsonify({"runs": items, "count": len(items)})


@app.route('/api/admin/ansible/runs/<run_id>', methods=['GET'])
@login_required
def admin_ansible_run_detail(run_id):
    """单条执行详情（含 output；运行中优先返回内存增量日志）。"""
    with db_conn() as conn:
        r = conn.execute("SELECT * FROM ansible_runs WHERE run_id=?", (run_id,)).fetchone()
    if not r:
        return jsonify({"error": "未找到该执行记录"}), 404
    d = dict(r)
    with _ansible_runs_lock:
        live = _ansible_runs.get(run_id)
    if live:
        if live.get("status") == "running":
            d["status"] = "running"
        if live.get("output"):
            d["output"] = live["output"]
        if live.get("rc") is not None:
            d["rc"] = live["rc"]
        if live.get("finished_at"):
            d["finished_at"] = live["finished_at"]
            d["status"] = live.get("status") or d["status"]
    return jsonify(d)


@app.route('/api/admin/mirrors/<key>/sync', methods=['POST'])
@login_required
def admin_mirror_sync(key):
    """管理端手动同步单个镜像。"""
    if key not in sync_data:
        return jsonify({"error": "无效key"}), 400
    audit_log("sync", target=key, detail={"cmd": sync_data[key].get("sync_cmd", []), "via": "admin"})
    ret = trigger_sync_via_wrapper(key, "manual", "admin")
    code = 200 if ret.get("ok") else 400
    return jsonify({"message": ret.get("message"), "error": ret.get("error")}), code

@app.route('/api/admin/files/<key>', methods=['GET'])
@login_required
def admin_files(key):
    """镜像文件列表（JSON）。复用 browse 解析逻辑，带分页。"""
    root, is_system = _browse_resolve_root(key)
    if root is None:
        return jsonify({"error": "未知镜像目录"}), 404
    relpath = (request.args.get("path") or "").strip("/")
    full = os.path.normpath(os.path.join(root, relpath))
    # realpath 校验防符号链接逃逸（原 startswith 仅防 ../ 字面穿越；若镜像目录内含指向 root 外的符号链接，
    # realpath 会解析到 root 外，此处拦截。保留 full(normpath) 供 scandir 显示原始路径。）
    _real_root = os.path.realpath(root)
    _real_full = os.path.realpath(full)
    if not (_real_full == _real_root or _real_full.startswith(_real_root + os.sep)):
        return jsonify({"error": "非法路径"}), 403
    if not (full == root or full.startswith(root + os.sep)):
        return jsonify({"error": "非法路径"}), 403
    if not os.path.isdir(full):
        return jsonify({"error": "路径不存在"}), 404
    q = (request.args.get("q") or "").strip().lower()
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    page_size = 100
    raw = []
    try:
        with os.scandir(full) as it:
            for e in it:
                if q and q not in e.name.lower():
                    continue
                raw.append(e)
    except Exception as ex:
        return jsonify({"error": f"读取目录失败：{ex}"}), 500
    raw.sort(key=lambda e: (0 if e.is_dir(follow_symlinks=False) else 1, e.name.lower()))
    total = len(raw)
    start = (page - 1) * page_size
    page_items = raw[start:start + page_size]
    entries = []
    for e in page_items:
        is_dir = e.is_dir(follow_symlinks=False)
        ent = {"name": e.name, "is_dir": is_dir, "path": (relpath + "/" + e.name).strip("/")}
        if not is_dir:
            try:
                st = e.stat(follow_symlinks=False)
                ent["size"] = st.st_size
                ent["size_str"] = _human_size(st.st_size)
                ent["mtime"] = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ent["size"] = -1
                ent["size_str"] = "—"
                ent["mtime"] = "—"
        entries.append(ent)
    return jsonify({
        "key": key, "path": relpath, "page": page,
        "page_size": page_size, "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "entries": entries,
    })

# ====================== 扩展 API：audit / logs / monitor ======================

@app.route('/api/admin/audit', methods=['GET'])
@login_required
def admin_audit():
    """操作审计日志（分页 + 筛选）。支持 action=单值 或 actions=逗号分隔多值。"""
    action = request.args.get("action") or ""
    actions_raw = request.args.get("actions") or ""
    target = request.args.get("target") or ""
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        page_size = max(1, min(int(request.args.get("page_size") or 50), 500))
    except ValueError:
        page_size = 50
    where, params = [], []
    if action:
        where.append("action = ?"); params.append(action)
    elif actions_raw:
        acts = [a.strip() for a in actions_raw.split(",") if a.strip()]
        if acts:
            where.append("action IN (%s)" % ",".join("?" * len(acts)))
            params.extend(acts)
    if target:
        where.append("target LIKE ?"); params.append(f"%{target}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_db_conn()
    try:
        total = conn.execute(f"SELECT count(*) FROM audit_log {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM audit_log {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "page": page, "page_size": page_size, "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": [dict(r) for r in rows],
    })

@app.route('/api/admin/sync/history', methods=['GET'])
@login_required
def admin_sync_history():
    """同步历史记录（分页 + 按镜像/状态筛选）。"""
    key = request.args.get("key") or ""
    status = request.args.get("status") or ""
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    page_size = 50
    where, params = [], []
    if key:
        where.append("key = ?"); params.append(key)
    if status:
        where.append("status = ?"); params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    conn = get_db_conn()
    try:
        total = conn.execute(f"SELECT count(*) FROM sync_history {where_sql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM sync_history {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "page": page, "page_size": page_size, "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "items": [dict(r) for r in rows],
    })

@app.route('/api/admin/sync/stats', methods=['GET'])
@login_required
def admin_sync_stats():
    """同步统计：最近 7 天的成功/失败次数、各镜像最近一次同步状态。"""
    conn = get_db_conn()
    try:
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        rows = conn.execute(
            """SELECT date(started_at) as d, status, count(*) as c
               FROM sync_history WHERE started_at >= ?
               GROUP BY date(started_at), status ORDER BY d""",
            (since,),
        ).fetchall()
        # 各镜像最近一次
        latest = conn.execute(
            """SELECT key, name, status, started_at, finished_at, duration_sec
               FROM sync_history WHERE id IN (
                   SELECT MAX(id) FROM sync_history GROUP BY key
               ) ORDER BY key"""
        ).fetchall()
    finally:
        conn.close()
    trend = {}
    for r in rows:
        d = r["d"]
        trend.setdefault(d, {"success": 0, "fail": 0, "skip": 0})
        trend[d][r["status"]] = r["c"]
    return jsonify({
        "trend": [{"date": d, **v} for d, v in sorted(trend.items())],
        "latest": [dict(r) for r in latest],
    })


# ====================== AI 运维日报（按日落库，可回溯） ======================
DAILY_REPORT_HOUR = int(os.environ.get("DAILY_REPORT_HOUR", "22"))  # 当天日报默认生成时刻


def _parse_report_date(s):
    """校验 YYYY-MM-DD，非法返回 None。"""
    s = (s or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        return None


def _fmt_duration(sec):
    try:
        sec = int(sec or 0)
    except Exception:
        return "-"
    if sec <= 0:
        return "-"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def _daily_report_collect_facts(day):
    """采集某日事实：同步明细、登录、当前异常镜像（仅「今天」有意义）。"""
    day = _parse_report_date(day)
    if not day:
        raise ValueError("无效日期")
    today = datetime.now().strftime("%Y-%m-%d")
    with db_conn() as conn:
        sync_rows = conn.execute(
            """SELECT key, name, status, trigger_type, trigger_by, started_at, finished_at,
                      duration_sec, output_summary, node
               FROM sync_history
               WHERE date(started_at)=?
               ORDER BY started_at ASC, id ASC""",
            (day,),
        ).fetchall()
        login_rows = conn.execute(
            """SELECT action, operator, target, detail, created_at
               FROM audit_log
               WHERE date(created_at)=? AND action IN ('login','login_failed','logout')
               ORDER BY id ASC""",
            (day,),
        ).fetchall()
    sync_items = [dict(r) for r in sync_rows]
    counts = {"success": 0, "fail": 0, "timeout": 0, "skip": 0, "other": 0}
    for it in sync_items:
        st = it.get("status") or "other"
        if st in counts:
            counts[st] += 1
        else:
            counts["other"] += 1
    login_items = [dict(r) for r in login_rows]
    login_counts = {
        "login": sum(1 for x in login_items if x.get("action") == "login"),
        "login_failed": sum(1 for x in login_items if x.get("action") == "login_failed"),
        "logout": sum(1 for x in login_items if x.get("action") == "logout"),
    }
    fail_now = []
    if day == today:
        with lock:
            for k, v in sync_data.items():
                if v.get("status") == "fail":
                    fail_now.append({"key": k, "name": v.get("name") or k})
    return {
        "date": day,
        "is_today": day == today,
        "counts": counts,
        "sync_total": len(sync_items),
        "sync_items": sync_items,
        "login_counts": login_counts,
        "login_items": login_items[-80:],
        "fail_now": fail_now[:30],
    }


def _daily_report_fallback_md(facts):
    """无 AI 时的汇报日志底稿：口语、按时间流水，不写规范公文分段。"""
    day = facts["date"]
    c = facts["counts"]
    items = facts.get("sync_items") or []
    lines = [f"{day} 镜像站运维汇报", ""]

    tot = facts["sync_total"]
    if tot:
        lines.append(
            f"今天一共跑了 {tot} 次同步，成功 {c.get('success', 0)}，"
            f"失败 {c.get('fail', 0)}，超时 {c.get('timeout', 0)}，跳过 {c.get('skip', 0)}。"
        )
    else:
        lines.append("今天没有同步任务记录。")

    lc = facts["login_counts"]
    login_ok = lc.get("login", 0)
    login_fail = lc.get("login_failed", 0)
    if login_ok or login_fail:
        bit = f"后台登录 {login_ok} 次"
        if login_fail:
            bit += f"，另有 {login_fail} 次失败"
        lines.append(bit + "。")
    lines.append("")

    if items:
        lines.append("同步情况（按时间）：")
        for it in items:
            name = it.get("name") or it.get("key") or "-"
            key_ = it.get("key") or ""
            when = (it.get("started_at") or "")[11:16] or "?"
            dur = _fmt_duration(it.get("duration_sec"))
            st = it.get("status") or "?"
            if st == "success":
                tag = "完成"
            elif st == "fail":
                tag = "失败"
            elif st == "timeout":
                tag = "超时"
            elif st == "skip":
                tag = "跳过"
            else:
                tag = st
            line = f"{when}  {name}"
            if key_ and key_ != name:
                line += f"（{key_}）"
            line += f"  {tag}"
            if dur and dur != "-":
                line += f"，耗时 {dur}"
            lines.append(line)
            if st in ("fail", "timeout"):
                summ = (it.get("output_summary") or "").strip().replace("\n", " ")
                if summ:
                    lines.append(f"        备注：{summ[:160]}")
        lines.append("")
    else:
        lines.append("（无同步明细）")
        lines.append("")

    if facts.get("fail_now"):
        names = "、".join((x.get("name") or x.get("key")) for x in facts["fail_now"][:12])
        lines.append(f"目前还有异常镜像：{names}，需要继续跟。")
    else:
        lines.append("当前没有挂着的失败镜像。")

    lines.append("")
    lines.append("（以上为自动底稿；AI 可用时会改写成更顺口的汇报。）")
    return "\n".join(lines).strip() + "\n"


def _daily_report_ai_write(facts):
    """用技能网关写汇报日志口吻的日报；失败抛错由上层回退。"""
    day = facts["date"]
    c = facts["counts"]
    sync_lines = []
    for it in (facts.get("sync_items") or [])[:120]:
        sync_lines.append(
            f"- [{it.get('status')}] {it.get('name') or it.get('key')} ({it.get('key')}) "
            f"@{(it.get('started_at') or '')[11:19]} dur={_fmt_duration(it.get('duration_sec'))} "
            f"sum={(it.get('output_summary') or '')[:80].replace(chr(10), ' ')}"
        )
    if len(facts.get("sync_items") or []) > 120:
        sync_lines.append(f"- …另有 {len(facts['sync_items']) - 120} 条")
    fail_now = "、".join((x.get("name") or x.get("key")) for x in (facts.get("fail_now") or [])[:20]) or "无"
    system = (
        "你是镜像站值班同学，写给同事看的「汇报日志」，不是正式公文。"
        "口吻：口语、简洁、像微信/群里报进度；少用小标题和加粗；不要 Markdown 大标题层级堆砌；"
        "可以按时间流水写「几点谁同步完了」；必须把当天同步过的镜像都点到（名称即可，key 可括注）；"
        "失败/超时单独提一句原因或下一步；不要编造数据；不要输出思考过程或 XML 标签。"
    )
    user = (
        f"写 {day} 的运维汇报日志。\n"
        f"统计：同步 {facts['sync_total']} 次（成功{c.get('success',0)}/失败{c.get('fail',0)}/"
        f"超时{c.get('timeout',0)}/跳过{c.get('skip',0)}）；"
        f"登录成功{facts['login_counts'].get('login',0)} 失败{facts['login_counts'].get('login_failed',0)}；"
        f"当前异常={fail_now}\n\n"
        "同步记录：\n" + ("\n".join(sync_lines) if sync_lines else "（无）") + "\n\n"
        "示例口吻：\n"
        f"{day} 汇报\n"
        "今天同步挺顺，一共 XX 次都成功了。\n"
        "上午把 pypi、epel 跑完了；下午 conda-forge 跑得比较久。\n"
        "目前没有挂着的失败项。\n"
    )
    msg = _agent_openai_call(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        tools=None, max_tokens=2200, timeout=90,
    )
    text = _strip_think((msg.get("content") if isinstance(msg, dict) else str(msg)) or "").strip()
    if not text or len(text) < 40:
        raise RuntimeError("AI 日报内容过短或为空")
    return text


def _daily_report_generate(day, force=False, use_ai=True):
    """生成并落库某日日报。返回报告 dict。"""
    day = _parse_report_date(day)
    if not day:
        raise ValueError("无效日期")
    if not force:
        with db_conn() as conn:
            row = conn.execute("SELECT * FROM daily_reports WHERE report_date=?", (day,)).fetchone()
        if row and (row["content"] or "").strip():
            return dict(row)
    facts = _daily_report_collect_facts(day)
    generated_by = "fallback"
    content = None
    status = "ok"
    if use_ai:
        try:
            content = _daily_report_ai_write(facts)
            generated_by = "ai"
        except Exception as e:
            _log_sync(f"[DAILY-REPORT] AI 撰写失败 {day}: {e}")
            status = "fallback"
    if not content:
        content = _daily_report_fallback_md(facts)
        if status == "ok":
            status = "fallback"
    if facts["sync_total"] == 0 and not facts["login_counts"].get("login") and not facts.get("fail_now"):
        status = "empty"
    title = f"镜像站运维日报 · {day}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    facts_json = json.dumps({
        "date": facts["date"],
        "counts": facts["counts"],
        "sync_total": facts["sync_total"],
        "login_counts": facts["login_counts"],
        "fail_now": facts.get("fail_now") or [],
        "sync_items": [
            {
                "key": x.get("key"), "name": x.get("name"), "status": x.get("status"),
                "started_at": x.get("started_at"), "duration_sec": x.get("duration_sec"),
                "trigger_type": x.get("trigger_type"), "trigger_by": x.get("trigger_by"),
                "node": x.get("node"),
            }
            for x in (facts.get("sync_items") or [])
        ],
    }, ensure_ascii=False)
    with db_conn() as conn:
        conn.execute("DELETE FROM daily_reports WHERE report_date=?", (day,))
        conn.execute(
            """INSERT INTO daily_reports(report_date, title, content, facts_json, status, generated_at, generated_by)
               VALUES(?,?,?,?,?,?,?)""",
            (day, title, content, facts_json, status, now, generated_by),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM daily_reports WHERE report_date=?", (day,)).fetchone()
    return dict(row)


def _daily_report_daemon():
    """定时写日报：每 30 分钟检查；补写昨日；在 DAILY_REPORT_HOUR 后刷新当日。"""
    _daemon_beat("daily_report")
    time.sleep(90)
    while True:
        _daemon_beat("daily_report")
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            with db_conn() as conn:
                y = conn.execute("SELECT generated_at FROM daily_reports WHERE report_date=?", (yesterday,)).fetchone()
                t = conn.execute("SELECT generated_at, generated_by FROM daily_reports WHERE report_date=?", (today,)).fetchone()
            if not y:
                _daily_report_generate(yesterday, force=True, use_ai=True)
                _log_sync(f"[DAILY-REPORT] 已补写昨日日报 {yesterday}")
            # 当日：到达设定小时后生成；已有且超过 6 小时则刷新（把晚间同步写进去）
            if now.hour >= DAILY_REPORT_HOUR:
                need = True
                if t and t["generated_at"]:
                    try:
                        gat = datetime.strptime(str(t["generated_at"])[:19], "%Y-%m-%d %H:%M:%S")
                        need = (now - gat).total_seconds() >= 6 * 3600
                    except Exception:
                        need = True
                if need:
                    _daily_report_generate(today, force=True, use_ai=True)
                    _log_sync(f"[DAILY-REPORT] 已生成/刷新当日日报 {today}")
            elif not t and now.hour >= 8:
                # 白天若还没有当日稿，先落一版（可后续刷新）
                _daily_report_generate(today, force=True, use_ai=True)
                _log_sync(f"[DAILY-REPORT] 已预写当日日报 {today}")
        except Exception as e:
            _log_sync(f"[DAILY-REPORT] 定时任务异常：{e}")
        time.sleep(1800)


@app.route('/api/admin/daily-report', methods=['GET'])
@login_required
def admin_daily_report_get():
    """取某日 AI 日报；无则按需生成（默认不强制重写）。query: date, generate=1"""
    day = _parse_report_date(request.args.get("date") or datetime.now().strftime("%Y-%m-%d"))
    if not day:
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
    generate = (request.args.get("generate") or "1").strip() not in ("0", "false", "no")
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM daily_reports WHERE report_date=?", (day,)).fetchone()
    if not row and generate:
        try:
            row = _daily_report_generate(day, force=True, use_ai=True)
        except Exception as e:
            return jsonify({"error": f"生成日报失败：{e}"}), 500
    elif not row:
        return jsonify({"error": "该日尚无日报", "date": day}), 404
    else:
        row = dict(row)
    facts = {}
    try:
        facts = json.loads(row.get("facts_json") or "{}")
    except Exception:
        facts = {}
    # 同步明细以最新采集为准（便于回溯时看到完整列表），正文仍用落库 content
    try:
        live = _daily_report_collect_facts(day)
        facts = {
            "date": live["date"],
            "counts": live["counts"],
            "sync_total": live["sync_total"],
            "login_counts": live["login_counts"],
            "fail_now": live.get("fail_now") or [],
            "sync_items": [
                {
                    "key": x.get("key"), "name": x.get("name"), "status": x.get("status"),
                    "started_at": x.get("started_at"), "duration_sec": x.get("duration_sec"),
                    "trigger_type": x.get("trigger_type"), "trigger_by": x.get("trigger_by"),
                    "node": x.get("node"), "output_summary": (x.get("output_summary") or "")[:200],
                }
                for x in (live.get("sync_items") or [])
            ],
        }
    except Exception:
        pass
    return jsonify({
        "date": day,
        "title": row.get("title"),
        "content": row.get("content"),
        "status": row.get("status"),
        "generated_at": row.get("generated_at"),
        "generated_by": row.get("generated_by"),
        "facts": facts,
    })


@app.route('/api/admin/daily-report', methods=['POST'])
@login_required
def admin_daily_report_post():
    """强制重写某日日报。body: {date, use_ai?}"""
    data = request.get_json(silent=True) or {}
    day = _parse_report_date(data.get("date") or request.args.get("date") or datetime.now().strftime("%Y-%m-%d"))
    if not day:
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
    use_ai = data.get("use_ai", True)
    try:
        row = _daily_report_generate(day, force=True, use_ai=bool(use_ai))
    except Exception as e:
        return jsonify({"error": f"重写失败：{e}"}), 500
    audit_log("daily_report_regen", target=day, detail={"by": row.get("generated_by")})
    facts = {}
    try:
        facts = json.loads(row.get("facts_json") or "{}")
    except Exception:
        facts = {}
    return jsonify({
        "message": "日报已更新",
        "date": day,
        "title": row.get("title"),
        "content": row.get("content"),
        "status": row.get("status"),
        "generated_at": row.get("generated_at"),
        "generated_by": row.get("generated_by"),
        "facts": facts,
    })


@app.route('/api/admin/daily-report/dates', methods=['GET'])
@login_required
def admin_daily_report_dates():
    """已有日报日期列表（供回溯）。"""
    try:
        limit = max(1, min(int(request.args.get("limit") or 90), 366))
    except ValueError:
        limit = 90
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT report_date, status, generated_at, generated_by FROM daily_reports "
            "ORDER BY report_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify({"dates": [dict(r) for r in rows]})


def _tail_file(path, n=300, keyword=""):
    """安全读取文件尾部 n 行，可选关键字过滤。返回行列表。"""
    try:
        if not os.path.isfile(path):
            return [], f"文件不存在：{path}"
        # 用 tail 避免把超大日志全读进内存
        cmd = ["tail", "-n", str(n * 3 if keyword else n), path]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
        lines = result.stdout.splitlines()
        if keyword:
            kw = keyword.lower()
            lines = [l for l in lines if kw in l.lower()]
            lines = lines[-n:]
        else:
            lines = lines[-n:]
        return lines, None
    except Exception as e:
        return [], str(e)

@app.route('/api/admin/logs/access', methods=['GET'])
@login_required
def admin_logs_access():
    """mirrors.tibhpc.net 访问日志（解析为 IP / 镜像源 / 下载路径 / 字节）。
    查询参数：
      n          读取尾部行数（默认 300，最大 2000）
      q          关键字（IP / 路径 / 镜像 / UA）
      mirror     按镜像 key 过滤
      downloads  1=仅镜像下载（排除首页/api/admin，默认 1）；0=全部可解析行
    """
    try:
        n = max(50, min(int(request.args.get("n") or 300), 2000))
    except ValueError:
        n = 300
    kw = (request.args.get("q") or "").strip().lower()
    mirror_f = (request.args.get("mirror") or "").strip().lower()
    downloads_only = request.args.get("downloads", "1") != "0"

    path = _nginx_access_path()
    lines, err = _tail_file(path, n, "")
    if err:
        return jsonify({"error": err, "path": path}), 500

    # 多读一些再过滤，downloads 模式下尽量凑满 n 条有效记录
    if downloads_only:
        extra, _ = _tail_file(path, min(max(n * 8, 2000), 12000), "")
        if extra:
            lines = extra

    prefixes = _mirror_url_prefixes()
    items = []
    for line in lines:
        rec = _parse_nginx_access_line(line, prefixes)
        if not rec:
            continue
        if downloads_only:
            if not rec.get("mirror_key"):
                continue
            if rec["method"] not in ("GET", "HEAD"):
                continue
            # 目录索引页也算访问，保留；纯站点首页已在 match 时排除
        if mirror_f and (rec.get("mirror_key") or "").lower() != mirror_f:
            continue
        if kw:
            blob = " ".join([
                rec.get("ip") or "",
                rec.get("path") or "",
                rec.get("file") or "",
                rec.get("mirror_key") or "",
                rec.get("mirror_name") or "",
                rec.get("ua") or "",
            ]).lower()
            if kw not in blob:
                continue
        items.append(rec)

    # 最新在前，截断到 n
    items = list(reversed(items))[:n]

    # 汇总：独立 IP / 镜像 / 总字节
    ips = set()
    mirrors = {}
    total_bytes = 0
    for it in items:
        ips.add(it["ip"])
        total_bytes += it.get("bytes") or 0
        mk = it.get("mirror_key")
        if mk:
            row = mirrors.setdefault(mk, {"key": mk, "name": it.get("mirror_name") or mk, "bytes": 0, "reqs": 0})
            row["bytes"] += it.get("bytes") or 0
            row["reqs"] += 1
    top_mirrors = sorted(mirrors.values(), key=lambda x: x["bytes"], reverse=True)[:8]
    top_ips = {}
    for it in items:
        row = top_ips.setdefault(it["ip"], {"ip": it["ip"], "bytes": 0, "reqs": 0})
        row["bytes"] += it.get("bytes") or 0
        row["reqs"] += 1
    top_ips_list = sorted(top_ips.values(), key=lambda x: x["bytes"], reverse=True)[:8]

    # 按 IP 汇总：该 IP 访问了哪些源、下了哪些文件
    by_ip = {}
    for it in items:
        ip = it["ip"]
        g = by_ip.setdefault(ip, {
            "ip": ip, "bytes": 0, "reqs": 0, "mirrors": {}, "files": [], "ua": it.get("ua") or "",
        })
        g["bytes"] += it.get("bytes") or 0
        g["reqs"] += 1
        if it.get("ua"):
            g["ua"] = it["ua"]
        mk = it.get("mirror_key")
        if mk:
            m = g["mirrors"].setdefault(mk, {
                "key": mk, "name": it.get("mirror_name") or mk, "bytes": 0, "reqs": 0,
            })
            m["bytes"] += it.get("bytes") or 0
            m["reqs"] += 1
        if len(g["files"]) < 12:
            g["files"].append({
                "time": it.get("time"),
                "mirror_key": mk,
                "mirror_name": it.get("mirror_name"),
                "file": it.get("file") or it.get("path"),
                "path": it.get("path"),
                "bytes": it.get("bytes") or 0,
                "status": it.get("status"),
            })
    by_ip_list = sorted(by_ip.values(), key=lambda x: x["bytes"], reverse=True)
    for g in by_ip_list:
        g["mirrors"] = sorted(g["mirrors"].values(), key=lambda x: x["bytes"], reverse=True)

    return jsonify({
        "host": "https://mirrors.tibhpc.net",
        "path": path,
        "count": len(items),
        "scanned": len(lines),
        "downloads_only": downloads_only,
        "summary": {
            "ips": len(ips),
            "mirrors": len(mirrors),
            "bytes": total_bytes,
            "reqs": len(items),
        },
        "top_mirrors": top_mirrors,
        "top_ips": top_ips_list,
        "by_ip": by_ip_list,
        "items": items,
    })

@app.route('/api/admin/logs/sync', methods=['GET'])
@login_required
def admin_logs_sync():
    """聚合各同步脚本日志，按镜像分组返回。
    读取两处：
      1. /hpcfs/fpublic/manage/baiwx/sync_logs/<镜像>/  （新脚本统一目录）
      2. /hpcfs/fpublic/manage/baiwx/log/<镜像>/        （旧脚本各自目录）
    每个镜像取最新一个日志文件的尾部，标记是否含错误。
    """
    n = request.args.get("n", "200")
    only_error = request.args.get("error", "") == "1"
    n_int = int(n) if n.isdigit() else 200

    NEW_ROOT = "/hpcfs/fpublic/manage/baiwx/sync_logs"
    OLD_ROOT = "/hpcfs/fpublic/manage/baiwx/log"
    mirrors = {}  # mirror -> {latest_file, mtime, source}

    # scan 共享存储较慢，用模块级缓存 60 秒
    global _SYNC_LOG_SCAN_CACHE, _SYNC_LOG_SCAN_TS
    now = time.time()
    if "_SYNC_LOG_SCAN_CACHE" not in globals() or now - globals().get("_SYNC_LOG_SCAN_TS", 0) > 60:
        _mirrors = {}

        def scan(root, source):
            if not os.path.isdir(root):
                return
            for name in os.listdir(root):
                sub = os.path.join(root, name)
                if not os.path.isdir(sub):
                    continue
                try:
                    files = [f for f in os.listdir(sub)
                             if f.endswith(".log") or f.endswith(".txt")]
                except OSError:
                    continue
                if not files:
                    continue
                latest = os.path.join(sub, sorted(files)[-1])
                try:
                    mtime = os.path.getmtime(latest)
                except OSError:
                    continue
                key = name.lower()
                if key in _mirrors and _mirrors[key]["source"] == "new" and source == "old":
                    continue
                _mirrors[key] = {"latest_file": latest, "mtime": mtime,
                                 "source": source, "name": name}

        scan(NEW_ROOT, "new")
        scan(OLD_ROOT, "old")
        _SYNC_LOG_SCAN_CACHE = _mirrors
        _SYNC_LOG_SCAN_TS = now
    mirrors = dict(_SYNC_LOG_SCAN_CACHE)

    # 并发读取各镜像最新日志，避免串行 tail 卡顿
    from concurrent.futures import ThreadPoolExecutor
    items = list(mirrors.items())

    def fetch(kv):
        key, info = kv
        lines, err = _tail_file(info["latest_file"], n_int, "")
        has_error = any("[ERROR]" in l or "失败" in l or "❌" in l for l in lines)
        return key, info, lines, err, has_error

    fetched = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, info, lines, err, has_error in ex.map(fetch, items):
            if only_error and not has_error:
                continue
            fetched.append((info["mtime"], key, info, lines, err, has_error))

    fetched.sort(key=lambda x: -x[0])
    result = []
    for mtime, key, info, lines, err, has_error in fetched:
        result.append({
            "mirror": key,
            "name": info["name"],
            "source": info["source"],
            "file": os.path.basename(info["latest_file"]),
            "mtime": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "has_error": has_error,
            "lines": lines,
            "count": len(lines),
            "error": err,
        })
    return jsonify({"items": result, "total": len(result)})

def _read_proc_cpu():
    """读 /proc/stat 的 cpu 总行，返回 (idle, total) jiffies。"""
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu "):
                    parts = list(map(int, line.split()[1:]))
                    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
                    total = sum(parts)
                    return idle, total
    except Exception:
        pass
    return None, None

def _read_proc_mem():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.strip().split()[0]) if v.strip() else 0
        return info
    except Exception:
        return {}

# CPU 采样缓存：两次采样需 sleep 0.5s，/alerts 每次路由切换都会调（角标），
# 同一采样窗口内复用结果，避免页面加载被固定 0.5s 阻塞。
_cpu_cache = {"at": 0.0, "pct": None}
_cpu_cache_lock = threading.Lock()

def _cpu_percent_cached(ttl=30):
    """返回近期 CPU 使用率（ttl 秒内复用缓存），采样本身 0.5s。"""
    now = time.monotonic()
    with _cpu_cache_lock:
        if _cpu_cache["pct"] is not None and (now - _cpu_cache["at"]) < ttl:
            return _cpu_cache["pct"]
    # 采样在锁外进行，避免并发请求排队叠加 sleep
    i1, t1 = _read_proc_cpu()
    time.sleep(0.5)
    i2, t2 = _read_proc_cpu()
    pct = None
    if i1 is not None and i2 is not None and t2 > t1:
        pct = round((1 - (i2 - i1) / (t2 - t1)) * 100, 1)
    with _cpu_cache_lock:
        _cpu_cache["at"] = time.monotonic()
        _cpu_cache["pct"] = pct
    return pct

def _read_proc_net():
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
            total_rx = total_tx = 0
            for line in lines:
                parts = line.split()
                if len(parts) < 10:
                    continue
                # 跳过 lo
                if parts[0].rstrip(":") == "lo":
                    continue
                total_rx += int(parts[1])
                total_tx += int(parts[9])
            return total_rx, total_tx
    except Exception:
        return None, None

# —— 全站下载/入站流量：后台 2s 采样，供首页实时曲线 ——
from collections import deque, defaultdict
_traffic_hist = deque(maxlen=90)  # ~3 分钟 @2s
_traffic_lock = threading.Lock()
_traffic_last = {"rx": 0, "tx": 0, "ts": "", "ts_epoch": 0.0, "hostname": ""}

# nginx 访问日志 → 各镜像下载字节滑动窗（真实 body_bytes_sent）
_NGINX_ACCESS_CANDIDATES = (
    "/var/log/nginx/mirrors.tibhpc.net.log",
    "/www/wwwlogs/mirrors.tibhpc.net.log",
)
_NGINX_LINE_RE = re.compile(
    r'^\S+ \S+ \S+ \[([^\]]+)\] "(\S+) (\S+) [^"]*" (\d{3}) (\S+)'
)
# combined 全字段：IP · 时间 · 方法 · 路径 · 状态 · 字节 · Referer · UA
_NGINX_ACCESS_RE = re.compile(
    r'^(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+)(?: [^"]*)?" (\d{3}) (\S+) "([^"]*)" "(.*)"\s*$'
)
_NGINX_SKIP_PREFIXES = (
    "/api/", "/admin/", "/vendor/", "/logo/", "/favicon",
    "/loading.html", "/index.html",
)
_nginx_dl_lock = threading.Lock()
_nginx_dl_events = deque()  # (epoch, key, name, bytes)
_nginx_dl_state = {"path": "", "pos": 0, "inode": None}
_nginx_dl_window_sec = 3600     # 首页镜像排行统计窗口（1 小时）
_nginx_dl_retain_sec = 7200     # 内存保留 2 小时

def _sample_net_rates(interval=1.0):
    """两次 /proc/net/dev 采样，返回 (rx_bps, tx_bps)。失败返回 (None, None)。"""
    rx1, tx1 = _read_proc_net()
    if rx1 is None:
        return None, None
    time.sleep(interval)
    rx2, tx2 = _read_proc_net()
    if rx2 is None:
        return None, None
    # 防时钟回拨 / 计数器重置
    rx = max(0, rx2 - rx1)
    tx = max(0, tx2 - tx1)
    return int(rx / interval), int(tx / interval)

_iface_cache = {"at": 0.0, "rows": []}
_iface_cache_lock = threading.Lock()

def _sample_net_ifaces(interval=0.5):
    """分网卡速率（排除 lo）。返回 [{name, rx_bps, tx_bps}, ...]。"""
    def _snap():
        out = {}
        try:
            with open("/proc/net/dev") as f:
                for line in f.readlines()[2:]:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    name = parts[0].rstrip(":")
                    if name == "lo":
                        continue
                    out[name] = (int(parts[1]), int(parts[9]))
        except Exception:
            return {}
        return out
    a = _snap()
    if not a:
        return []
    time.sleep(interval)
    b = _snap()
    rows = []
    for name, (rx1, tx1) in a.items():
        if name not in b:
            continue
        rx2, tx2 = b[name]
        rows.append({
            "name": name,
            "rx_bps": max(0, int((rx2 - rx1) / interval)),
            "tx_bps": max(0, int((tx2 - tx1) / interval)),
        })
    rows.sort(key=lambda x: x["tx_bps"] + x["rx_bps"], reverse=True)
    return rows

def _sample_net_ifaces_cached(ttl=2.0):
    now = time.monotonic()
    with _iface_cache_lock:
        if _iface_cache["rows"] and (now - _iface_cache["at"]) < ttl:
            return list(_iface_cache["rows"])
    rows = _sample_net_ifaces(0.5)
    with _iface_cache_lock:
        _iface_cache["at"] = time.monotonic()
        _iface_cache["rows"] = rows
    return list(rows)

def _traffic_push_sample(rx, tx):
    now = datetime.now()
    point = {
        "t": now.strftime("%H:%M:%S"),
        "rx": int(rx or 0),
        "tx": int(tx or 0),
        "ts_epoch": time.time(),
    }
    with _traffic_lock:
        _traffic_hist.append(point)
        _traffic_last.update({
            "rx": point["rx"],
            "tx": point["tx"],
            "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
            "ts_epoch": point["ts_epoch"],
            "hostname": socket.gethostname(),
        })
    return point

def _mirror_url_prefixes():
    """按 url 前缀长度降序，便于最长匹配。"""
    rows = []
    with lock:
        for k, v in sync_data.items():
            u = (v.get("url") or k or "").strip()
            if not u:
                continue
            if not u.startswith("/"):
                u = "/" + u
            rows.append((u if u.endswith("/") else u + "/", k, v.get("name") or k))
    rows.sort(key=lambda x: len(x[0]), reverse=True)
    return rows

def _match_download_mirror(url_path, prefixes):
    path = (url_path or "").split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    if path in ("/", "/index.html", "/loading.html"):
        return None
    low = path.lower()
    for p in _NGINX_SKIP_PREFIXES:
        if low.startswith(p.rstrip("/")) or low == p.rstrip("/"):
            return None
    for pref, key, name in prefixes:
        if path.startswith(pref) or path.rstrip("/") == pref.rstrip("/"):
            return key, name
    # 未登记路径：用首段作 key，便于仍能看见流量
    seg = path.strip("/").split("/", 1)[0]
    if not seg or seg in ("api", "admin", "vendor", "logo"):
        return None
    return seg, seg

def _nginx_access_path():
    for p in _NGINX_ACCESS_CANDIDATES:
        if os.path.isfile(p):
            return p
    return _NGINX_ACCESS_CANDIDATES[0]

def _parse_nginx_access_line(line, prefixes=None):
    """解析一行 combined access.log → 结构化访问记录。"""
    line = (line or "").rstrip("\n")
    if not line:
        return None
    m = _NGINX_ACCESS_RE.match(line)
    if m:
        ip, t_raw, method, url, status, bs, ref, ua = m.groups()
    else:
        m2 = _NGINX_LINE_RE.match(line)
        if not m2:
            return None
        t_raw, method, url, status, bs = m2.groups()
        ip, ref, ua = (line.split(" ", 1)[0] if line else "-"), "-", "-"
    path = (url or "").split("?", 1)[0]
    try:
        nbytes = 0 if bs == "-" else int(bs)
    except ValueError:
        nbytes = 0
    try:
        ts = datetime.strptime(t_raw, "%d/%b/%Y:%H:%M:%S %z")
        time_str = ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        time_str = t_raw
    if prefixes is None:
        prefixes = _mirror_url_prefixes()
    matched = _match_download_mirror(path, prefixes)
    mirror_key, mirror_name = (matched if matched else (None, None))
    # 相对镜像根的文件路径，便于一眼看到下了什么
    file_path = path
    if matched:
        stripped = False
        for pref, key, _name in prefixes:
            if key == mirror_key and (path.startswith(pref) or path.rstrip("/") == pref.rstrip("/")):
                file_path = path[len(pref.rstrip("/")):] or "/"
                if not file_path.startswith("/"):
                    file_path = "/" + file_path
                stripped = True
                break
        if not stripped:
            # 首段回退匹配：去掉 /{key}/ 前缀
            seg = "/" + str(mirror_key).strip("/") + "/"
            if path.lower().startswith(seg.lower()):
                file_path = path[len(seg) - 1:]  # 保留前导 /
                if not file_path.startswith("/"):
                    file_path = "/" + file_path
    return {
        "ip": ip,
        "time": time_str,
        "method": method,
        "path": path,
        "file": file_path,
        "url": "https://mirrors.tibhpc.net" + path,
        "status": int(status) if str(status).isdigit() else status,
        "bytes": nbytes,
        "referer": ref if ref != "-" else "",
        "ua": ua or "",
        "mirror_key": mirror_key,
        "mirror_name": mirror_name,
        "raw": line,
    }

def _nginx_ingest_downloads():
    """增量读取 nginx access.log，把下载事件推进滑动窗。"""
    path = _nginx_access_path()
    if not os.path.isfile(path):
        return
    try:
        st = os.stat(path)
    except OSError:
        return
    prefixes = _mirror_url_prefixes()
    with _nginx_dl_lock:
        state = _nginx_dl_state
        if state["inode"] != st.st_ino or state["path"] != path:
            # 新文件 / 轮转：从末尾读一段，覆盖 retain 窗口内的日志即可
            state["path"] = path
            state["inode"] = st.st_ino
            state["pos"] = max(0, st.st_size - 4_000_000)
        try:
            with open(path, "rb") as f:
                f.seek(state["pos"])
                if state["pos"] > 0:
                    f.readline()  # 丢半行
                now_epoch = time.time()
                retain_cut = now_epoch - _nginx_dl_retain_sec
                while True:
                    raw = f.readline()
                    if not raw:
                        break
                    try:
                        line = raw.decode("utf-8", "replace")
                    except Exception:
                        continue
                    m = _NGINX_LINE_RE.match(line)
                    if not m:
                        continue
                    t_raw, method, url, status, bs = m.groups()
                    if method not in ("GET", "HEAD") or status not in ("200", "206"):
                        continue
                    try:
                        b = 0 if bs == "-" else int(bs)
                    except ValueError:
                        b = 0
                    if b <= 0:
                        continue
                    matched = _match_download_mirror(url, prefixes)
                    if not matched:
                        continue
                    key, name = matched
                    try:
                        ts = datetime.strptime(t_raw, "%d/%b/%Y:%H:%M:%S %z").timestamp()
                    except Exception:
                        ts = now_epoch
                    if ts < retain_cut:
                        continue
                    _nginx_dl_events.append((ts, key, name, b))
                state["pos"] = f.tell()
        except OSError:
            return
        # 修剪过期事件
        cut = time.time() - _nginx_dl_retain_sec
        while _nginx_dl_events and _nginx_dl_events[0][0] < cut:
            _nginx_dl_events.popleft()

def _nginx_download_top(limit=10, window_sec=None):
    """近 window 秒各镜像下载字节 / 请求数 / 折合 bps。"""
    _nginx_ingest_downloads()
    win = int(window_sec or _nginx_dl_window_sec)
    cut = time.time() - win
    agg = defaultdict(lambda: {"bytes": 0, "reqs": 0, "name": ""})
    with _nginx_dl_lock:
        for ts, key, name, b in _nginx_dl_events:
            if ts < cut:
                continue
            row = agg[key]
            row["bytes"] += b
            row["reqs"] += 1
            row["name"] = name or key
    items = []
    for key, row in agg.items():
        items.append({
            "key": key,
            "name": row["name"],
            "bytes": row["bytes"],
            "reqs": row["reqs"],
            "bps": int(row["bytes"] / win) if win > 0 else 0,
        })
    items.sort(key=lambda x: x["bytes"], reverse=True)
    total_bytes = sum(i["bytes"] for i in items)
    return {
        "window_sec": win,
        "total_bytes": total_bytes,
        "total_reqs": sum(i["reqs"] for i in items),
        "total_bps": int(total_bytes / win) if win > 0 else 0,
        "items": items[:limit],
        "log_path": _nginx_dl_state.get("path") or _nginx_access_path(),
    }

def _traffic_daemon():
    """持续采样本机网卡速率（排除 lo）：tx≈用户下载，rx≈同步入站；并 ingest nginx 下载明细。"""
    while True:
        _daemon_beat("traffic")
        try:
            rx, tx = _sample_net_rates(1.0)
            if rx is not None:
                _traffic_push_sample(rx, tx)
            try:
                _nginx_ingest_downloads()
            except Exception as e:
                print(f"⚠️  nginx 下载统计失败：{e}", flush=True)
        except Exception as e:
            print(f"⚠️  traffic 采样失败：{e}", flush=True)
        time.sleep(1.0)  # 合计约 2s 一点

@app.route('/api/admin/traffic', methods=['GET'])
@login_required
def admin_traffic():
    """全站实时流量（真实数据源）：
    - /proc/net/dev：整机/分网卡 rx·tx
    - nginx access.log：近 60s 各镜像下载排行（body_bytes_sent）
    """
    with _traffic_lock:
        stale = (time.time() - (_traffic_last.get("ts_epoch") or 0)) > 5
        last = dict(_traffic_last)
        hist = [{"t": p["t"], "rx": p["rx"], "tx": p["tx"]} for p in _traffic_hist]
    if stale or not hist:
        rx, tx = _sample_net_rates(0.8)
        if rx is not None:
            point = _traffic_push_sample(rx, tx)
            last = dict(_traffic_last)
            hist = hist + [{"t": point["t"], "rx": point["rx"], "tx": point["tx"]}]
            if len(hist) > 90:
                hist = hist[-90:]
    try:
        ifaces = _sample_net_ifaces_cached(2.0)
    except Exception:
        ifaces = []
    try:
        downloads = _nginx_download_top(limit=12, window_sec=_nginx_dl_window_sec)
    except Exception as e:
        downloads = {
            "window_sec": _nginx_dl_window_sec,
            "total_bytes": 0, "total_reqs": 0, "total_bps": 0,
            "items": [], "error": str(e),
            "log_path": _nginx_access_path(),
        }
    return jsonify({
        "hostname": last.get("hostname") or socket.gethostname(),
        "rx_bytes_per_sec": last.get("rx", 0),
        "tx_bytes_per_sec": last.get("tx", 0),
        "download_bytes_per_sec": last.get("tx", 0),
        "sync_in_bytes_per_sec": last.get("rx", 0),
        "history": hist,
        "ifaces": ifaces,
        "downloads": downloads,
        "timestamp": last.get("ts") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sample_interval_sec": 2,
        "source": "/proc/net/dev + nginx access.log",
    })

@app.route('/api/admin/monitor', methods=['GET'])
@login_required
def admin_monitor():
    """监控指标：CPU/内存/负载/磁盘/网络（真实采集，无 psutil）。
    CPU / 网络用后台缓存采样，请求路径不再 sleep(1)。"""
    cpu_percent = _cpu_percent_cached(ttl=5)
    with _traffic_lock:
        last = dict(_traffic_last)
    net_rx_rate = last.get("rx")
    net_tx_rate = last.get("tx")
    if net_rx_rate is None or (time.time() - (last.get("ts_epoch") or 0)) > 5:
        try:
            rx, tx = _sample_net_rates(0.4)
            if rx is not None:
                _traffic_push_sample(rx, tx)
                with _traffic_lock:
                    last = dict(_traffic_last)
                net_rx_rate = last.get("rx")
                net_tx_rate = last.get("tx")
        except Exception:
            pass
    # 内存
    mem = _read_proc_mem()
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    mem_used = mem_total - mem_avail
    mem_percent = round(mem_used / mem_total * 100, 1) if mem_total else 0
    # 负载
    load1 = load5 = load15 = None
    try:
        with open("/proc/loadavg") as f:
            la = f.read().split()
            load1, load5, load15 = float(la[0]), float(la[1]), float(la[2])
    except Exception:
        pass
    return jsonify({
        "hostname": socket.gethostname(),
        "cpu_percent": cpu_percent,
        "mem_total_kb": mem_total,
        "mem_used_kb": mem_used,
        "mem_percent": mem_percent,
        "load1": load1, "load5": load5, "load15": load15,
        "net_rx_bytes_per_sec": net_rx_rate,
        "net_tx_bytes_per_sec": net_tx_rate,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

# ====================== 上游源纳管（data/upstreams.json）+ 延迟探测 ======================

UPSTREAMS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "upstreams.json")
_upstreams_lock = threading.Lock()

# 内置种子：仅在 catalog 文件缺失时写入；日常以文件为准
_UPSTREAM_SEED = [
    {"id": "nju", "name": "南京大学 NJU", "host": "mirror.nju.edu.cn", "protocol": "rsync/https",
     "url": "https://mirror.nju.edu.cn/", "source": "https://mirror.nju.edu.cn/ · rsync://mirror.nju.edu.cn/",
     "category": "国内镜像", "note": "操作系统 / 语言包 / conda / CRAN 等主同步源", "enabled": True},
    {"id": "aliyun", "name": "阿里云开源镜像站", "host": "mirrors.aliyun.com", "protocol": "https",
     "url": "https://mirrors.aliyun.com/", "source": "https://mirrors.aliyun.com/",
     "category": "国内镜像", "note": "MySQL 等", "enabled": True},
]

# 兼容旧名：部分代码可能仍引用 UPSTREAM_SOURCES
UPSTREAM_SOURCES = _UPSTREAM_SEED

def _upstream_normalize(item):
    """规范化上游源条目。"""
    if not isinstance(item, dict):
        return None
    host = (item.get("host") or "").strip().lower()
    name = (item.get("name") or "").strip()
    url = (item.get("url") or "").strip()
    if not host and url:
        try:
            from urllib.parse import urlparse
            u = url if "://" in url else "https://" + url
            host = (urlparse(u).hostname or "").lower()
        except Exception:
            host = ""
    if not host and not name:
        return None
    uid = (item.get("id") or "").strip() or re.sub(r"[^a-z0-9._-]+", "-", host or name.lower()).strip("-")
    return {
        "id": uid,
        "name": name or host,
        "host": host,
        "protocol": (item.get("protocol") or "https").strip(),
        "url": url or (("https://" + host + "/") if host else ""),
        "source": (item.get("source") or url or "").strip(),
        "category": (item.get("category") or "其他").strip() or "其他",
        "note": (item.get("note") or "").strip(),
        "enabled": bool(item.get("enabled", True)),
    }

def get_managed_upstreams():
    """读取纳管目录；文件不存在则写入种子。"""
    with _upstreams_lock:
        try:
            if os.path.isfile(UPSTREAMS_FILE):
                with open(UPSTREAMS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                items = []
                for it in (raw if isinstance(raw, list) else []):
                    n = _upstream_normalize(it)
                    if n:
                        items.append(n)
                return items
        except Exception as e:
            print(f"⚠️  读取上游源目录失败: {e}", flush=True)
        # 种子落盘
        items = [_upstream_normalize(x) for x in _UPSTREAM_SEED]
        items = [x for x in items if x]
        try:
            os.makedirs(os.path.dirname(UPSTREAMS_FILE), exist_ok=True)
            with open(UPSTREAMS_FILE, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️  写入上游源种子失败: {e}", flush=True)
        return items

def save_managed_upstreams(items):
    """整表保存纳管目录。"""
    clean = []
    seen = set()
    for it in items or []:
        n = _upstream_normalize(it)
        if not n or n["id"] in seen:
            continue
        seen.add(n["id"])
        clean.append(n)
    with _upstreams_lock:
        os.makedirs(os.path.dirname(UPSTREAMS_FILE), exist_ok=True)
        tmp = UPSTREAMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        os.replace(tmp, UPSTREAMS_FILE)
    return clean

_UPSTREAM_URL_RE = re.compile(r"(?:rsync|https?|ftp)://[^\s'\"\\)>\]]+", re.I)

def _extract_hosts_from_text(text):
    """从文本抽出上游主机集合。"""
    hosts = set()
    if not text:
        return hosts
    from urllib.parse import urlparse
    for m in _UPSTREAM_URL_RE.findall(text):
        u = m.rstrip(");,.")
        try:
            if u.lower().startswith("rsync://"):
                host = u[8:].split("/")[0].split("@")[-1].lower()
            else:
                host = (urlparse(u).hostname or "").lower()
            if host and host not in ("localhost", "127.0.0.1") and "{" not in host:
                hosts.add(host)
        except Exception:
            continue
    return hosts

def _upstream_mirror_links():
    """host -> [{key,name}]：从 sync_cmd / sync_script 反查使用该上游的镜像。"""
    from collections import defaultdict
    mapping = defaultdict(list)
    seen = defaultdict(set)
    with lock:
        items = list(sync_data.items())
    for key, v in items:
        blob = ""
        cmd = v.get("sync_cmd")
        if isinstance(cmd, (list, dict)):
            try:
                blob += " " + json.dumps(cmd, ensure_ascii=False)
            except Exception:
                blob += " " + str(cmd)
        elif cmd:
            blob += " " + str(cmd)
        sp = (v.get("sync_script") or "").strip()
        if sp and os.path.isfile(sp):
            try:
                with open(sp, "r", encoding="utf-8", errors="ignore") as f:
                    blob += "\n" + f.read()
            except Exception:
                pass
        name = v.get("name") or key
        for host in _extract_hosts_from_text(blob):
            if key in seen[host]:
                continue
            seen[host].add(key)
            mapping[host].append({"key": key, "name": name})
    return mapping

def _discover_upstreams_from_mirrors():
    """扫描镜像配置，发现尚未纳管的上游主机。"""
    existing = { (u.get("host") or "").lower(): u for u in get_managed_upstreams() }
    # api.github.com 归并到 github.com
    alias = {"api.github.com": "github.com"}
    links = _upstream_mirror_links()
    discovered = []
    for host, mirrors in sorted(links.items(), key=lambda x: -len(x[1])):
        canon = alias.get(host, host)
        if canon in existing or host in existing:
            continue
        if "{" in host:
            continue
        proto = "https"
        if host.startswith("ftp.") or host.startswith("rsync."):
            proto = "ftp" if host.startswith("ftp.") else "rsync"
        probe = "https://" + host + "/"
        if host.startswith("rsync."):
            # rsync 专用机：尝试常见 https 入口
            probe = "https://" + host.replace("rsync.", "", 1) + "/" if host.startswith("rsync.") else probe
        discovered.append(_upstream_normalize({
            "id": re.sub(r"[^a-z0-9._-]+", "-", canon).strip("-"),
            "name": canon,
            "host": canon,
            "protocol": proto,
            "url": probe,
            "source": host,
            "category": "自动发现",
            "note": "关联镜像：" + ", ".join(m["key"] for m in mirrors[:8]),
            "enabled": True,
        }))
    return [d for d in discovered if d]

def _probe_upstream(url, timeout=8):
    """用 curl 探测单个上游源的响应延迟(ms)与 HTTP 状态码。"""
    try:
        result = subprocess.run(
            ["curl", "-o", "/dev/null", "-s", "-w",
             "%{http_code} %{time_total}",
             "--max-time", str(timeout), "-L", "--head", url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout + 2,
        )
        out = result.stdout.strip().split()
        if len(out) >= 2:
            code = out[0]
            time_total = float(out[1])
            http_code = int(code) if code.isdigit() else 0
            latency_ms = round(time_total * 1000)
            return {"latency_ms": latency_ms, "http_code": http_code,
                    "ok": http_code > 0 and result.returncode == 0,
                    "timeout": result.returncode == 28}
        return {"latency_ms": None, "http_code": 0, "ok": False,
                "timeout": result.returncode == 28}
    except subprocess.TimeoutExpired:
        return {"latency_ms": None, "http_code": 0, "ok": False, "timeout": True}
    except Exception:
        return {"latency_ms": None, "http_code": 0, "ok": False, "timeout": False}


@app.route('/api/admin/upstreams', methods=['GET'])
@login_required
def admin_upstreams():
    """纳管上游源列表 + 延迟探测 + 关联镜像。
    ?probe=0 仅返回目录不探测（更快）；默认 probe=1。
    """
    probe = request.args.get("probe", "1") != "0"
    if probe:
        ups = _probe_all_upstreams()
    else:
        links = _upstream_mirror_links()
        ups = []
        for src in get_managed_upstreams():
            row = dict(src)
            host = (src.get("host") or "").lower()
            row["mirrors"] = links.get(host, [])
            if host == "github.com":
                extra = links.get("api.github.com", [])
                seen = {m["key"] for m in row["mirrors"]}
                for m in extra:
                    if m["key"] not in seen:
                        row["mirrors"].append(m)
            ups.append(row)
    return jsonify({
        "upstreams": ups,
        "total": len(ups),
        "file": UPSTREAMS_FILE,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

@app.route('/api/admin/upstreams', methods=['POST'])
@login_required
def admin_upstreams_create():
    """新增上游源。"""
    payload = request.get_json(silent=True) or {}
    item = _upstream_normalize(payload)
    if not item or not item.get("host"):
        return jsonify({"error": "请填写名称与主机 host"}), 400
    items = get_managed_upstreams()
    if any(u["id"] == item["id"] or u["host"] == item["host"] for u in items):
        return jsonify({"error": f"上游源已存在：{item['host']}"}), 409
    items.append(item)
    save_managed_upstreams(items)
    audit_log("upstream_create", target=item["id"], detail=item)
    return jsonify({"message": "已添加", "item": item})

@app.route('/api/admin/upstreams/discover', methods=['POST'])
@login_required
def admin_upstreams_discover():
    """从镜像 sync_cmd / 脚本扫描，把未纳管上游合并进目录。"""
    found = _discover_upstreams_from_mirrors()
    if not found:
        return jsonify({"message": "没有新的上游源", "added": [], "count": 0})
    items = get_managed_upstreams()
    known = {(u.get("host") or "").lower() for u in items} | {u["id"] for u in items}
    added = []
    for d in found:
        if d["host"] in known or d["id"] in known:
            continue
        items.append(d)
        known.add(d["host"]); known.add(d["id"])
        added.append(d)
    if added:
        save_managed_upstreams(items)
        audit_log("upstream_discover", target="system",
                  detail={"added": [a["host"] for a in added]})
    return jsonify({"message": f"新纳入 {len(added)} 个上游源", "added": added, "count": len(added)})

@app.route('/api/admin/upstreams/<uid>', methods=['PUT'])
@login_required
def admin_upstreams_update(uid):
    """更新上游源。"""
    payload = request.get_json(silent=True) or {}
    items = get_managed_upstreams()
    idx = next((i for i, u in enumerate(items) if u["id"] == uid), None)
    if idx is None:
        return jsonify({"error": "未找到上游源"}), 404
    merged = dict(items[idx])
    merged.update(payload)
    merged["id"] = uid
    item = _upstream_normalize(merged)
    if not item:
        return jsonify({"error": "无效数据"}), 400
    items[idx] = item
    save_managed_upstreams(items)
    audit_log("upstream_update", target=uid, detail=item)
    return jsonify({"message": "已更新", "item": item})

@app.route('/api/admin/upstreams/<uid>', methods=['DELETE'])
@login_required
def admin_upstreams_delete(uid):
    """删除上游源。"""
    items = get_managed_upstreams()
    keep = [u for u in items if u["id"] != uid]
    if len(keep) == len(items):
        return jsonify({"error": "未找到上游源"}), 404
    save_managed_upstreams(keep)
    audit_log("upstream_delete", target=uid)
    return jsonify({"message": "已删除"})

# ====================== 扩展 API：alerts / users / settings ======================

@app.route('/api/admin/alerts', methods=['GET'])
@login_required
def admin_alerts():
    """告警列表：同步失败镜像 + 磁盘/CPU/内存超阈值。
    该接口为只读视图，不再触发任何通知发送。"""
    settings = get_all_settings()
    disk_threshold = int(settings.get("disk_alert_threshold", "85"))
    cpu_threshold = int(settings.get("cpu_alert_threshold", "90"))
    mem_threshold = int(settings.get("mem_alert_threshold", "90"))
    alerts = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 同步失败 / 超时（超时单独一类，避免与真实失败混为一谈）
    with lock:
        for k, v in sync_data.items():
            st = v.get("status")
            if st == "fail":
                alerts.append({
                    "level": "error",
                    "type": "sync_fail",
                    "title": f"镜像同步失败：{v.get('name', k)}",
                    "target": k,
                    "reason": "上游连接异常或同步命令失败",
                    "time": v.get("last_sync_date"),
                })
            elif st == "timeout":
                alerts.append({
                    "level": "info",
                    "type": "sync_resume",
                    "title": f"镜像续传中：{v.get('name', k)}",
                    "target": k,
                    "reason": "上次触达墙钟时限，系统正在监控并自动续跑",
                    "time": v.get("last_sync_date"),
                })
    # 磁盘超阈值
    try:
        result = subprocess.run(["df", "-h"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
        for line in result.stdout.strip().split("\n")[1:]:
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                pct = int(parts[4].replace("%", ""))
            except Exception:
                continue
            if pct >= disk_threshold:
                title = f"磁盘使用率告警：{parts[-1]} ({pct}%)"
                alerts.append({
                    "level": "warning",
                    "type": "disk_full",
                    "title": title,
                    "target": parts[-1],
                    "reason": f"已用 {parts[2]}/{parts[1]}，超过阈值 {disk_threshold}%",
                    "time": now_str,
                })
    except Exception:
        pass
    # CPU 超阈值（采样带 30s 缓存，避免每次调用固定阻塞 0.5s）
    try:
        cpu_pct = _cpu_percent_cached()
        if cpu_pct is not None:
            if cpu_pct >= cpu_threshold:
                title = f"CPU 使用率告警：{cpu_pct}%"
                alerts.append({
                    "level": "warning",
                    "type": "cpu_high",
                    "title": title,
                    "target": "cpu",
                    "reason": f"CPU 使用率 {cpu_pct}%，超过阈值 {cpu_threshold}%",
                    "time": now_str,
                })
    except Exception:
        pass
    # 内存超阈值
    try:
        mem = _read_proc_mem()
        mem_total = mem.get("MemTotal", 0)
        mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        if mem_total:
            mem_used = mem_total - mem_avail
            mem_pct = round(mem_used / mem_total * 100, 1)
            if mem_pct >= mem_threshold:
                title = f"内存使用率告警：{mem_pct}%"
                alerts.append({
                    "level": "warning",
                    "type": "mem_high",
                    "title": title,
                    "target": "memory",
                    "reason": f"内存使用率 {mem_pct}%，超过阈值 {mem_threshold}%",
                    "time": now_str,
                })
    except Exception:
        pass
    return jsonify({"alerts": alerts, "count": len(alerts)})

@app.route('/api/admin/users', methods=['GET'])
@login_required
def admin_users():
    """当前登录用户信息 + 近期登录记录（取自审计日志）。"""
    current_user = session.get("admin_user") or "admin"
    # 角色说明（域账号统一为运维管理员，本地 admin 为超级管理员）
    roles = [
        {"role": "super_admin", "name": "超级管理员", "desc": "本地 admin 账号，全部权限", "permissions": ["*"]},
        {"role": "domain_admin", "name": "域管理员", "desc": "AD 域 hpcadmin 组成员，全部权限", "permissions": ["*"]},
    ]
    current = {
        "username": current_user,
        "role": "super_admin" if current_user == "admin" else "domain_admin",
        "role_name": "超级管理员" if current_user == "admin" else "域管理员",
        "login_via": "本地账号" if current_user == "admin" else "AD 域认证",
    }
    # 近期登录记录（从审计日志拉 login / login_failed）
    recent_logins = []
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT action, target, created_at FROM audit_log "
                "WHERE action IN ('login','login_failed') "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()
        recent_logins = [
            {"user": r[1] or "admin", "result": "成功" if r[0] == "login" else "失败", "time": r[2]}
            for r in rows
        ]
    except Exception:
        pass
    return jsonify({"current": current, "roles": roles, "recent_logins": recent_logins})

@app.route('/api/admin/settings', methods=['GET'])
@login_required
def admin_settings_get():
    s = get_all_settings()
    # 凭证脱敏：密码 / 加签密钥不回传明文，前端留空占位（留空则 PUT 保留原值）
    for k in ("smtp_password", "notify_dingtalk_secret"):
        if s.get(k):
            s[k] = "***"
    return jsonify(s)

@app.route('/api/admin/settings', methods=['PUT'])
@login_required
def admin_settings_put():
    payload = request.get_json(silent=True) or {}
    allowed = set(DEFAULT_SETTINGS.keys())
    masked = {"smtp_password", "notify_dingtalk_secret"}
    changed = []
    for k, v in payload.items():
        if k in allowed:
            # 凭证字段：传 "***" 或空表示保留原值，不覆盖
            if k in masked and (v == "***" or v == ""):
                continue
            set_setting(k, v)
            changed.append(k)
    audit_log("settings_update", target="system", detail={"keys": changed})
    return jsonify({"message": "设置已更新", "changed": changed})

@app.route('/api/admin/settings/test', methods=['POST'])
@login_required
def admin_settings_test():
    """向钉钉发送测试通知（绕过冷却）。body.kind: alert|success|both。"""
    s = get_all_settings()
    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or "alert").strip().lower()
    if kind not in ("alert", "success", "both"):
        kind = "alert"
    kinds = ["alert", "success"] if kind == "both" else [kind]
    results = []
    if s.get("notify_dingtalk") != "1":
        return jsonify({"message": "请先在系统设置中启用钉钉通知", "results": []}), 400
    if not (s.get("notify_dingtalk_webhook") or "").strip():
        return jsonify({"message": "钉钉 Webhook 为空，请先填写", "results": ["钉钉：Webhook 为空"]}), 400
    for k in kinds:
        facts = (
            f"这是一条测试{'告警' if k == 'alert' else '成功'}通知\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"来源：镜像站后台 · 系统设置"
        )
        raw_title = "镜像站告警测试" if k == "alert" else "镜像站同步成功测试"
        title, content = _notify_compose(k, raw_title, facts)
        # 回显实际发送的完整内容（含标签与关键词前缀），
        # admin 页看到的应与钉钉群里收到的完全一致，便于排查 AI 文案异常
        sent_preview = _dingtalk_body(s, title, content, kind=k).replace("\n", " ⏎ ")
        if _send_dingtalk(s, title, content, kind=k):
            results.append(f"钉钉({k})：发送成功 ➜ {sent_preview}")
        else:
            results.append(f"钉钉({k})：发送失败，请检查 Webhook / 加签 / 关键词")
    audit_log("settings_test", target="system", detail={"results": results})
    return jsonify({"message": "；".join(results), "results": results}), 200

# ====================== 防火墙管理（firewalld） ======================
# 受保护端口：禁止通过 admin 页移除，避免把自己锁在外面
FIREWALL_PROTECTED_PORTS = {"22", "80", "443"}
# 允许操作的端口范围（避免开放任意高危端口）
FIREWALL_ALLOWED_PORT_RANGE = (1, 65535)

def _firewall_available():
    """探测 firewalld 是否可用（服务 active 且 firewall-cmd 可执行）。
    本机 firewalld 未启用/缺 python3-gobject 时 firewall-cmd 会直接抛错，
    提前探测以便接口降级返回而非 500。
    """
    try:
        r = subprocess.run(["systemctl", "is-active", "firewalld"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=5)
        if (r.stdout or "").strip() != "active":
            return False, "firewalld 服务未启用（systemctl is-active: %s）" % (r.stdout or "").strip()
    except Exception as e:
        return False, f"检测 firewalld 状态失败：{e}"
    rc, _, err = _firewall_cmd(["--state"], timeout=5)
    if rc != 0:
        return False, f"firewall-cmd 不可用：{(err or '').splitlines()[0] if err else '未知错误'}"
    return True, ""

def _firewall_cmd(args, timeout=10):
    """执行 firewall-cmd，返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run(
            ["firewall-cmd"] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=timeout,
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "firewall-cmd 执行超时"
    except Exception as e:
        return -1, "", str(e)

def _parse_ports(port_str):
    """解析 firewall-cmd --list-ports 输出，如 '5000/tcp 80/tcp 39000-40000/tcp'。"""
    ports = []
    for token in (port_str or "").split():
        if "/" not in token:
            continue
        port_part, proto = token.rsplit("/", 1)
        ports.append({"port": port_part, "proto": proto, "raw": token})
    return ports

@app.route('/api/admin/firewall', methods=['GET'])
@login_required
def admin_firewall_list():
    """列出当前区放行的端口、服务、默认区。firewalld 未启用时降级返回停用状态。"""
    ok, why = _firewall_available()
    if not ok:
        return jsonify({"enabled": False, "zone": "-", "ports": [],
                        "services": [], "interfaces": [], "note": why})
    rc, zone, err = _firewall_cmd(["--get-default-zone"])
    if rc != 0:
        return jsonify({"error": f"读取默认区失败：{err}"}), 500
    rc, ports_str, err = _firewall_cmd(["--list-ports"])
    if rc != 0:
        return jsonify({"error": f"读取端口失败：{err}"}), 500
    rc, services_str, _ = _firewall_cmd(["--list-services"])
    rc2, interfaces_str, _ = _firewall_cmd(["--list-interfaces"])
    ports = _parse_ports(ports_str)
    for p in ports:
        p["protected"] = p["port"].split("-")[0] in FIREWALL_PROTECTED_PORTS
    return jsonify({
        "zone": zone,
        "ports": ports,
        "services": (services_str or "").split(),
        "interfaces": (interfaces_str or "").split(),
    })

@app.route('/api/admin/firewall', methods=['POST'])
@login_required
def admin_firewall_add():
    """添加端口放行（永久+立即）。body: {port, proto}"""
    ok, why = _firewall_available()
    if not ok:
        return jsonify({"error": f"firewalld 不可用，无法放行端口：{why}"}), 400
    data = request.get_json(silent=True) or {}
    port = str(data.get("port", "")).strip()
    proto = str(data.get("proto", "tcp")).strip().lower()
    if proto not in ("tcp", "udp"):
        return jsonify({"error": "协议必须是 tcp 或 udp"}), 400
    # 校验端口：单端口或区间
    try:
        if "-" in port:
            a, b = port.split("-", 1)
            a, b = int(a), int(b)
            if not (FIREWALL_ALLOWED_PORT_RANGE[0] <= a <= b <= FIREWALL_ALLOWED_PORT_RANGE[1]):
                raise ValueError
            port_spec = f"{a}-{b}"
        else:
            p = int(port)
            if not (FIREWALL_ALLOWED_PORT_RANGE[0] <= p <= FIREWALL_ALLOWED_PORT_RANGE[1]):
                raise ValueError
            port_spec = str(p)
    except (ValueError, IndexError):
        return jsonify({"error": "端口格式错误，应为单端口或 a-b 区间"}), 400
    target = f"{port_spec}/{proto}"
    rc, out, err = _firewall_cmd(["--permanent", "--add-port", target])
    if rc != 0:
        return jsonify({"error": f"添加失败：{err or out}"}), 500
    rc, out2, err2 = _firewall_cmd(["--reload"])
    if rc != 0:
        return jsonify({"error": f"已添加但 reload 失败：{err2 or out2}"}), 500
    audit_log("firewall_add", target=target, detail={"zone": "default"})
    return jsonify({"message": f"已放行 {target}", "port": port_spec, "proto": proto})

@app.route('/api/admin/firewall', methods=['DELETE'])
@login_required
def admin_firewall_remove():
    """移除端口放行（永久+立即）。body: {port, proto}"""
    ok, why = _firewall_available()
    if not ok:
        return jsonify({"error": f"firewalld 不可用，无法移除端口：{why}"}), 400
    data = request.get_json(silent=True) or {}
    port = str(data.get("port", "")).strip()
    proto = str(data.get("proto", "tcp")).strip().lower()
    base_port = port.split("-")[0]
    if base_port in FIREWALL_PROTECTED_PORTS:
        return jsonify({"error": f"端口 {base_port} 受保护，禁止移除"}), 403
    if proto not in ("tcp", "udp"):
        return jsonify({"error": "协议必须是 tcp 或 udp"}), 400
    target = f"{port}/{proto}"
    rc, out, err = _firewall_cmd(["--permanent", "--remove-port", target])
    if rc != 0:
        return jsonify({"error": f"移除失败：{err or out}"}), 500
    rc, out2, err2 = _firewall_cmd(["--reload"])
    if rc != 0:
        return jsonify({"error": f"已移除但 reload 失败：{err2 or out2}"}), 500
    audit_log("firewall_remove", target=target, detail={"zone": "default"})
    return jsonify({"message": f"已移除 {target}", "port": port, "proto": proto})

# ====================== 系统/软件源子项（os/software 下级目录） ======================
# du 在 NFS 上很慢，用内存缓存 + 后台线程异步采集，前端轮询。
_system_source_cache = {}   # {"os/almalinux-elevate": {"size": "994M", "status": "done"|"pending"|"fail"}}
_system_source_lock = threading.Lock()

def _system_source_key(group, name):
    return f"{group}/{name}"

def _scan_system_source_size(group, name):
    """后台线程：远程到 8.74 跑 du（本地 NFS 会卡死），结果写缓存。"""
    key = _system_source_key(group, name)
    parent = "os_mirrors" if group == "os" else "soft_mirrors"
    path = f"{BASE_SYSTEM_PATH}/{parent}/{shlex.quote(name)}"
    try:
        result = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", SYNC_EXEC_HOST,
             f"du -sh {path} 2>/dev/null | awk '{{print $1}}'"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=120,
        )
        size = result.stdout.strip() if result.returncode == 0 else None
        with _system_source_lock:
            _system_source_cache[key] = {"size": size or "—", "status": "done" if size else "fail"}
    except Exception as e:
        print(f"⚠️  系统源大小扫描失败 [{key}]：{e}", flush=True)
        with _system_source_lock:
            _system_source_cache[key] = {"size": "—", "status": "fail"}

@app.route('/api/admin/system-sources', methods=['GET'])
@login_required
def admin_system_sources():
    """列出 os / software 下的子目录（almalinux、ubuntu、conda、pypi 等）。
    用 scandir 瞬间返回名称与顶层条目数；大小走异步缓存，前端轮询取。"""
    groups = [
        ("os", "操作系统镜像", "os_mirrors"),
        ("software", "软件应用镜像", "soft_mirrors"),
    ]
    result = {}
    for group, label, parent in groups:
        root = os.path.join(BASE_SYSTEM_PATH, parent)
        items = []
        try:
            with os.scandir(root) as it:
                for e in sorted(it, key=lambda x: x.name.lower()):
                    # 跳过 macOS 垃圾文件和隐藏文件
                    if e.name.startswith(".") or not e.is_dir(follow_symlinks=False):
                        continue
                    ck = _system_source_key(group, e.name)
                    with _system_source_lock:
                        cached = _system_source_cache.get(ck, {"size": "—", "status": "pending"})
                    items.append({
                        "name": e.name,
                        "size": cached["size"],
                        "status": cached["status"],
                        "url": f"{parent}/{e.name}/",
                    })
        except Exception as ex:
            return jsonify({"error": f"读取 {parent} 失败：{ex}"}), 500
        result[group] = {"label": label, "parent": parent, "items": items}
    return jsonify(result)

@app.route('/api/admin/system-source-size', methods=['POST'])
@login_required
def admin_system_source_size():
    """触发异步 du 采集 os/software 子目录大小。body: {targets: [["os","almalinux-elevate"], ...]}
    返回已提交数量；前端随后轮询 /system-sources 取结果。"""
    payload = request.get_json(silent=True) or {}
    targets = (payload.get("targets") or [])[:50]  # 上限 50，防无界线程+SSH 打爆 8.74
    kicked = 0
    for t in targets:
        if not (isinstance(t, list) and len(t) == 2):
            continue
        group, name = t[0], t[1]
        if group not in ("os", "software") or not re.fullmatch(r"[A-Za-z0-9._+-]+", name) or name in (".", ".."):
            continue
        ck = _system_source_key(group, name)
        with _system_source_lock:
            already = _system_source_cache.get(ck, {}).get("status")
        if already == "done":
            continue  # 已采集，跳过
        with _system_source_lock:
            _system_source_cache[ck] = {"size": "计算中", "status": "pending"}
        threading.Thread(target=_scan_system_source_size, args=(group, name), daemon=True).start()
        kicked += 1
    return jsonify({"message": "已提交采集", "kicked": kicked})

# ====================== 管理端页面 ======================
@app.route('/admin')
def admin_page():
    return send_from_directory('/hpcfs/fhome/baiwx/mirrors', 'admin.html')

# ====================== 大目录分页浏览（替代 nginx autoindex） ======================
# 仅对超大目录（如 biocyc，2万+ 条目）启用，由 nginx 按需 proxy 过来。
# 其余目录仍走 nginx autoindex，不受影响。
BROWSE_PAGE_SIZE = 100

def _browse_resolve_root(key):
    """根据 mirror key 解析磁盘根目录，返回 (root, is_system) 或 (None, None)。"""
    with lock:
        item = sync_data.get(key)
    if item:
        base = BASE_SYSTEM_PATH if item.get("is_system") else BASE_MIRROR_PATH
        return os.path.join(base, item["url"]).rstrip("/"), item.get("is_system")
    # 未登记的目录，按 database 根下同名目录尝试
    cand = os.path.join(BASE_MIRROR_PATH, key)
    if os.path.isdir(cand):
        return cand, False
    return None, None

def _human_size(num):
    for unit in ["B", "K", "M", "G", "T", "P"]:
        if abs(num) < 1024.0:
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}E"

def _browse_path_href(base_url, relpath, name):
    """拼接目录/文件链接：路径段 quote，避免空格/特殊字符与注入。"""
    parts = [base_url.rstrip("/")]
    if relpath:
        parts.extend(quote(p, safe="") for p in relpath.strip("/").split("/") if p)
    if name:
        parts.append(quote(name, safe=""))
    return "/".join(parts)


def _browse_html(key, relpath, entries, page, total, q, sort, order, is_system):
    """渲染分页目录浏览页。entries 为当前页条目列表。"""
    page = max(1, page)
    total_pages = max(1, (total + BROWSE_PAGE_SIZE - 1) // BROWSE_PAGE_SIZE)
    # 对外路径：nginx 把 /{key}/ 代理到内部 /browse/{key}/，
    # 故页面内所有链接（面包屑/子目录/文件下载/搜索）都用 /{key} 这一对公网路径，
    # 否则浏览器拿到 /browse/... 链接后在公网侧无对应 location → 404。
    key_e = html.escape(key, quote=True)
    q_e = html.escape(q or "", quote=True)
    q_q = quote(q or "", safe="")
    sort_e = html.escape(sort or "name", quote=True)
    order_e = html.escape(order or "asc", quote=True)
    base_url = f"/{quote(key, safe='')}"
    form_action = _browse_path_href(base_url, relpath, "") or base_url
    if relpath:
        form_action = form_action.rstrip("/") + "/"
    else:
        form_action = base_url + "/"
    # 面包屑
    crumbs = ['<a href="/">首页</a>', f'<a href="{html.escape(base_url, quote=True)}">{key_e}</a>']
    if relpath:
        parts = relpath.strip("/").split("/")
        acc = ""
        for p in parts:
            acc = (acc + "/" + p).strip("/")
            href = html.escape(_browse_path_href(base_url, acc, ""), quote=True)
            crumbs.append(f'<a href="{href}">{html.escape(p)}</a>')
    crumb_html = " / ".join(crumbs)

    def sort_link(col, label):
        arrow = " ↑" if sort == col and order == "asc" else (" ↓" if sort == col and order == "desc" else "")
        o = "asc" if (sort == col and order == "desc") else "desc"
        return (
            f'<a href="?q={q_q}&sort={html.escape(col, quote=True)}'
            f'&order={html.escape(o, quote=True)}&page=1">{html.escape(label)}{arrow}</a>'
        )

    rows = []
    # 非 根目录 显示返回上级
    if relpath:
        parent = "/".join(relpath.strip("/").split("/")[:-1])
        if parent:
            parent_href = _browse_path_href(base_url, parent, "").rstrip("/") + "/"
        else:
            parent_href = base_url + "/"
        rows.append(
            f'<tr><td colspan="4"><a class="up" href="{html.escape(parent_href, quote=True)}">'
            f'.. （返回上级）</a></td></tr>'
        )
    for e in entries:
        name = e["name"]
        name_e = html.escape(name)
        href = _browse_path_href(base_url, relpath, name)
        if e["is_dir"]:
            rows.append(
                f'<tr><td class="name"><span class="ico">📁</span>'
                f'<a href="{html.escape(href + "/", quote=True)}">{name_e}/</a></td>'
                f'<td class="meta">—</td><td class="meta">—</td><td class="meta">目录</td></tr>'
            )
        else:
            mt = html.escape(str(e.get("mtime_str", "—")))
            sz = html.escape(str(e.get("size_str", "—")))
            rows.append(
                f'<tr><td class="name"><span class="ico">📄</span>'
                f'<a href="{html.escape(href, quote=True)}" download>{name_e}</a></td>'
                f'<td class="meta">{sz}</td><td class="meta">{mt}</td><td class="meta">文件</td></tr>'
            )

    # 分页器
    def purl(p):
        return f"?q={q_q}&sort={sort_e}&order={order_e}&page={int(p)}"
    pager = []
    if page > 1:
        pager.append(f'<a class="pg" href="{purl(1)}">首页</a>')
        pager.append(f'<a class="pg" href="{purl(page-1)}">上一页</a>')
    pager.append(f'<span class="pg-cur">{page} / {total_pages}</span>')
    if page < total_pages:
        pager.append(f'<a class="pg" href="{purl(page+1)}">下一页</a>')
        pager.append(f'<a class="pg" href="{purl(total_pages)}">末页</a>')
    pager_html = " ".join(pager)

    # 大小/时间排序仅对本页 100 条生效（全量 stat 在 NFS 上不可接受），文案须诚实
    sort_tip = ""
    if sort in ("size", "mtime"):
        sort_tip = (
            '<div class="count" style="width:100%;margin:0 0 .6rem">'
            "提示：按大小/修改时间仅对本页条目排序（每页最多 "
            f"{BROWSE_PAGE_SIZE} 项），不是全目录排序。</div>"
        )

    html_out = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{key_e} - 目录浏览</title><style>
body{{font-family:"Inter","Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
background:linear-gradient(180deg,#eef4fb 0%,#f7faff 40%,#eef4fb 100%);color:#0f172a;margin:0;padding:2rem 1rem;}}
.wrap{{max-width:1200px;margin:0 auto;}}
h2{{color:#004a86;font-size:1.3rem;margin:0 0 .6rem;}}
.crumbs{{font-size:.9rem;color:#64748b;margin-bottom:1rem;word-break:break-all;}}
.crumbs a{{color:#005fb6;text-decoration:none;}}.crumbs a:hover{{text-decoration:underline;}}
.bar{{display:flex;gap:.8rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap;}}
.bar form{{flex:1;min-width:240px;display:flex;gap:.5rem;}}
.bar input{{flex:1;padding:.6rem .9rem;font-size:.9rem;border:1px solid #dde7f4;border-radius:8px;background:#fff;}}
.bar button{{padding:.6rem 1.2rem;font-size:.9rem;font-weight:600;color:#fff;background:linear-gradient(135deg,#005fb6,#004a86);border:none;border-radius:8px;cursor:pointer;}}
.bar .count{{font-size:.84rem;color:#64748b;}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dde7f4;border-radius:12px;overflow:hidden;box-shadow:0 6px 18px rgba(15,38,84,.06);}}
thead th{{padding:.8rem 1rem;text-align:left;color:#004a86;background:linear-gradient(180deg,#f4f9ff,#eaf3ff);font-size:.84rem;border-bottom:1px solid #dde7f4;}}
thead th a{{color:inherit;text-decoration:none;}}thead th a:hover{{text-decoration:underline;}}
tbody td{{padding:.7rem 1rem;border-bottom:1px solid #e9f0fa;font-size:.9rem;}}
tbody tr:hover{{background:#f4f9ff;}}tbody tr:last-child td{{border-bottom:none;}}
.name a{{color:#005fb6;text-decoration:none;}}.name a:hover{{text-decoration:underline;}}
.ico{{margin-right:6px;}}.up{{color:#b45309;font-weight:600;}}
.meta{{color:#64748b;font-variant-numeric:tabular-nums;}}
.pager{{margin-top:1.2rem;text-align:center;}}
.pager .pg,.pager .pg-cur{{display:inline-block;padding:.4rem .9rem;margin:0 2px;font-size:.86rem;border:1px solid #dde7f4;border-radius:6px;}}
.pager .pg{{color:#005fb6;text-decoration:none;background:#fff;}}.pager .pg:hover{{background:#eaf3ff;}}
.pager .pg-cur{{color:#0f172a;font-weight:600;background:#eaf3ff;}}
</style></head><body><div class="wrap">
<h2>📁 {key_e}</h2>
<div class="crumbs">{crumb_html}</div>
{sort_tip}
<div class="bar"><form method="get" action="{html.escape(form_action, quote=True)}" autocomplete="off">
<input type="text" name="q" value="{q_e}" placeholder="搜索当前目录下的名称…">
<input type="hidden" name="sort" value="{sort_e}"><input type="hidden" name="order" value="{order_e}">
<button type="submit">搜索</button></form>
<span class="count">共 {total} 项 · 第 {page}/{total_pages} 页</span></div>
<table><thead><tr>
<th>{sort_link('name','名称')}</th>
<th>{sort_link('size','大小')}</th>
<th>{sort_link('mtime','修改时间')}</th>
<th>类型</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<div class="pager">{pager_html}</div>
</div></body></html>"""
    return html_out

@app.route('/browse/<key>', methods=['GET'])
@app.route('/browse/<key>/', methods=['GET'])
@app.route('/browse/<key>/<path:subpath>', methods=['GET'])
def browse_dir(key, subpath=""):
    root, is_system = _browse_resolve_root(key)
    if root is None:
        return jsonify({"error": "未知镜像目录"}), 404
    # 拼接并校验路径，防穿越（用 realpath 解析符号链接 + commonpath 严格包含判断）
    full = os.path.join(root, subpath)
    if not is_safe_path(root, full) and os.path.realpath(full) != os.path.realpath(root):
        return jsonify({"error": "非法路径"}), 403
    if os.path.isfile(full):
        # 文件：直接下载
        try:
            return send_file(full, as_attachment=True)
        except Exception as e:
            return jsonify({"error": f"下载失败：{e}"}), 500
    if not os.path.isdir(full):
        return jsonify({"error": "路径不存在"}), 404

    # 读取参数
    q = (request.args.get("q") or "").strip().lower()
    sort = request.args.get("sort") or "name"
    if sort not in ("name", "size", "mtime"):
        sort = "name"
    order = request.args.get("order") or "asc"
    if order not in ("asc", "desc"):
        order = "asc"
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1

    relpath = subpath.strip("/")
    # 收集条目名（scandir 不 stat，2万条仅十几毫秒）
    raw = []
    try:
        with os.scandir(full) as it:
            for e in it:
                name = e.name
                if q and q not in name.lower():
                    continue
                raw.append({"name": name, "is_dir": e.is_dir(follow_symlinks=False), "entry": e})
    except Exception as ex:
        return jsonify({"error": f"读取目录失败：{ex}"}), 500

    from datetime import datetime as _dt

    def _fill_stat_strs(page_items):
        for e in page_items:
            if e["is_dir"]:
                e["size_str"] = "—"
                e["mtime_str"] = "—"
                continue
            try:
                # size/mtime 分支可能已 stat 过，复用数值
                if "size" in e and "mtime" in e and e.get("size", -1) >= 0 and e.get("mtime", -1) >= 0:
                    e["size_str"] = _human_size(e["size"])
                    e["mtime_str"] = _dt.fromtimestamp(e["mtime"]).strftime("%Y-%m-%d %H:%M")
                    continue
                st = e["entry"].stat(follow_symlinks=False)
                e["size"] = st.st_size
                e["mtime"] = st.st_mtime
                e["size_str"] = _human_size(st.st_size)
                e["mtime_str"] = _dt.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                e["size_str"] = "—"
                e["mtime_str"] = "—"

    # 排序：目录优先，组内按选定字段
    def sort_key(e):
        return (0 if e["is_dir"] else 1, e["name"].lower())
    if sort == "name":
        raw.sort(key=sort_key, reverse=(order == "desc"))
        total = len(raw)
        start = (page - 1) * BROWSE_PAGE_SIZE
        page_items = raw[start:start + BROWSE_PAGE_SIZE]
        _fill_stat_strs(page_items)
        return _browse_html(key, relpath, page_items, page, total, q, sort, order, is_system)

    # size/mtime：全量 stat 在 biocyc 等大目录上不可接受；
    # 仅对本页补 stat 后重排，并在页面提示「仅本页排序」。
    raw.sort(key=sort_key)
    total = len(raw)
    start = (page - 1) * BROWSE_PAGE_SIZE
    page_items = raw[start:start + BROWSE_PAGE_SIZE]
    for e in page_items:
        try:
            st = e["entry"].stat(follow_symlinks=False)
            e["size"] = st.st_size
            e["mtime"] = st.st_mtime
        except Exception:
            e["size"] = -1
            e["mtime"] = -1
    page_items.sort(key=lambda e: (0 if e["is_dir"] else 1, e.get(sort, -1)),
                    reverse=(order == "desc"))
    _fill_stat_strs(page_items)
    return _browse_html(key, relpath, page_items, page, total, q, sort, order, is_system)


# ------------------------------
# 路由
# ------------------------------
@app.errorhandler(NotFound)
def handle_404(e):
    if request.path.startswith(('/api/', '/db/', '/browse/')):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory('/hpcfs/fhome/baiwx/mirrors', 'index.html')

@app.route('/')
def index():
    return send_from_directory('/hpcfs/fhome/baiwx/mirrors', 'index.html')

# ====================== 健康检查 ======================
@app.route('/healthz', methods=['GET'])
def healthz():
    """存活 + 可选闭环探针。
    - 默认：SQLite + sync_data（systemd / Uptime Kuma）
    - ?deep=1：附带 daemon / resume_queue 摘要；关键 daemon 挂了返回 503
    """
    try:
        with db_conn() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as e:
        return jsonify({"status": "error", "db": str(e)}), 503
    if not sync_data:
        return jsonify({"status": "error", "detail": "sync_data 未加载"}), 503
    body = {"status": "ok", "mirrors": len(sync_data)}
    if (request.args.get("deep") or "").strip() in ("1", "true", "yes"):
        loop = _build_loop_status()
        body["loop"] = {
            "status": loop["status"],
            "issues": loop["issues"],
            "resume_queue_count": loop["resume_queue_count"],
            "syncing_count": loop["syncing_count"],
            "daemons": {k: {"ok": v["ok"], "age_sec": v["age_sec"]} for k, v in (loop.get("daemons") or {}).items()},
            "last_report": loop.get("last_report"),
        }
        # 仅关键守护线程挂死才 503；resume 积压算 degraded 但仍 200
        critical = [i for i in (loop.get("issues") or []) if i.startswith("daemon:")]
        if critical:
            body["status"] = "error"
            return jsonify(body), 503
        if loop["status"] != "ok":
            body["status"] = "degraded"
    return jsonify(body), 200

if __name__ == '__main__':
    _check_auth_env()
    _recover_orphaned_syncing()
    try:
        _heal_incomplete_statuses()
    except Exception as e:
        print(f"⚠️  启动未完成纠偏失败：{e}", flush=True)
    threading.Thread(target=size_scan_daemon, daemon=True).start()
    threading.Thread(target=cron_sync_daemon, daemon=True).start()
    threading.Thread(target=crontab_cache_daemon, daemon=True).start()
    threading.Thread(target=_agent_inspect_daemon, daemon=True).start()
    threading.Thread(target=_daily_report_daemon, daemon=True).start()
    threading.Thread(target=_traffic_daemon, daemon=True).start()
    threading.Thread(target=_auto_resume_daemon, daemon=True).start()
    # 生产必须开线程：默认 threaded=False 时 /api/report 会串行排队超时，拖垮同步闭环
    app.run(host='127.0.0.1', port=5000, debug=False, threaded=True)
