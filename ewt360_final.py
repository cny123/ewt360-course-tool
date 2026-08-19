11#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EWT360 课程进度自动提交工具 —— 基于 hmruu/ewt360 仓库思路实现
================================================================

三种模式：

  1) quick 快速模式
     直接调用 gateway 的 updateUserLessonTaskV2，把 playTime / clientLessonTime
     设为 88888888，携带 body 签名一次提交完成。
     body 签名算法（仓库 课程刷取.py / ewt360.js）：
        sign = MD5(BODY_SALT + clientLessonTime + homeworkId
                   + lessonId + playTime + BODY_SALT)
     ⚠️ 2026-08 实测: 该接口已被平台改为假成功(返回 success 但进度不涨), 仅保留作参考

  2) bfe 保守模式
     模拟 APP 端 BFE 监控心跳，按真实时间间隔逐步上报进度：
        POST bfe.ewt360.com/monitor/app/collect/batch
        签名: HMAC-SHA1(secret, sig_str)，secret 由 getPlayerGlobalConf 动态获取1
        
     每轮间隔约 60s 上报 120s 时长，结束后补发 reportVideoPoint 监测上报，
     每轮回查 playTime 是否真实增长。

  3) fast 快速验证模式
     每门课最多发送一个受限 BFE 心跳，并在提交后复查 playTime。
     仅在服务端实际记账且达到 finishPlayTime 时报告完成；未完成课程
     需要切换 bfe 模式继续真实计时。
     注意: 必须使用真实时间戳, 回拨/伪造会触发 699101 风控;
           手机 App 同时在播会触发 699001, 运行前请关闭 App。

依赖: pip install -r requirements.txt   (requests + pycryptodome 或 pyaes)

用法:
  # 登录 + 快速验证模式（自动扫描课程）
  python ewt_auto.py --user 13800000000 --pass 123456 --mode fast

  # 已有 token + BFE 模式（指定课程）
  python ewt_auto.py --token <TOKEN> --mode bfe \
      --homework-id <HOMEWORK_ID> --lesson-ids 111,222,333

  # 只提交指定课程（秒刷模式）
  python ewt_auto.py --token <TOKEN> --mode fast \
      --homework-id <HOMEWORK_ID> --lesson-ids 111,222
