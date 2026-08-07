"""Offline regression checks for the release entry point."""

import ewt360_final


class Response:
    status_code = 200
    ok = True
    text = '{"success":true,"code":200}'

    def json(self):
        return {"success": True, "code": 200}


def test_oversize_is_not_submitted():
    original = {
        "task": ewt360_final.get_task_info,
        "detail": ewt360_final.get_lesson_detail,
        "report": ewt360_final.report_bfe_round,
    }
    sent = []
    try:
        ewt360_final.get_task_info = lambda *args: {
            "playTime": 0, "finishPlayTime": 180000, "percent": 0,
        }
        ewt360_final.get_lesson_detail = lambda *args: (7, 3600, 1)
        ewt360_final.report_bfe_round = lambda *args: sent.append(args)
        result = ewt360_final.fast_complete_lesson(
            "T", 1, 2, 3, 4, 1, "S", "X"
        )
        assert not result[0]
        assert result[2].startswith("requires-bfe;")
        assert not sent
    finally:
        ewt360_final.get_task_info = original["task"]
        ewt360_final.get_lesson_detail = original["detail"]
        ewt360_final.report_bfe_round = original["report"]


def test_verified_completion():
    original = {
        "sleep": ewt360_final.time.sleep,
        "task": ewt360_final.get_task_info,
        "detail": ewt360_final.get_lesson_detail,
        "report": ewt360_final.report_bfe_round,
        "point": ewt360_final.report_video_point,
    }
    states = iter((
        {"playTime": 0, "finishPlayTime": 100000, "percent": 0},
        {"playTime": 100000, "finishPlayTime": 100000, "percent": 1},
    ))
    calls = []
    try:
        ewt360_final.time.sleep = lambda _: None
        ewt360_final.get_task_info = lambda *args: next(states)
        ewt360_final.get_lesson_detail = lambda *args: (7, 3600, 1)
        ewt360_final.report_bfe_round = lambda *args: (
            calls.append(args) or Response()
        )
        ewt360_final.report_video_point = lambda *args: calls.append(("point", args))
        result = ewt360_final.fast_complete_lesson(
            "T", 1, 2, 3, 4, 1, "S", "X"
        )
        assert result[0] and result[2].startswith("verified-complete;")
        event = calls[0]
        assert event[6] == 4
        assert event[8] == event[9] == 100000
        assert event[10] == 60000
        assert any(item[0] == "point" for item in calls if isinstance(item, tuple))
    finally:
        ewt360_final.time.sleep = original["sleep"]
        ewt360_final.get_task_info = original["task"]
        ewt360_final.get_lesson_detail = original["detail"]
        ewt360_final.report_bfe_round = original["report"]
        ewt360_final.report_video_point = original["point"]


test_oversize_is_not_submitted()
test_verified_completion()
print("release offline verification: PASS")
