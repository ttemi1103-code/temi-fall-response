import argparse
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from fall_response.incident import IncidentManager, LayerResult, build_summary
from fall_response.models import FallEvent
from fall_response.temi_rim import (
    TOKEN_LIFETIME_S,
    ConfiguredPlanner,
    RimClient,
    RimError,
    RimRobotController,
    TemiConfig,
    load_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "secret-pass"
EVENT = FallEvent(1.0, "2026-11-06T10:00:00", (0.0, 0.0), 3.0, False)


def config(**overrides):
    values = dict(authsid="team", username="user", password=PASSWORD, routes={"客廳": "跌倒應變-客廳"})
    values.update(overrides)
    return TemiConfig(**values)


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def success(data):
    return {"status": "success", "code": 10005, "msg": "查詢成功", "data": data}


class FakeRim:
    """模擬 RIM2 伺服器，記錄收到的請求。"""

    def __init__(self, robots=None, routes=None, execute=None, execute_errors=(), login_error=None, on_execute=None):
        self.requests = []
        self.timeouts = []
        self.robots = robots if robots is not None else [{"id": 48, "name": "測試機"}]
        self.routes = routes if routes is not None else [{"id": 116, "name": "跌倒應變-客廳"}]
        self.execute = execute
        self.execute_errors = list(execute_errors)  # 依序丟出，用完後恢復正常
        self.login_error = login_error
        self.on_execute = on_execute

    def __call__(self, request, timeout):
        url = urllib.parse.urlparse(request.full_url)
        body = json.loads(request.data) if request.data else None
        query = dict(urllib.parse.parse_qsl(url.query))
        self.requests.append((request.get_method(), url.path, query, body, request.get_header("Authorization")))
        self.timeouts.append(timeout)
        if url.path.endswith("/login"):
            if self.login_error:
                raise self.login_error
            return FakeResponse(success({"token": f"token-{len(self.requests)}"}))
        if url.path.endswith("/robot/robots"):
            return FakeResponse(success(self.robots))
        if url.path.endswith("/eroute/eroutes"):
            return FakeResponse(success(self.routes))
        if url.path.endswith("/eroute/execute"):
            if self.on_execute:
                self.on_execute()
            if self.execute_errors:
                raise self.execute_errors.pop(0)
            if self.execute:
                raise self.execute
            return FakeResponse(success({}))
        raise AssertionError(f"unexpected path {url.path}")

    def paths(self):
        return [path.rsplit("/v1", 1)[1] for _, path, _, _, _ in self.requests]


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def controller(rim, cfg=None, clock=None, **options):
    cfg, clock = cfg or config(), clock or Clock()
    slept = []
    client = RimClient(cfg, opener=rim, clock=clock)
    robot = RimRobotController(client, cfg, sleep=slept.append, clock=clock, **options)
    return robot, slept


def http_error(code, payload):
    return urllib.error.HTTPError("https://rim2", code, "error", {}, io.BytesIO(json.dumps(payload).encode()))


class Dispatch(unittest.TestCase):
    def test_finds_route_by_name_and_executes(self):
        rim = FakeRim()
        robot, slept = controller(rim, config(arrival_wait_s=15))
        self.assertTrue(robot.go_to("客廳", timeout_s=60))
        self.assertEqual(rim.paths(), ["/login", "/robot/robots", "/eroute/eroutes", "/eroute/execute"])
        method, _, query, _, auth = rim.requests[-1]
        self.assertEqual((method, query), ("GET", {"robot_id": "48", "route_id": "116"}))
        self.assertEqual(auth, "Bearer token-1")
        self.assertEqual(slept, [15])

    def test_login_sends_credentials_in_body(self):
        rim = FakeRim()
        controller(rim)[0].go_to("客廳", 60)
        method, _, _, body, auth = rim.requests[0]
        self.assertEqual(method, "POST")
        self.assertEqual(body, {"authsid": "team", "username": "user", "password": PASSWORD})
        self.assertIsNone(auth)

    def test_numeric_route_skips_lookup(self):
        rim = FakeRim()
        controller(rim, config(routes={"客廳": 200}))[0].go_to("客廳", 60)
        self.assertEqual(rim.paths(), ["/login", "/robot/robots", "/eroute/execute"])
        self.assertEqual(rim.requests[-1][2]["route_id"], "200")

    def test_wait_never_exceeds_navigation_timeout(self):
        robot, slept = controller(FakeRim(), config(arrival_wait_s=90))
        robot.go_to("客廳", timeout_s=60)
        self.assertEqual(slept, [60])

    def test_second_dispatch_reuses_ids_and_token(self):
        rim = FakeRim()
        robot, _ = controller(rim)
        robot.go_to("客廳", 60)
        robot.go_to("客廳", 60)
        self.assertEqual(rim.paths()[4:], ["/eroute/execute"])

    def test_token_is_renewed_before_it_expires(self):
        rim, clock = FakeRim(), Clock()
        robot, _ = controller(rim, clock=clock)
        robot.go_to("客廳", 60)
        clock.now = TOKEN_LIFETIME_S + 1
        robot.go_to("客廳", 60)
        self.assertEqual(rim.paths()[4:], ["/login", "/eroute/execute"])

    def test_dry_run_looks_up_route_but_never_executes(self):
        rim = FakeRim()
        robot, slept = controller(rim, dry_run=True)
        self.assertTrue(robot.go_to("客廳", 60))
        self.assertNotIn("/eroute/execute", rim.paths())
        self.assertEqual(rim.paths(), ["/login", "/robot/robots", "/eroute/eroutes"])
        self.assertEqual(slept, [])

    def test_dry_run_still_reports_missing_route(self):
        robot, _ = controller(FakeRim(routes=[]), dry_run=True)
        with self.assertRaises(RimError):
            robot.go_to("客廳", 60)

    def test_selects_robot_by_name(self):
        rim = FakeRim(robots=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])
        controller(rim, config(robot_name="B"))[0].go_to("客廳", 60)
        self.assertEqual(rim.requests[-1][2]["robot_id"], "2")


