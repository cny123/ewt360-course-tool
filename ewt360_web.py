#!/usr/bin/env python3
"""Local web adapter for ewt360_final.py.

Run this file and open http://127.0.0.1:8765. Credentials and token stay in
memory for the lifetime of this process and are never written to disk.
"""

from __future__ import annotations

import json
import argparse
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
LOCAL_DEPS = ROOT / ".deps"
if LOCAL_DEPS.exists():
    sys.path.insert(0, str(LOCAL_DEPS))

import ewt360_final as core


HOST = "127.0.0.1"
PORT = 8765


class AppState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.token: str | None = None
        self.school_id: int | None = None
        self.user_id: int | None = None
        self.lessons: list[dict[str, Any]] = []
        self.logs: list[dict[str, str]] = []
        self.job: dict[str, Any] = {"state": "idle", "mode": None, "done": 0, "total": 0}
        self.stop_event = threading.Event()

    def log(self, message: str, level: str = "INFO") -> None:
        item = {"message": str(message), "level": level}
        with self.lock:
            self.logs.append(item)
            self.logs = self.logs[-300:]
        print(f"[{level}] {message}", flush=True)

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {
                "loggedIn": bool(self.token),
                "schoolId": self.school_id,
                "userId": self.user_id,
                "lessons": [public_lesson(x) for x in self.lessons],
                "logs": list(self.logs),
                "job": dict(self.job),
            }


STATE = AppState()


def public_lesson(lesson: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(lesson.get("contentId", "")),
        "title": lesson.get("title", ""),
        "subject": lesson.get("subject", ""),
        "contentType": lesson.get("contentType", 1),
        "percent": float(lesson.get("percent", lesson.get("ratio", 0)) or 0) * (
            100 if float(lesson.get("percent", lesson.get("ratio", 0)) or 0) <= 1 else 1
        ),
        "finished": bool(lesson.get("finished", False)),
        "homeworkId": lesson.get("homeworkId"),
    }


def require_login() -> None:
    if not STATE.token:
        raise RuntimeError("请先登录")


def run_job(mode: str) -> None:
    try:
        with STATE.lock:
            token = STATE.token
            school_id = STATE.school_id
            user_id = STATE.user_id
            lessons = list(STATE.lessons)
            STATE.job = {"state": "running", "mode": mode, "done": 0, "total": len(lessons)}
        if not token or school_id is None or user_id is None:
            raise RuntimeError("登录状态已失效")

        original_log = core.log

        def bridge(message: str, level: str = "INFO") -> None:
            STATE.log(message, level)
            original_log(message, level)

        core.log = bridge
        try:
            if mode == "diagnose":
                for index, lesson in enumerate(lessons, 1):
                    task = core.get_task_info(
                        token,
                        school_id,
                        lesson["homeworkId"],
                        lesson["contentId"],
                        lesson.get("contentType", 1),
                    )
                    if task:
                        current = int(task.get("playTime", 0))
                        finish = int(task.get("finishPlayTime", 0))
                        lesson["percent"] = float(task.get("percent", 0) or 0)
                        lesson["finished"] = current >= finish
                        STATE.log(
                            f"[{index}/{len(lessons)}] {lesson.get('title', lesson['contentId'])} "
                            f"playTime={current} finishPlayTime={finish}",
                            "OK",
                        )
                        with STATE.lock:
                            STATE.lessons = lessons
                            STATE.job["done"] = index
            elif mode == "fast":
                secret, session_id = core.get_player_config(token)
                core.run_fast(token, lessons, school_id, user_id, secret, session_id)
            elif mode == "bfe":
                for index, lesson in enumerate(lessons, 1):
                    if STATE.stop_event.is_set():
                        break
                    STATE.log(f"开始 BFE 课程 {index}/{len(lessons)}", "INFO")
                    lesson_secret, lesson_session = core.get_player_config(token)
                    core.run_bfe_lesson(
                        token,
                        school_id,
                        user_id,
                        lesson["homeworkId"],
                        lesson["contentId"],
                        core.BIZCODE,
                        lesson_secret,
                        lesson_session,
                    )
                    with STATE.lock:
                        STATE.job["done"] = index
                    if index < len(lessons):
                        core.time.sleep(5)
            else:
                raise RuntimeError(f"不支持的模式: {mode}")
        finally:
            core.log = original_log
        with STATE.lock:
            STATE.job["state"] = "stopped" if STATE.stop_event.is_set() else "done"
    except Exception as exc:
        STATE.log(f"运行失败: {exc}", "ERR")
        traceback.print_exc()
        with STATE.lock:
            STATE.job["state"] = "error"
            STATE.job["error"] = str(exc)


class Handler(BaseHTTPRequestHandler):
    server_version = "EWT360Local/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求过大")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                data = (ROOT / "ewt360_dashboard.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/health":
                self.send_json({"ok": True, "service": "ewt360_web"})
            elif path == "/api/state":
                self.send_json({"ok": True, "state": STATE.public()})
            else:
                self.send_json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/login":
                account = str(body.get("account", "")).strip()
                password = str(body.get("password", ""))
                if not account or not password:
                    raise ValueError("账号和密码不能为空")
                token = core.login(account, password)
                school_id, user_id = core.get_school_user_info(token)
                with STATE.lock:
                    STATE.token, STATE.school_id, STATE.user_id = token, school_id, user_id
                    STATE.lessons = []
                    STATE.logs = []
                    STATE.job = {"state": "idle", "mode": None, "done": 0, "total": 0}
                STATE.log(f"登录成功: schoolId={school_id} userId={user_id}", "OK")
                self.send_json({"ok": True, "state": STATE.public()})
            elif path == "/api/logout":
                with STATE.lock:
                    STATE.token = STATE.school_id = STATE.user_id = None
                    STATE.lessons = []
                STATE.log("已退出当前会话", "INFO")
                self.send_json({"ok": True, "state": STATE.public()})
            elif path == "/api/scan":
                require_login()
                lessons = core.scan_lessons(STATE.token, STATE.school_id)
                with STATE.lock:
                    STATE.lessons = lessons
                STATE.log(f"扫描完成: {len(lessons)} 门课程", "OK")
                self.send_json({"ok": True, "state": STATE.public()})
            elif path == "/api/run":
                require_login()
                mode = str(body.get("mode", "diagnose"))
                if mode not in {"diagnose", "fast", "bfe"}:
                    raise ValueError("模式不支持")
                with STATE.lock:
                    if STATE.job.get("state") == "running":
                        raise RuntimeError("已有任务正在运行")
                    STATE.stop_event.clear()
                    STATE.job = {"state": "starting", "mode": mode, "done": 0, "total": len(STATE.lessons)}
                threading.Thread(target=run_job, args=(mode,), daemon=True).start()
                self.send_json({"ok": True, "state": STATE.public()})
            elif path == "/api/stop":
                STATE.stop_event.set()
                STATE.log("已请求停止任务", "WARN")
                self.send_json({"ok": True, "state": STATE.public()})
            else:
                self.send_json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)


def main() -> None:
    parser = argparse.ArgumentParser(description="EWT360 本地网页控制台")
    parser.add_argument("--host", default=HOST, help="监听地址，默认仅本机访问")
    parser.add_argument("--port", type=int, default=PORT, help="监听端口")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"本机访问 http://127.0.0.1:{args.port}", flush=True)
    if args.host == "0.0.0.0":
        try:
            lan_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            lan_ip = "电脑局域网IP"
        print(f"同一 Wi-Fi 手机访问 http://{lan_ip}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
