import json
import tempfile
import threading
import unittest
from pathlib import Path

from fall_response.app import FallMonitorApp
from fall_response.fall_detector import FallDetector
from fall_response.incident import (
    BodyStatus,
    IncidentManager,
    IncidentTimeouts,
    LayerResult,
    Outcome,
    SimulatedPlanner,
    build_summary,
)
from fall_response.models import FallEvent, Frame

from .helpers import make_pose, timeline

EVENT = FallEvent(1.0, "2026-11-04T14:32:00", (300.0, 650.0), 3.0, False, 3.5)
VOICE_ANSWER = {"responded": True, "transcript": "右膝很痛", "pain_location": "右膝", "can_stand_up": False}


class FakeRobot:
    def __init__(self, arrives=True, error=None):
        self.arrives, self.error, self.calls = arrives, error, []

    def go_to(self, location, timeout_s):
        self.calls.append(location)
        if self.error:
            raise self.error
        return self.arrives


class FakeLayer:
    def __init__(self, name, responded=False, error=None, cost=0.0, clock=None, details=None, hang=None):
        self.name, self.responded, self.error = name, responded, error
        self.cost, self.clock, self.details, self.hang = cost, clock, details or {}, hang
        self.calls = 0

    def confirm(self, record, timeout_s):
        self.calls += 1
        if self.clock:
            self.clock.now += self.cost
        if self.hang:
            self.hang.wait(5)
        if self.error:
            raise self.error
        return LayerResult(self.responded, self.details)


class FakeNotifier:
    def __init__(self, acknowledged=True, summary_error=None):
        self.acknowledged, self.summary_error = acknowledged, summary_error
        self.summaries, self.calls = [], []

    def send_summary(self, record, summary):
        self.summaries.append(summary)
        if self.summary_error:
            raise self.summary_error

    def wait_for_ack(self, record, timeout_s):
        return self.acknowledged

    def call_contact(self, record, summary):
        self.calls.append(summary)


class FakeClock:
    now = 0.0

    def __call__(self):
        return self.now


def voice_ok():
    return FakeLayer("語音", responded=True, details=VOICE_ANSWER)


def manager(robot=None, voice=None, body=None, notifier=None, **kwargs):
    return IncidentManager(
        SimulatedPlanner(),
        robot or FakeRobot(),
        voice or FakeLayer("語音"),
        body or FakeLayer("肢體"),
        notifier or FakeNotifier(),
        **kwargs,
    )


class FlowRules(unittest.TestCase):
    def test_voice_response_still_runs_body_check(self):
        body, notifier = FakeLayer("肢體", responded=True), FakeNotifier()
        record = manager(voice=voice_ok(), body=body, notifier=notifier).handle(EVENT)
        self.assertEqual(body.calls, 1)
        self.assertEqual(record.outcome, Outcome.SUMMARY_SENT)
        self.assertEqual(record.body_status, BodyStatus.COMPLETED)
        self.assertEqual(len(notifier.summaries), 1)
        self.assertEqual(notifier.calls, [])

    def test_body_not_completed_is_recorded_and_summary_still_sent(self):
        notifier = FakeNotifier()
        record = manager(voice=voice_ok(), notifier=notifier).handle(EVENT)
        self.assertEqual(record.body_status, BodyStatus.NOT_COMPLETED)
        self.assertEqual(notifier.summaries[0]["肢體反應"], "未能完成肢體指令")
        self.assertEqual(notifier.calls, [])

    def test_no_voice_response_calls_contact_and_skips_body(self):
        body, notifier = FakeLayer("肢體", responded=True), FakeNotifier()
        record = manager(body=body, notifier=notifier).handle(EVENT)
        self.assertEqual(record.outcome, Outcome.DIRECT_CALL)
        self.assertEqual(body.calls, 0)
        self.assertEqual(notifier.summaries, [])
        self.assertEqual(len(notifier.calls), 1)
        self.assertTrue(record.contact_called)

    def test_unread_summary_makes_temi_call_contact(self):
        notifier = FakeNotifier(acknowledged=False)
        record = manager(voice=voice_ok(), notifier=notifier).handle(EVENT)
        self.assertFalse(record.contact_acknowledged)
        self.assertEqual(len(notifier.calls), 1)

    def test_read_summary_leaves_decision_to_contact(self):
        notifier = FakeNotifier(acknowledged=True)
        record = manager(voice=voice_ok(), notifier=notifier).handle(EVENT)
        self.assertTrue(record.contact_acknowledged)
        self.assertFalse(record.contact_called)


