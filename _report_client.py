#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""8.13 本地回写客户端：从 stdin 读 JSON payload，调 /api/report 回写。
由 8.74 的 wrapper.py 通过 ssh 调用，payload 走 stdin 避开引号地狱。
令牌从 /etc/systemd/system/flask-mirror.env 读取。

闭环加固：瞬时网络/Flask 重启时自动重试，避免同步已完成却丢终态。
"""
import os, sys, json, time, urllib.request, urllib.error

ENV = "/etc/systemd/system/flask-mirror.env"
MAX_TRIES = int(os.environ.get("REPORT_CLIENT_TRIES", "3"))
RETRY_SLEEP = float(os.environ.get("REPORT_CLIENT_RETRY_SLEEP", "2"))


def read_token():
    try:
        with open(ENV) as f:
            for line in f:
                if line.startswith("RELOAD_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def post_report(payload, token):
    req = urllib.request.Request(
        "http://127.0.0.1:5000/api/report",
        data=payload.encode(),
        headers={"Content-Type": "application/json", "X-Reload-Token": token},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode()


def main():
    payload = sys.stdin.read().strip()
    if not payload:
        print("空 payload", file=sys.stderr)
        sys.exit(1)
    try:
        json.loads(payload)
    except Exception as e:
        print(f"payload 非 JSON：{e}", file=sys.stderr)
        sys.exit(1)
    token = read_token()
    if not token:
        print("未找到 RELOAD_TOKEN", file=sys.stderr)
        sys.exit(1)

    last_err = None
    for i in range(1, MAX_TRIES + 1):
        try:
            print(post_report(payload, token))
            return
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            # 4xx（鉴权/参数）不重试；5xx 可重试
            if 400 <= int(e.code) < 500:
                print(f"HTTP {e.code}: {body}", file=sys.stderr)
                sys.exit(1)
            last_err = f"HTTP {e.code}: {body}"
        except Exception as e:
            last_err = str(e)
        if i < MAX_TRIES:
            time.sleep(RETRY_SLEEP * i)
    print(f"回写失败（重试 {MAX_TRIES} 次）：{last_err}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