class SafeDispatch(unittest.TestCase):
    """派遣時要避免派錯車、避免逾時後背景仍在派遣。"""

    def test_real_dispatch_requires_robot_name(self):
        rim = FakeRim(robots=[{"id": 1, "name": "別組的"}, {"id": 2, "name": "我們的"}])
        robot, _ = controller(rim, require_robot_name=True)
        with self.assertRaises(RimError) as context:
            robot.go_to("客廳", 60)
        self.assertIn("robot_name", str(context.exception))
        self.assertNotIn("/eroute/execute", rim.paths())

    def test_real_dispatch_with_robot_name(self):
        rim = FakeRim(robots=[{"id": 1, "name": "別組的"}, {"id": 2, "name": "我們的"}])
        controller(rim, config(robot_name="我們的"), require_robot_name=True)[0].go_to("客廳", 60)
        self.assertEqual(rim.requests[-1][2]["robot_id"], "2")

    def test_prepare_resolves_everything_so_dispatch_is_one_request(self):
        rim = FakeRim(routes=[{"id": 116, "name": "跌倒應變-客廳"}, {"id": 117, "name": "跌倒應變-餐廳"}])
        cfg = config(routes={"客廳": "跌倒應變-客廳", "餐廳": "跌倒應變-餐廳"})
        robot, _ = controller(rim, cfg)
        robot.prepare(["客廳", "餐廳"])
        before = len(rim.requests)
        robot.go_to("餐廳", 60)
        self.assertEqual(rim.paths()[before:], ["/eroute/execute"])
        self.assertEqual(rim.requests[-1][2]["route_id"], "117")

    def test_prepare_reports_missing_location_before_any_fall(self):
        robot, _ = controller(FakeRim())
        with self.assertRaises(RimError) as context:
            robot.prepare(["客廳", "定點2"])
        self.assertIn("定點2", str(context.exception))

    def test_go_to_returns_within_timeout_even_if_request_is_slow(self):
        clock = Clock()

        def slow():
            clock.now += 50

        rim = FakeRim(on_execute=slow)
        robot, slept = controller(rim, config(arrival_wait_s=20), clock=clock)
        robot.prepare(["客廳"])
        robot.go_to("客廳", timeout_s=60)
        self.assertEqual(slept, [10])

    def test_request_timeout_never_exceeds_navigation_time(self):
        rim = FakeRim()
        robot, _ = controller(rim, config(request_timeout_s=30))
        robot.prepare(["客廳"])
        robot.go_to("客廳", timeout_s=5)
        self.assertLessEqual(rim.timeouts[-1], 5)


