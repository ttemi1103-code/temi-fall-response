import argparse
import math
import tempfile
import unittest
from pathlib import Path

from fall_response.floor_map import (
    CalibrationPoint,
    FloorMap,
    Homography,
    NearPersonDispatch,
    load_floor_map,
    save_floor_map,
    standoff_target,
)
from fall_response.models import FallEvent
from fall_response.temi_app import TemiAppError


def true_transform(u, v):
    """測試用的已知透視轉換：模擬斜拍的鏡頭，畫面上方（遠處）的一格代表的距離比下方（近處）長。"""
    w = 1.0 + 0.8 * v
    return (4.0 * u - 1.0) / w, 6.0 * (1.2 - v) / w


CORNERS = [(0.1, 0.4), (0.9, 0.4), (0.1, 0.95), (0.9, 0.95)]


def floor_map(points=CORNERS, standoff=1.0, approach=None):
    return FloorMap([CalibrationPoint(p, true_transform(*p)) for p in points], standoff, approach)


def event_at(u, v, frame_size=(1000, 800)):
    position = None if u is None else (u * frame_size[0], v * frame_size[1])
    return FallEvent(1.0, "2026-11-18T10:00:00", position, 2.0, False, frame_size=frame_size)


class Conversion(unittest.TestCase):
    def test_four_points_recover_the_transform(self):
        homography = floor_map().homography()
        for u, v in [(0.5, 0.7), (0.2, 0.5), (0.85, 0.9)]:
            x, y = homography.to_map(u, v)
            ex, ey = true_transform(u, v)
            self.assertAlmostEqual(x, ex, places=6)
            self.assertAlmostEqual(y, ey, places=6)

    def test_extra_points_are_fitted_and_report_small_error(self):
        fm = floor_map(CORNERS + [(0.5, 0.6), (0.3, 0.8)])
        self.assertLess(max(fm.errors()), 1e-6)

    def test_points_in_a_line_are_rejected(self):
        line = [(0.1, 0.5), (0.3, 0.5), (0.6, 0.5), (0.9, 0.5)]
        with self.assertRaises(ValueError):
            floor_map(line).homography()

    def test_needs_four_measured_points(self):
        fm = FloorMap([CalibrationPoint(p, true_transform(*p)) for p in CORNERS[:3]] + [CalibrationPoint((0.5, 0.5))])
        self.assertFalse(fm.complete)
        with self.assertRaises(ValueError):
            fm.homography()

    def test_covers_only_the_measured_area(self):
        fm = floor_map()
        self.assertTrue(fm.covers(0.5, 0.7))
        self.assertFalse(fm.covers(0.5, 0.1))


class Standoff(unittest.TestCase):
    def test_stops_short_of_the_person_and_faces_them(self):
        target = standoff_target(person=(3.0, 4.0), start=(0.0, 0.0), distance=1.0)
        self.assertAlmostEqual(math.hypot(target.x - 3.0, target.y - 4.0), 1.0)
        self.assertAlmostEqual((target.x, target.y), (2.4, 3.2))
        self.assertAlmostEqual(target.yaw, math.atan2(4.0, 3.0))

    def test_already_close_only_turns_to_face(self):
        target = standoff_target(person=(1.0, 0.0), start=(0.5, 0.0), distance=1.0)
        self.assertEqual((target.x, target.y), (0.5, 0.0))
        self.assertAlmostEqual(target.yaw, 0.0)

    def test_start_on_the_person_still_gives_a_valid_target(self):
        target = standoff_target(person=(1.0, 1.0), start=(1.0, 1.0), distance=1.0)
        self.assertAlmostEqual(math.hypot(target.x - 1.0, target.y - 1.0), 1.0)


class Files(unittest.TestCase):
    def test_round_trip_keeps_unmeasured_points(self):
        original = FloorMap(
            [CalibrationPoint((0.1, 0.2), (1.0, 2.0)), CalibrationPoint((0.3, 0.4))], 0.8, (0.0, -1.0)
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "floor_map.json"
            save_floor_map(path, original)
            loaded = load_floor_map(path)
        self.assertEqual(loaded, original)

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_floor_map(Path(tempfile.gettempdir()) / "no_such_floor_map.json"))


class FakeApp:
    def __init__(self, position=(0.0, 0.0), position_result=True, position_error=None):
        self._position = position
        self.position_result = position_result
        self.position_error = position_error
        self.calls = []

    def position(self):
        if self._position is None:
            raise TemiAppError("尚未收到 Temi 的位置")
        return self._position

    def go_to_position(self, x, y, yaw, timeout_s):
        self.calls.append(("position", round(x, 3), round(y, 3)))
        if self.position_error:
            raise self.position_error
        return self.position_result

    def go_to(self, location, timeout_s):
        self.calls.append(("location", location))
        return True


class FixedPlanner:
    def choose(self, event):
        return "vr"


def dispatch(app, **kwargs):
    return NearPersonDispatch(floor_map(**kwargs), app, FixedPlanner())


class NearPerson(unittest.TestCase):
    def test_goes_next_to_the_person(self):
        app = FakeApp(position=(0.0, 0.0))
        d = dispatch(app)
        label = d.choose(event_at(0.5, 0.7))
        self.assertIn("傷患旁", label)
        self.assertTrue(d.go_to(label, 60))
        _, x, y = app.calls[0]
        px, py = true_transform(0.5, 0.7)
        self.assertAlmostEqual(math.hypot(px - x, py - y), 1.0, places=3)
        self.assertEqual(len(app.calls), 1)

    def test_blocked_path_falls_back_to_observation_point(self):
        app = FakeApp(position_error=TemiAppError("Temi 導航中止：前方有障礙物"))
        d = dispatch(app)
        self.assertTrue(d.go_to(d.choose(event_at(0.5, 0.7)), 60))
        self.assertEqual(app.calls[-1], ("location", "vr"))

    def test_unknown_fall_position_uses_observation_point(self):
        app = FakeApp()
        d = dispatch(app)
        self.assertEqual(d.choose(event_at(None, None)), "vr")
        d.go_to("vr", 60)
        self.assertEqual(app.calls, [("location", "vr")])

    def test_unknown_robot_position_uses_approach_point(self):
        app = FakeApp(position=None)
        d = dispatch(app, approach=(0.0, 0.0))
        self.assertIn("傷患旁", d.choose(event_at(0.5, 0.7)))

    def test_unknown_robot_position_without_approach_uses_observation_point(self):
        d = dispatch(FakeApp(position=None))
        self.assertEqual(d.choose(event_at(0.5, 0.7)), "vr")

    def test_main_uses_near_person_dispatch_only_with_app(self):
        from fall_response.main import build_incidents, load_near_person_map

        args = argparse.Namespace(
            simulate_voice_response=False, language="zh-TW", robot="app", temi_app_url="http://temi:8080"
        )
        manager = build_incidents(args, (), FakeApp(), floor_map())
        self.assertIsInstance(manager._planner, NearPersonDispatch)
        self.assertIs(manager._planner, manager._robot)

        with self.assertRaises(ValueError):
            load_near_person_map(argparse.Namespace(robot="rim", floor_map="floor_map.json"))


if __name__ == "__main__":
    unittest.main()