class FailureHandling(unittest.TestCase):
    def test_robot_failure_calls_contact_without_voice(self):
        for robot in (FakeRobot(arrives=False), FakeRobot(error=RuntimeError("API 無法連線"))):
            with self.subTest(arrives=robot.arrives):
                voice, notifier = voice_ok(), FakeNotifier()
                record = manager(robot=robot, voice=voice, notifier=notifier).handle(EVENT)
                self.assertEqual(voice.calls, 0)
                self.assertEqual(record.outcome, Outcome.DIRECT_CALL)
                self.assertEqual(len(notifier.calls), 1)

    def test_voice_crash_counts_as_no_response(self):
        notifier = FakeNotifier()
        record = manager(voice=FakeLayer("語音", error=RuntimeError("STT 失敗")), notifier=notifier).handle(EVENT)
        self.assertEqual(record.outcome, Outcome.DIRECT_CALL)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(len(record.errors), 1)

    def test_body_crash_is_not_reported_as_elder_failure(self):
        body = FakeLayer("肢體", error=RuntimeError("鏡頭中斷"))
        record = manager(voice=voice_ok(), body=body).handle(EVENT)
        self.assertEqual(record.body_status, BodyStatus.UNAVAILABLE)
        self.assertEqual(record.outcome, Outcome.SUMMARY_SENT)

    def test_summary_failure_falls_back_to_call(self):
        notifier = FakeNotifier(acknowledged=False, summary_error=RuntimeError("通知失敗"))
        record = manager(voice=voice_ok(), notifier=notifier).handle(EVENT)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(len(record.errors), 1)

    def test_stuck_voice_layer_is_abandoned_and_contact_is_called(self):
        release = threading.Event()
        notifier = FakeNotifier()
        timeouts = IncidentTimeouts(per_layer_s=0.05, grace_s=0.05)
        try:
            record = manager(voice=FakeLayer("語音", hang=release), notifier=notifier, timeouts=timeouts).handle(EVENT)
        finally:
            release.set()
        self.assertEqual(record.outcome, Outcome.DIRECT_CALL)
        self.assertIn("語音: timeout", record.errors)
        self.assertEqual(len(notifier.calls), 1)

    def test_overall_timeout_skips_body_check(self):
        clock = FakeClock()
        voice = FakeLayer("語音", responded=True, details=VOICE_ANSWER, cost=200.0, clock=clock)
        body = FakeLayer("肢體", responded=True)
        record = manager(voice=voice, body=body, clock=clock).handle(EVENT)
        self.assertEqual(body.calls, 0)
        self.assertEqual(record.body_status, BodyStatus.UNAVAILABLE)
        self.assertIn("overall_timeout", record.errors)


class Records(unittest.TestCase):
    def test_total_time_starts_from_fall(self):
        clock = FakeClock()
        record = manager(voice=FakeLayer("語音", cost=10.0, clock=clock), clock=clock).handle(EVENT)
        self.assertEqual(record.timeline["total_s"], 13.5)

    def test_summary_contains_only_observed_information(self):
        record = manager(voice=voice_ok(), body=FakeLayer("肢體", responded=True)).handle(EVENT)
        summary = build_summary(record)
        self.assertEqual(summary["跌倒時間"], EVENT.wall_time)
        self.assertEqual(summary["疼痛部位"], "右膝")
        self.assertEqual(summary["自行起身"], "無法")
        self.assertEqual(summary["需要協助"], "未取得")
        self.assertFalse([key for key in summary if "嚴重" in key or "傷勢" in key])

    def test_writes_json_log_without_images(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logs" / "incidents.jsonl"
            manager(voice=voice_ok(), log_path=path).handle(EVENT)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["outcome"], "summary_sent")
            self.assertEqual(saved["body_status"], "未能完成肢體指令")
            self.assertEqual(saved["event"]["wall_time"], EVENT.wall_time)


class FakeEstimator:
    def __init__(self, poses):
        self.poses = poses

    def estimate(self, frame):
        return self.poses[frame.index]


class MonitorDisplay(unittest.TestCase):
    def test_fall_alert_stays_on_screen_for_a_few_seconds(self):
        from fall_response.fall_detector import FallState, FallUpdate
        from fall_response.main import CONFIRM_BANNER, FALL_BANNER, make_monitor_callback

        banners, all_lines = [], []

        def show(frame, pose, lines, banner):
            banners.append(banner)
            all_lines.append(lines)
            return True

        callback = make_monitor_callback(show, alert_seconds=5.0)
        idle = FallUpdate(FallState.MONITORING, 0.0)
        callback(Frame(None, 0.0, 0), None, idle)
        callback(Frame(None, 0.5, 1), None, FallUpdate(FallState.CONFIRMING, 2.4))
        callback(Frame(None, 1.0, 2), None, FallUpdate(FallState.FALLEN, 0.0, EVENT))
        callback(Frame(None, 5.5, 3), None, idle)
        callback(Frame(None, 6.5, 4), None, idle)
        self.assertEqual(banners, [None, CONFIRM_BANNER, FALL_BANNER, FALL_BANNER, None])
        self.assertEqual(FALL_BANNER[0], "偵測到摔倒")
        self.assertTrue(any("peak descent (5s): 2.40" in line for line in all_lines[2]))


class AppPipeline(unittest.TestCase):
    def test_fall_runs_incident_once_and_resets_detector(self):
        poses = [make_pose() for _ in timeline(2.0)]
        fall_frames = len(timeline(0.6))
        poses += [
            make_pose(hip_y=400 + 250 * i / fall_frames, angle_deg=85 * i / fall_frames)
            for i in range(fall_frames)
        ]
        poses += [make_pose(hip_y=650, angle_deg=85) for _ in timeline(6.0)]
        frames = [Frame(None, i / 15, i) for i in range(len(poses))]

        notifier = FakeNotifier()
        detector = FallDetector()
        app = FallMonitorApp(FakeEstimator(poses), detector, manager(notifier=notifier))
        app.run(frames)

        self.assertEqual(len(app.records), 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(detector.state.value, "monitoring")
        self.assertGreater(app.records[0].event.onset_delay_s, 3.0)

    def test_callback_can_stop_the_loop(self):
        seen = []

        def stop_at_third(frame, pose, update):
            seen.append(frame.index)
            return frame.index < 2

        frames = [Frame(None, i / 15, i) for i in range(10)]
        app = FallMonitorApp(FakeEstimator([make_pose()] * 10), FallDetector(), manager())
        app.run(frames, stop_at_third)
        self.assertEqual(seen, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