class Reauthentication(unittest.TestCase):
    def test_relogin_once_when_token_is_rejected(self):
        rim = FakeRim(execute_errors=[http_error(401, {"status": "fail", "msg": "token invalid"})])
        robot, _ = controller(rim)
        self.assertTrue(robot.go_to("客廳", 60))
        self.assertEqual(rim.paths()[-3:], ["/eroute/execute", "/login", "/eroute/execute"])

    def test_gives_up_after_second_rejection(self):
        error = http_error(401, {"status": "fail", "msg": "token invalid"})
        robot, _ = controller(FakeRim(execute_errors=[error, error]))
        with self.assertRaises(RimError) as context:
            robot.go_to("客廳", 60)
        self.assertIn("驗證失敗", str(context.exception))

    def test_wrong_password_does_not_loop(self):
        rim = FakeRim(login_error=http_error(401, {"status": "fail", "msg": "wrong password"}))
        robot, _ = controller(rim)
        with self.assertRaises(RimError) as context:
            robot.go_to("客廳", 60)
        self.assertIn("驗證失敗", str(context.exception))
        self.assertEqual(rim.paths(), ["/login"])
        self.assertNotIn(PASSWORD, str(context.exception))


class Errors(unittest.TestCase):
    def assert_fails(self, rim, cfg=None, message=""):
        with self.assertRaises(RimError) as context:
            controller(rim, cfg)[0].go_to("客廳", 60)
        self.assertIn(message, str(context.exception))
        self.assertNotIn(PASSWORD, str(context.exception))

    def test_unknown_location(self):
        self.assert_fails(FakeRim(), config(routes={"臥室": 1}), "客廳")

    def test_unknown_route_lists_available_names(self):
        self.assert_fails(FakeRim(routes=[{"id": 1, "name": "巡邏"}]), message="巡邏")

    def test_unknown_robot_name(self):
        self.assert_fails(FakeRim(), config(robot_name="不存在"), "測試機")

    def test_no_robot(self):
        self.assert_fails(FakeRim(robots=[]), message="沒有機器人")

    def test_robot_timeout_from_rim(self):
        rim = FakeRim(execute=http_error(504, {"status": "fail", "code": 10012, "msg": "Timeout", "data": {}}))
        self.assert_fails(rim, message="Timeout")

    def test_http_error_without_json(self):
        error = urllib.error.HTTPError("https://rim2", 500, "error", {}, io.BytesIO(b"<html>"))
        self.assert_fails(FakeRim(execute=error), message="HTTP 500")

    def test_network_down(self):
        self.assert_fails(FakeRim(execute=urllib.error.URLError("unreachable")), message="無法連線")

    def test_config_repr_hides_password(self):
        self.assertNotIn(PASSWORD, repr(config()))