"""

import argparse
import binascii
import hashlib
import hmac
import json
import math
import os
import random
import sys
import time
import uuid

import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------
GATEWAY = "https://gateway.ewt360.com"

# 已废弃: updateUserLessonTaskV2 已被平台改为假成功(返回 success 但不记账)
# 实测 2026-08: 无论 playTime 填什么值, 进度均不变化。真正有效的是 BFE 心跳上报。
DONE_TIME = 88888888

# BFE 心跳参数 (已验证有效: 真实时间戳逐轮上报, 进度真实累加)
HEARTBEAT_MS = 120000    # 每轮上报的观看时长 (ms)  = 2 倍速
INTERVAL_S = 60          # 轮间真实等待 (s)
PROGRESS_VERIFY_DELAY_S = 1.5
BIZCODE = "2013"         # APP 视频业务码

REQUEST_TIMEOUT = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.30")


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def log(msg: str, level: str = "INFO") -> None:
    tag = {"INFO": "[*]", "OK": "[+]", "WARN": "[!]", "ERR": "[-]"}.get(level, "[*]")
    print(f"{tag} {msg}", flush=True)


def required_setting(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量 {name}，请参考 .env.example")
    return value


def aes_material() -> tuple[bytes, bytes]:
    key = required_setting("EWT360_AES_KEY").encode("utf-8")
    iv = required_setting("EWT360_AES_IV").encode("utf-8")
    if len(key) not in (16, 24, 32) or len(iv) != 16:
        raise RuntimeError("EWT360_AES_KEY 必须为 16/24/32 字节，EWT360_AES_IV 必须为 16 字节")
    return key, iv


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_ok(data: dict) -> None:
    """兼容 success 与 code 两种返回格式"""
    if isinstance(data, dict) and data.get("success") in (True, "true"):
        return
    code = data.get("code") if isinstance(data, dict) else None
    if str(code) in ("200", "0"):
        return
    raise RuntimeError(f"接口返回异常: {json.dumps(data, ensure_ascii=False)[:300]}")


def response_summary(resp) -> str:
    """保留 code/message，避免把 HTTP 200 当作成功。"""
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code} body={resp.text[:180]}"
    if isinstance(data, dict):
        details = {k: data.get(k) for k in ("code", "success", "message", "msg")
                   if k in data}
        return (f"HTTP {resp.status_code} {json.dumps(details, ensure_ascii=False)} "
                f"body={resp.text[:180]}")
    return f"HTTP {resp.status_code} body={resp.text[:180]}"


# ----------------------------------------------------------------------
# AES-CBC (登录密码加密) —— 优先 pycryptodome，退化到 pyaes
# ----------------------------------------------------------------------
try:
    from Crypto.Cipher import AES as _AES
    from Crypto.Util.Padding import pad as _pad

    def aes_cbc_encrypt(plain_text: str) -> str:
        key, iv = aes_material()
        cipher = _AES.new(key, _AES.MODE_CBC, iv)
        return binascii.hexlify(
            cipher.encrypt(_pad(plain_text.encode("utf-8"), 16))
        ).decode("utf-8").upper()

except ImportError:
    try:
        import pyaes

        def aes_cbc_encrypt(plain_text: str) -> str:
            key, iv = aes_material()
            raw = plain_text.encode("utf-8")
            pad_len = 16 - len(raw) % 16
            raw += bytes([pad_len]) * pad_len
            aes = pyaes.AESModeOfOperationCBC(key=key, iv=iv)
            return binascii.b2a_hex(aes.encrypt(raw)).decode("utf-8").upper()

    except ImportError:
        raise SystemExit(
            "缺少加密库。请使用当前 Python 安装: "
            f'"{sys.executable}" -m pip install pyaes requests'
        )


# ----------------------------------------------------------------------
# 账号 / 课程信息
# ----------------------------------------------------------------------
def login(account: str, password: str) -> str:
    """Web 端登录 (secretId=2)，返回 token"""
    ts = now_ms()
    web_secret = required_setting("EWT360_SECRET_WEB")
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "platform": "1",
        "secretid": "2",
        "timestamp": str(ts),
        "sign": hashlib.md5(f"{ts}{web_secret}".encode()).hexdigest().upper(),
        "User-Agent": UA,
    }
    body = {
        "autoLogin": "true",
        "password": aes_cbc_encrypt(password),
        "platform": 1,
        "userName": account,
    }
    log(f"登录 {account} ...")
    resp = requests.post(f"{GATEWAY}/api/authcenter/v2/oauth/login/account",
                         json=body, headers=headers, timeout=REQUEST_TIMEOUT)
    data = resp.json()
    ensure_ok(data)
    return data["data"]["token"]


def get_school_user_info(token: str):
    resp = requests.get(f"{GATEWAY}/api/eteacherproduct/school/getSchoolUserInfo",
                        headers={"token": token}, timeout=REQUEST_TIMEOUT)
    data = resp.json()
    ensure_ok(data)
    return data["data"]["schoolId"], data["data"]["userId"]


def scan_lessons(token: str, school_id: int):
    """场景 -> 作业 -> 天数 -> 课程任务，自动收集待完成课程"""
    def _get(path: str, **params):
        params["timestamp"] = now_ms()
        return requests.get(f"{GATEWAY}{path}", headers={"token": token},
                            params=params, timeout=REQUEST_TIMEOUT).json()

    def _post(path: str, body: dict):
        return requests.post(f"{GATEWAY}{path}", json=body,
                             headers={"token": token,
                                      "Content-Type": "application/json"},
                             timeout=REQUEST_TIMEOUT).json()

    lessons = []
    scenes = _get("/api/holidayprod/scene/student/study/checkHoliday",
                  clientType=1, preview=0, schoolId=school_id)
    ensure_ok(scenes)

    for scene in scenes["data"]["sceneList"]:
        scene_id = scene["id"]
        hw = _get("/api/homeworkprod/homework/student/holiday/getHomeworkSummaryInfo",
                  schoolId=school_id, sceneId=scene_id)
        ensure_ok(hw)
        for homework_id in hw["data"]["homeworkIds"]:
            dist = _post("/api/homeworkprod/homework/student/holiday/getHomeworkDistribution",
                         {"homeworkIds": [homework_id], "type": 2,
                          "isSelfTask": "false", "userOptionTaskId": "null",
                          "schoolId": school_id, "sceneId": scene_id})
            ensure_ok(dist)
            for day in dist["data"]["days"]:
                # status=0 未完成 / status=1 已完成, 合并去重
                seen = {}
                for status in (0, 1):
                    tasks = _post("/api/homeworkprod/homework/student/holiday/pageHomeworkTasks",
                                  {"dayId": [int(x) for x in day["dayId"]],
                                   "day": day["day"],
                                   "status": status, "homeworkIds": [homework_id],
                                   "isSelfTask": "false", "userOptionTaskId": "null",
                                   "pageIndex": 1, "pageSize": 30, "missionType": 0,
                                   "schoolId": school_id, "sceneId": scene_id})
                    ensure_ok(tasks)
                    for t in tasks["data"]["data"]:
                        if t.get("contentType") == 2:
                            continue  # 试题类跳过
                        seen[str(t["contentId"])] = {
                            "contentId": t["contentId"],
                            "parentContentId": t.get("parentContentId", t["contentId"]),
                            "homeworkId": homework_id,
                            "title": t.get("title", ""),
                            "contentType": t.get("contentType", 1),
                            "duration": t.get("duration", 0),
                            "ratio": t.get("ratio", 0),
                            "finished": t.get("finished", False),
                        }
                lessons.extend(seen.values())
    return lessons


def get_task_info(token: str, school_id: int, homework_id: int,
                  lesson_id: int, content_type: int = 1):
    """查询当前播放进度"""
    resp = requests.post(
        f"{GATEWAY}/api/homeworkprod/homework/student/getUserHomeworkLessonTaskInfo",
        json={"schoolId": school_id, "homeworkId": homework_id,
              "lessonId": lesson_id, "contentType": content_type},
        headers={"Content-Type": "application/json", "token": token},
        timeout=REQUEST_TIMEOUT)
    data = resp.json()
    ensure_ok(data)
    return data["data"]


def get_lesson_detail(token: str, homework_id: int, lesson_id: int, school_id: int):
    """BFE 模式需要: 课程点数 + 视频总时长 + 内容类型"""
    resp = requests.post(
        f"{GATEWAY}/api/homeworkprod/player/getLessonDetailV2",
        json={"homeworkId": homework_id, "lessonId": lesson_id, "schoolId": school_id},
        headers={"token": token, "Content-Type": "application/json; charset=UTF-8"},
        timeout=REQUEST_TIMEOUT)
    data = resp.json()
    ensure_ok(data)
    ld = data["data"]
    minutes = int(ld["playTime"].split(":")[0])
    return minutes + 1, ld["videoPlayTime"], ld.get("contentType", 1)


# ----------------------------------------------------------------------
# quick 模式: updateUserLessonTaskV2 直接提交
# ----------------------------------------------------------------------
def make_body_sign(client_lesson_time: int, homework_id: int,
                   lesson_id: int, play_time: int) -> str:
    salt = required_setting("EWT360_BODY_SALT")
    raw = f"{salt}{client_lesson_time}{homework_id}{lesson_id}{play_time}{salt}"
    return hashlib.md5(raw.encode()).hexdigest()


def submit_direct(token: str, homework_id: int, lesson_id: int,
                  header_sign: bool = False):
    ts = now_ms()
    headers = {
        "platform": "2",
        "version": "99.9.9",
        "token": token,
        "secretId": "1",
        "osVersion": "14",
        "channel": "ewt360",
        "device-type": "phone",
        "device-brand": "Redmi",
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": "okhttp/3.12.0",
    }
    if header_sign:  # 仓库逆向文档中的 Gateway header 签名, 默认可不带
        app_secret = required_setting("EWT360_SECRET_APP")
        headers["timestamp"] = str(ts)
        headers["sign"] = hashlib.md5(f"{ts}{app_secret}".encode()).hexdigest().upper()

    body = {
        "homeworkId": str(homework_id),
        "lessonId": str(lesson_id),
        "playTime": DONE_TIME,
        "clientLessonTime": DONE_TIME,
        "sign": make_body_sign(DONE_TIME, homework_id, lesson_id, DONE_TIME),
    }
    return requests.post(
        f"{GATEWAY}/api/homeworkprod/homework/student/updateUserLessonTaskV2",
        json=body, headers=headers, timeout=REQUEST_TIMEOUT)


def run_quick(token: str, lessons, header_sign: bool = False) -> None:
    log("警告: updateUserLessonTaskV2 已被平台修复为假成功, "
        "quick 模式不会真正增加进度, 请改用 BFE 模式", "WARN")
    ok = fail = 0
    for i, lesson in enumerate(lessons, 1):
        hid, lid = lesson["homeworkId"], lesson["contentId"]
        log(f"[{i}/{len(lessons)}] 提交 {lesson['title'] or lid} "
            f"(homeworkId={hid}, lessonId={lid})")
        try:
            resp = submit_direct(token, hid, lid, header_sign)
            text = resp.text[:150]
            code = "?"
            try:
                code = resp.json().get("code")
            except Exception:
                pass
            if resp.ok and str(code) in ("200", "0", "None", "?"):
                ok += 1
                log(f"    成功 -> HTTP {resp.status_code} | {text}", "OK")
            else:
                fail += 1
                log(f"    失败 -> HTTP {resp.status_code} | {text}", "WARN")
        except Exception as e:
            fail += 1
            log(f"    请求异常: {e}", "ERR")
        time.sleep(0.7)
    log(f"完成: 成功 {ok}, 失败 {fail}")


# ----------------------------------------------------------------------
# bfe 模式: 模拟 APP 心跳上报
# ----------------------------------------------------------------------
def get_player_config(token: str):
    resp = requests.get(
        f"{GATEWAY}/api/videoplayerprod/videoplayer/getPlayerGlobalConf",
        headers={"token": token},
        params={"videoBizCode": "1001", "sdkVersion": "2.0.95-test-rc21",
                "_": now_ms()},
        timeout=REQUEST_TIMEOUT)
    data = resp.json()
    ensure_ok(data)
    gi = data["data"]["globalInfo"]
    return gi["secret"], gi["sessionId"]


def make_bfe_signature(secret: str, action: int, duration: int, media_time: int,
                       mstid: str, timestamp_ms: int) -> str:
    raw = (f"action={action}&duration={duration}&mediaTime={media_time}"
           f"&mstid={mstid}&platform=2&signatureMethod=HMAC-SHA1"
           f"&signatureVersion=1.0&timestamp={timestamp_ms}&version=2022-08-02")
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"),
                    hashlib.sha1).hexdigest()


def build_common_package(user_id: int, token: str, school_id: int) -> dict:
    return {
        "os": "Android", "appBrand": "android",
        "schoolProvinceCode": "320000", "memberProvinceCode": "320000",
        "userid": str(user_id), "resolution": "1080*2306", "platform": "2",
        "appOnline": "1", "osVersion": "10", "appDeviceModel": "android",
        "appDevId": "0f99d6c0-693e-3f13-abef-60f6af4d9218",
        "schoolId": str(school_id), "sdkVersion": "2.0.95-test-rc21",
        "appCarrier": "N/A", "appAccess": "NETWORK_MOBILE",
        "mstid": token, "appLanguage": "zh",
    }


def report_bfe_round(token: str, session_id: str, user_id: int, school_id: int,
                     lesson_id: int, bizcode: str, action: int, event_type: str,
                     stay_time: int, media_time: int, point_time: int,
                     begin_time: int, point_num: int, secret: str):
    ts = now_ms()
    signature = make_bfe_signature(secret, action, stay_time, media_time,
                                   token, ts)
    trace_id = uuid.uuid4().hex[:12]
    log_id = str(uuid.uuid4())
    url = (f"https://bfe.ewt360.com/monitor/app/collect/batch"
           f"?TrLessonId={lesson_id}&TrVideoBizCode={bizcode}"
           f"&TrUuId={trace_id}&TrFallback=0&TrUserId={user_id}&token={token}")
    headers = {
        "token": token,
        "x-bfe-session-id": session_id,
        "Content-Type": "application/json; charset=UTF-8",
        "Host": "bfe.ewt360.com",
        "Accept-Encoding": "gzip",
    }
    body = {
        "CommonPackage": build_common_package(user_id, token, school_id),
        "EventPackage": [{
            "log_id": log_id,
            "course_id": lesson_id, "appVersion": "11.11.11",
            "point_time": point_time, "point_time_id": 0,
            "begin_time": begin_time, "lesson_id": lesson_id,
            "speed": 2.0, "appChannel": "android", "isonline": "1",
            "quality": "高清", "video_type": 1, "point_num": point_num,
            "event_type": event_type, "report_time": ts,
            "media_time": media_time, "action": action,
            "stay_time": stay_time, "video_bizcode": bizcode, "status": 1,
        }],
        "signature": signature,
        "sn": "moses_ewt_video_detail_2026",
        "_": ts,
    }
    resp = requests.post(url, json=body, headers=headers,
                         timeout=REQUEST_TIMEOUT)
    return resp


def report_video_point(token: str, homework_id: int, lesson_id: int) -> None:
    """结束后的监测上报"""
    ts = now_ms()
    app_secret = required_setting("EWT360_SECRET_APP")
    headers = {
        "Content-Type": "application/json",
        "token": token,
        "timestamp": str(ts),
        "sign": hashlib.md5(f"{ts}{app_secret}".encode()).hexdigest(),
    }
    body = {
        "homeworkId": homework_id, "lessonId": lesson_id, "type": 1,
        "platform": 2, "seriousCheckResult": 2,
    }
    try:
        resp = requests.post(
            f"{GATEWAY}/api/homeworkprod/homework/student/reportVideoPoint",
            json=body, headers=headers, timeout=REQUEST_TIMEOUT)
        log(f"    reportVideoPoint -> HTTP {resp.status_code} | {resp.text[:100]}", "OK")
    except Exception as e:
        log(f"    监测上报异常: {e}", "WARN")


def run_bfe_lesson(token: str, school_id: int, user_id: int,
                   homework_id: int, lesson_id: int, bizcode: str,
                   secret: str, session_id: str) -> bool:
    detail = get_lesson_detail(token, homework_id, lesson_id, school_id)
    point_num, video_play_time, content_type = detail
    log(f"课程 {lesson_id}: playTime 分钟数+1 -> point_num={point_num}, "
        f"videoPlayTime={video_play_time}s")

    task = get_task_info(token, school_id, homework_id, lesson_id, content_type)
    current_play = task["playTime"]
    finish_need = task["finishPlayTime"]
    log(f"当前进度 {current_play}ms / 达标 {finish_need}ms "
        f"({task['percent'] * 100:.1f}%)")
    if current_play >= finish_need:
        log(f"课程 {lesson_id} 已达标, 无需处理", "OK")
        report_video_point(token, homework_id, lesson_id)
        return True

    needed_rounds = math.ceil((finish_need - current_play) / HEARTBEAT_MS)
    log(f"剩余 {(finish_need - current_play) / 1000:.0f}s, "
        f"预计 {needed_rounds} 轮 (~{needed_rounds} 分钟)")

    begin_time = now_ms()
    last_play = current_play
    for i in range(needed_rounds):
        is_first, is_last = i == 0, i == needed_rounds - 1
        if is_first and is_last:
            action, event_type = 4, "video_oper"
        elif is_first:
            action, event_type = 2, "video_oper"
        elif is_last:
            action, event_type = 4, "video"
        else:
            action, event_type = 1, "video"

        log(f"[RUN {i + 1}/{needed_rounds}] action={action} "
            f"event_type={event_type} stay_time={HEARTBEAT_MS}ms")
        try:
            resp = report_bfe_round(
                token, session_id, user_id, school_id, lesson_id, bizcode,
                action, event_type, HEARTBEAT_MS, HEARTBEAT_MS, HEARTBEAT_MS,
                begin_time, point_num, secret)
            log(f"    Response -> HTTP {resp.status_code} | {resp.text[:100]}", "OK")
        except Exception as e:
            log(f"    请求异常: {e}", "ERR")

        if is_last:
            report_video_point(token, homework_id, lesson_id)

        # 回查进度是否增长
        time.sleep(1)
        task = get_task_info(token, school_id, homework_id, lesson_id, content_type)
        if task:
            new_play = task["playTime"]
            if new_play > last_play:
                log(f"    playTime: {last_play} -> {new_play} "
                    f"(+{new_play - last_play}ms) | {task['percent'] * 100:.1f}%", "OK")
            else:
                log(f"    [WARN] playTime 未增长: {new_play}ms "
                    f"({task['percent'] * 100:.1f}%)", "WARN")
            last_play = new_play
            if new_play >= finish_need:
                log(f"课程 {lesson_id} 已达标!", "OK")
                if not is_last:
                    report_video_point(token, homework_id, lesson_id)
                return True

        if not is_last:
            delay = INTERVAL_S + random.randint(-2, 2)
            log(f"等待 {delay}s 后进入下一轮 ...")
            for sec in range(delay, 0, -10):
                time.sleep(min(10, sec))
                log(f"    剩余 {sec - min(10, sec):>3}s", "INFO")

    task = get_task_info(token, school_id, homework_id, lesson_id, content_type)
    return bool(task and task["playTime"] >= finish_need)


# ----------------------------------------------------------------------
# fast 秒刷模式: 每门课一轮 BFE 上报, 声明剩余时长 (2026-08 实测有效)
# ----------------------------------------------------------------------
def fast_complete_lesson(token: str, school_id: int, user_id: int,
                         homework_id: int, lesson_id: int, content_type: int,
                         secret: str, session_id: str):
    """发送一个受限心跳并以真实 playTime 复查结果。

    服务器会接受 HTTP 请求但可能不记账，因此响应本身永远不是成功条件。
    单次事件也限制在 HEARTBEAT_MS 内；剩余时长较大的课程交给 bfe 模式
    按真实间隔继续，避免 fast 模式把整门课伪装成一个事件。
    """
    before = get_task_info(token, school_id, homework_id, lesson_id, content_type)
    if not before:
        return False, None, "progress-query-empty"
    current = int(before["playTime"])
    finish = int(before["finishPlayTime"])
    remain = finish - current
    if remain <= 0:
        return True, None, "already-done"
    if remain > HEARTBEAT_MS:
        return (False, None,
                f"requires-bfe; remaining={remain}ms exceeds one-event limit")

    chunk = remain
    report_at = now_ms()
    begin = report_at - chunk
    point_num = get_lesson_detail(token, homework_id, lesson_id, school_id)[0]
    # 只有最后一段才发送完成动作；point_time 是播放器的固定采样点。
    action = 4 if remain <= HEARTBEAT_MS else 2
    event_type = "video_oper"
    resp = report_bfe_round(
        token, session_id, user_id, school_id, lesson_id, BIZCODE,
        action, event_type, chunk, chunk, 60000, begin, point_num, secret,
    )
    summary = response_summary(resp)
    if not resp.ok:
        return False, resp, summary

    # BFE 入库有延迟；复查值才决定这次是否有效。
    time.sleep(PROGRESS_VERIFY_DELAY_S)
    after = get_task_info(token, school_id, homework_id, lesson_id, content_type)
    if not after:
        return False, resp, f"{summary}; progress-query-empty-after"
    new_play = int(after["playTime"])
    delta = new_play - current
    progress = f"playTime {current}->{new_play} (+{delta}ms), target={finish}"
    if new_play >= finish:
        report_video_point(token, homework_id, lesson_id)
        return True, resp, f"verified-complete; {progress}; {summary}"
    if delta > 0:
        return False, resp, f"partial-progress; {progress}; {summary}"
    return False, resp, f"no-progress; {progress}; {summary}"


def run_fast(token: str, lessons, school_id: int, user_id: int,
             secret: str, session_id: str,
             course_gap: float = 3.0, retry_gap: float = 15.0):
    """秒刷全部课程: 每门课一轮上报, 失败延迟重试一次, 结束复查"""
    todo = [l for l in lessons if not l.get("finished")]
    if not todo:
        log("没有未完成课程", "OK")
        return []
    done_n = ok_n = 0
    failed = []
    retryable = []
    total = len(todo)
    log(f"开始秒刷 {total} 门未完成课程 ...")

    for i, l in enumerate(todo, 1):
        title = (l["title"] or str(l["contentId"]))[:30]
        log(f"[{i}/{total}] {title}")
        try:
            # 每门课程建立新的播放器会话，避免跨课程被判定为并发播放。
            lesson_secret, lesson_session_id = get_player_config(token)
            ok_resp, resp, msg = fast_complete_lesson(
                token, school_id, user_id, l["homeworkId"], l["contentId"],
                l.get("contentType", 1), lesson_secret, lesson_session_id)
            if msg == "already-done":
                done_n += 1
                log("    已达标, 跳过", "OK")
            elif ok_resp:
                ok_n += 1
                log(f"    上报成功 -> {msg}", "OK")
            else:
                failed.append(l)
                log(f"    未达标 -> {msg}", "WARN")
                if msg.startswith("requires-bfe"):
                    log("    该课程剩余时长超过单包上限，请使用 bfe 模式。", "WARN")
                elif "699001" in msg or "699101" in msg:
                    failed.extend(todo[i:])
                    log("检测到服务端风控/并发播放状态，已停止本轮，避免重复请求。", "WARN")
                    return failed
                else:
                    retryable.append(l)
        except Exception as e:
            failed.append(l)
            log(f"    异常: {e}", "ERR")
        time.sleep(course_gap)

    # 失败课程延迟重试一次
    if retryable:
        log(f"{len(retryable)} 门提交未达标, 等待 {retry_gap:.0f}s 后重试 ...", "WARN")
        time.sleep(retry_gap)
        still = []
        for l in retryable:
            title = (l["title"] or str(l["contentId"]))[:30]
            log(f"[重试] {title}")
            try:
                lesson_secret, lesson_session_id = get_player_config(token)
                ok_resp, resp, msg = fast_complete_lesson(
                    token, school_id, user_id, l["homeworkId"], l["contentId"],
                    l.get("contentType", 1), lesson_secret, lesson_session_id)
                if msg == "already-done" or ok_resp:
                    ok_n += 1
                    log(f"    重试成功 -> {msg}", "OK")
                else:
                    still.append(l)
                    log(f"    仍未达标 -> {msg}", "WARN")
                    if "699001" in msg or "699101" in msg:
                        log("检测到服务端风控/并发播放状态，停止重试。", "WARN")
                        break
            except Exception as e:
                still.append(l)
                log(f"    重试异常: {e}", "ERR")
            time.sleep(course_gap)
        failed = [l for l in failed if l not in retryable] + still

    log(f"本轮: 已达标 {done_n}, 成功 {ok_n}, 失败 {len(failed)}")
    return failed


def run_diagnose(token: str, lessons, school_id: int) -> None:
    """只读检查课程进度，不发送任何播放或完成上报。"""
    done = incomplete = errors = 0
    for i, lesson in enumerate(lessons, 1):
        lesson_id = lesson["contentId"]
        title = (lesson.get("title") or str(lesson_id))[:36]
        try:
            task = get_task_info(
                token, school_id, lesson["homeworkId"], lesson_id,
                lesson.get("contentType", 1),
            )
            current = int(task["playTime"])
            finish = int(task["finishPlayTime"])
            percent = float(task.get("percent", 0)) * 100
            state = "done" if current >= finish else "incomplete"
            if state == "done":
                done += 1
            else:
                incomplete += 1
            log(f"[{i}/{len(lessons)}] {title} | playTime={current} "
                f"finishPlayTime={finish} percent={percent:.1f}% | {state}")
        except Exception as exc:
            errors += 1
            log(f"[{i}/{len(lessons)}] {title} | query-error={exc}", "WARN")
    log(f"只读诊断完成: done={done}, incomplete={incomplete}, errors={errors}")


def interactive_menu() -> None:
    """字符界面入口；无命令行参数时使用。"""
    state = {"token": None, "school_id": None, "user_id": None, "lessons": []}

    def banner() -> None:
        print("\n" + "=" * 58)
        print("        EWT360 课程工具 | 字符界面")
        print("=" * 58)
        login_state = "已登录" if state["token"] else "未登录"
        lesson_state = f"{len(state['lessons'])} 门课程" if state["lessons"] else "未扫描"
        print(f"状态: {login_state} | 课程: {lesson_state}")

    while True:
        banner()
        print("[1] 登录账号")
        print("[2] 扫描课程")
        print("[3] 只读诊断进度")
        print("[4] 快速验证未完成课程")
        print("[5] BFE 真实计时模式")
        print("[6] 退出")
        choice = input("请选择: ").strip()
        try:
            if choice == "1":
                account = input("账号: ").strip()
                password = input("密码: ")
                state["token"] = login(account, password)
                state["school_id"], state["user_id"] = get_school_user_info(state["token"])
                state["lessons"] = []
                log(f"登录成功: schoolId={state['school_id']} userId={state['user_id']}", "OK")
            elif choice == "2":
                if not state["token"]:
                    log("请先登录", "WARN")
                    continue
                state["lessons"] = scan_lessons(state["token"], state["school_id"])
                log(f"扫描到 {len(state['lessons'])} 门课程", "OK")
            elif choice == "3":
                if not state["token"] or not state["lessons"]:
                    log("请先登录并扫描课程", "WARN")
                    continue
                run_diagnose(state["token"], state["lessons"], state["school_id"])
            elif choice == "4":
                if not state["token"] or not state["lessons"]:
                    log("请先登录并扫描课程", "WARN")
                    continue
                secret, session_id = get_player_config(state["token"])
                failed = run_fast(
                    state["token"], state["lessons"], state["school_id"],
                    state["user_id"], secret, session_id,
                )
                log(f"快速验证结束，未达标 {len(failed)} 门", "WARN" if failed else "OK")
            elif choice == "5":
                if not state["token"] or not state["lessons"]:
                    log("请先登录并扫描课程", "WARN")
                    continue
                total = len(state["lessons"])
                for index, lesson in enumerate(state["lessons"], 1):
                    log(f">>>>>> [TASK {index}/{total}] 课程 {lesson['contentId']}")
                    secret, session_id = get_player_config(state["token"])
                    run_bfe_lesson(
                        state["token"], state["school_id"], state["user_id"],
                        lesson["homeworkId"], lesson["contentId"],
                        BIZCODE, secret, session_id,
                    )
                    if index < total:
                        time.sleep(5)
            elif choice == "6":
                print("已退出")
                return
            else:
                log("无效选项", "WARN")
        except KeyboardInterrupt:
            print("\n已退出")
            return
        except Exception as exc:
            log(f"操作失败: {exc}", "ERR")


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def main() -> None:
    if len(sys.argv) == 1:
        interactive_menu()
        return
    ap = argparse.ArgumentParser(description="EWT360 课程进度自动提交 (hmruu/ewt360 思路)")
    ap.add_argument("--user", help="登录账号(手机号)")
    ap.add_argument("--pass", dest="password", help="登录密码")
    ap.add_argument("--token", help="已有 token (优先于账号密码登录)")
    ap.add_argument("--mode", choices=["diagnose", "fast", "bfe", "quick"], default="fast",
                    help="diagnose=只读检查; fast=快速验证并复查; bfe=按真实间隔上报; quick=已失效")
    ap.add_argument("--homework-id", type=int, help="homeworkId (缺省自动扫描)")
    ap.add_argument("--lesson-ids", help="lessonId 列表, 逗号分隔 (缺省自动扫描)")
    ap.add_argument("--school-id", type=int, help="schoolId (缺省自动获取)")
    ap.add_argument("--bizcode", default=BIZCODE, help=f"视频业务码 (默认 {BIZCODE})")
    ap.add_argument("--header-sign", action="store_true",
                    help="给 updateUserLessonTaskV2 附加 Gateway header 签名")
    args = ap.parse_args()

    if not args.token and not (args.user and args.password):
        ap.error("需要 --token 或 --user/--pass")

    token = args.token or login(args.user, args.password)
    log(f"token: {token[:12]}...", "OK")

    school_id, user_id = get_school_user_info(token)
    if args.school_id:
        school_id = args.school_id
    log(f"schoolId={school_id}, userId={user_id}", "OK")

    if args.homework_id and args.lesson_ids:
        lessons = [{
            "contentId": int(x.strip()),
            "homeworkId": args.homework_id,
            "title": f"lesson-{x.strip()}",
            "contentType": 1,
        } for x in args.lesson_ids.split(",") if x.strip()]
    else:
        lessons = scan_lessons(token, school_id)
        if not lessons:
            log("未扫描到可处理的课程", "WARN")
            return
        log(f"扫描到 {len(lessons)} 个课程")

    if args.mode == "diagnose":
        run_diagnose(token, lessons, school_id)
        return
    if args.mode == "quick":
        run_quick(token, lessons, args.header_sign)
    else:
        secret, session_id = get_player_config(token)
        log(f"播放器配置获取成功 (sessionId={session_id[:12]}...)")
        if args.mode == "fast":
            failed = run_fast(token, lessons, school_id, user_id,
                              secret, session_id)
            if failed:
                log("以下课程未达标: 请查看 no-progress/partial-progress 原因并使用 bfe 模式:", "WARN")
                for l in failed:
                    log(f"  - {l['contentId']} {l.get('title')}", "WARN")
        else:
            for i, lesson in enumerate(lessons, 1):
                log(f">>>>>> [TASK {i}/{len(lessons)}] 课程 {lesson['contentId']}")
                lesson_secret, lesson_session_id = get_player_config(token)
                run_bfe_lesson(token, school_id, user_id,
                               lesson["homeworkId"], lesson["contentId"],
                               args.bizcode, lesson_secret, lesson_session_id)
                if i < len(lessons):
                    time.sleep(5)
    log("全部完成")


if __name__ == "__main__":
    main()
