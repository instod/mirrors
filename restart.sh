#!/bin/bash
# 重启 Flask 镜像站后端。后端由 systemd 管理（flask-mirror.service，绑 127.0.0.1:5000，
# 密钥读 /etc/systemd/system/flask-mirror.env）。勿再用 nohup 手起 app.py--会与 systemd
# 抢占 5000 端口、留下孤儿进程。app-02.py 已停用（app-02.py.disabled），不再启动。
set -e

echo "[1] 通过 systemd 重启 flask-mirror..."
systemctl restart flask-mirror
sleep 2

echo "[2] 服务状态："
systemctl status flask-mirror --no-pager | head -12

PID=$(systemctl show -p MainPID --value flask-mirror 2>/dev/null)
if [ -n "$PID" ] && [ "$PID" != "0" ]; then
    echo "✅ flask-mirror 已重启，MainPID: $PID"
else
    echo "❌ 启动失败，查看 journalctl -u flask-mirror"
    exit 1
fi