class Config(unittest.TestCase):
    def write(self, folder, data):
        path = Path(folder) / "temi_config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_example_file_is_valid(self):
        loaded = load_config(PROJECT_ROOT / "temi_config.example.json")
        self.assertEqual(loaded.default_location, "客廳")
        self.assertIn(loaded.default_location, loaded.routes)

    def test_missing_file(self):
        with self.assertRaises(RimError) as context:
            load_config("not_exists.json")
        self.assertIn("temi_config.example.json", str(context.exception))

    def test_missing_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(RimError) as context:
                load_config(self.write(folder, {"authsid": "a"}))
        self.assertIn("password", str(context.exception))

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "temi_config.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(RimError):
                load_config(path)

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            data = {"authsid": "a", "username": "u", "password": "p", "routes": {"客廳": 1}, "note": "x"}
            self.assertEqual(load_config(self.write(folder, data)).routes, {"客廳": 1})


class FakeLayer:
    name = "語音"

    def confirm(self, record, timeout_s):
        return LayerResult(False)


class FakeNotifier:
    def send_summary(self, record, summary):
        pass

    def wait_for_ack(self, record, timeout_s):
        return True

    def call_contact(self, record, summary):
        self.summary = summary


class WithIncidentFlow(unittest.TestCase):
    def test_dispatch_is_reported_as_unconfirmed_arrival(self):
        rim, notifier = FakeRim(), FakeNotifier()
        robot, _ = controller(rim)
        manager = IncidentManager(ConfiguredPlanner("客廳"), robot, FakeLayer(), FakeLayer(), notifier)
        record = manager.handle(EVENT)
        self.assertTrue(record.temi_arrived)
        self.assertFalse(record.arrival_confirmed)
        self.assertEqual(record.observation_point, "客廳")
        self.assertEqual(notifier.summary["Temi 到場"], "已派遣（未確認抵達）")

    def test_rim_failure_still_calls_contact(self):
        rim, notifier = FakeRim(execute=urllib.error.URLError("down")), FakeNotifier()
        robot, _ = controller(rim)
        record = IncidentManager(ConfiguredPlanner("客廳"), robot, FakeLayer(), FakeLayer(), notifier).handle(EVENT)
        self.assertFalse(record.temi_arrived)
        self.assertEqual(notifier.summary["Temi 到場"], "否")
        self.assertTrue(any("navigation" in error for error in record.errors))

    def test_simulated_robot_summary_unchanged(self):
        from fall_response.incident import IncidentRecord

        record = IncidentRecord(EVENT, temi_arrived=True, arrival_confirmed=True)
        self.assertEqual(build_summary(record)["Temi 到場"], "是")


class MainWiring(unittest.TestCase):
    def args(self, **overrides):
        values = dict(
            simulate_voice_response=False, language="zh-TW", robot="simulated",
            temi_config=str(PROJECT_ROOT / "temi_config.example.json"),
        )
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_rim_mode_builds_without_connecting(self):
        from fall_response.main import build_incidents

        manager = build_incidents(self.args(robot="rim"))
        self.assertIsInstance(manager._robot, RimRobotController)
        self.assertEqual(manager._planner.choose(EVENT), "客廳")

    def test_dry_run_mode_never_executes(self):
        from fall_response.main import build_incidents

        manager = build_incidents(self.args(robot="dry-run"))
        self.assertTrue(manager._robot._dry_run)
        self.assertFalse(manager._robot._require_robot_name)

    def test_rim_mode_requires_robot_name(self):
        from fall_response.main import build_incidents

        self.assertTrue(build_incidents(self.args(robot="rim"))._robot._require_robot_name)

    def test_prepared_robot_is_reused(self):
        from fall_response.main import build_incidents

        robot, _ = controller(FakeRim())
        self.assertIs(build_incidents(self.args(robot="rim"), robot=robot)._robot, robot)

    def test_simulated_is_default(self):
        from fall_response.incident import SimulatedRobot
        from fall_response.main import build_incidents

        manager = build_incidents(self.args())
        self.assertIsInstance(manager._robot, SimulatedRobot)


if __name__ == "__main__":
    unittest.main()
