import argparse
import io
import json
import unittest
import urllib.error
import urllib.parse

from fall_response.incident import IncidentManager, LayerResult, build_summary
from fall_response.models import FallEvent
from fall_response.temi_app import TemiAppController, TemiAppError
from fall_response.zones import Zone

EVENT = FallEvent(1.0, "2026-11-13T10:00:00", (0.0, 0.0), 3.0, False)


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class FakeApp:
    """模擬 Temi 上的 FallResponseServer；statuses 是每次查詢狀態時依序回傳的值。"""

    def __init__(self, clock, statuses=("going", "complete"), locations=("vr", "zone2"), goto_error=None):
        self.clock = clock
        self.statuses = list(statuses)
        self.available = list(locations)
        self.goto_error = goto_error
        self.requests = []
        self.request_id = "0"

    def __call__(self, request, timeout):
        path = urllib.parse.urlparse(request.full_url).path
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.get_method(), path, body, timeout))
        if path == "/locations":
            return FakeResponse({"locations": self.available})
        if path == "/goto":
            if self.goto_error:
                raise self.goto_error
            self.request_id = str(int(self.request_id) + 1)
            return FakeResponse({"request_id": self.request_id, "location": body["location"]})
        if path == "/status":
            status = self.statuses.pop(0) if self.statuses else "going"
            if isinstance(status, dict):
                return FakeResponse(status)
            return FakeResponse({"request_id": self.request_id, "status": status, "description": "前方有障礙物"})
        if path == "/stop":
            return FakeResponse({"ok": True})
        raise AssertionError(path)


def controller(app, clock):
    def sleep(seconds):
        clock.now += seconds

    return TemiAppController("http://temi:8080/", poll_s=0.5, opener=app, clock=clock, sleep=sleep)


class GoTo(unittest.TestCase):
    def test_returns_true_when_temi_reports_arrival(self):
        clock = Clock()
        app = FakeApp(clock, statuses=["start", "going", "going", "complete"])
        self.assertTrue(controller(app, clock).go_to("vr", timeout_s=60))
        self.assertEqual(app.requests[0][:3], ("POST", "/goto", {"location": "vr"}))
        self.assertEqual(clock.now, 2.0)

    def test_abort_raises_so_incident_calls_contact(self):
        clock = Clock()
        with self.assertRaises(TemiAppError) as context:
            controller(FakeApp(clock, statuses=["going", "abort"]), clock).go_to("vr", 60)
        self.assertIn("前方有障礙物", str(context.exception))

    def test_timeout_returns_false_within_limit(self):
        clock = Clock()
        app = FakeApp(clock, statuses=[])
        self.assertFalse(controller(app, clock).go_to("vr", timeout_s=3))
        self.assertLessEqual(clock.now, 3.0)
        self.assertTrue(all(timeout <= 5.0 for *_, timeout in app.requests))

    def test_request_timeout_never_exceeds_remaining_time(self):
        clock = Clock()
        app = FakeApp(clock, statuses=[])
        controller(app, clock).go_to("vr", timeout_s=1.2)
        self.assertLessEqual(app.requests[0][3], 1.2)

    def test_replaced_request_is_reported(self):
        clock = Clock()
        app = FakeApp(clock, statuses=[{"request_id": "99", "status": "going"}])
        with self.assertRaises(TemiAppError):
            controller(app, clock).go_to("vr", 60)

    def test_unknown_location_error_message_from_app(self):
        clock = Clock()
        error = urllib.error.HTTPError(
            "http://temi", 404, "Error", {}, io.BytesIO(json.dumps({"error": "Temi 上沒有地點：客廳"}).encode())
        )
        with self.assertRaises(TemiAppError) as context:
            controller(FakeApp(clock, goto_error=error), clock).go_to("客廳", 60)
        self.assertIn("客廳", str(context.exception))

    def test_app_not_reachable(self):
        clock = Clock()
        app = FakeApp(clock, goto_error=urllib.error.URLError("refused"))
        with self.assertRaises(TemiAppError) as context:
            controller(app, clock).go_to("vr", 60)
        self.assertIn("無法連線", str(context.exception))


class Prepare(unittest.TestCase):
    def test_all_locations_exist(self):
        clock = Clock()
        controller(FakeApp(clock), clock).prepare(["vr", "ZONE2"])

    def test_missing_location_is_reported_with_available_names(self):
        clock = Clock()
        with self.assertRaises(TemiAppError) as context:
            controller(FakeApp(clock), clock).prepare(["vr", "桌腳"])
        self.assertIn("桌腳", str(context.exception))
        self.assertIn("zone2", str(context.exception))


class SilentLayer:
    name = "語音"

    def confirm(self, record, timeout_s):
        return LayerResult(False)


class Notifier:
    def send_summary(self, record, summary):
        pass

    def wait_for_ack(self, record, timeout_s):
        return True

    def call_contact(self, record, summary):
        self.summary = summary


class WithIncidentFlow(unittest.TestCase):
    def test_arrival_is_confirmed_in_summary(self):
        from fall_response.incident import SimulatedPlanner

        clock, notifier = Clock(), Notifier()
        robot = controller(FakeApp(clock), clock)
        record = IncidentManager(SimulatedPlanner(), robot, SilentLayer(), SilentLayer(), notifier).handle(EVENT)
        self.assertTrue(record.temi_arrived)
        self.assertTrue(record.arrival_confirmed)
        self.assertEqual(build_summary(record)["Temi 到場"], "是")

    def test_main_builds_app_controller_with_zone_fallback(self):
        from fall_response.main import build_incidents
        from fall_response.zones import ZonePlanner

        args = argparse.Namespace(
            simulate_voice_response=False, language="zh-TW", robot="app", temi_app_url="http://temi:8080"
        )
        zones = [Zone("走一走跌倒", "thermal and eyeys", (0.0, 0.0, 0.5, 0.5))]
        manager = build_incidents(args, zones)
        self.assertIsInstance(manager._robot, TemiAppController)
        self.assertIsInstance(manager._planner, ZonePlanner)


if __name__ == "__main__":
    unittest.main()
