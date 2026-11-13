import argparse
import json
import tempfile
import unittest
from pathlib import Path

from fall_response.app import FallMonitorApp
from fall_response.fall_detector import FallDetector, FallState, FallUpdate
from fall_response.incident import IncidentManager, LayerResult
from fall_response.models import FallEvent, Frame
from fall_response.zones import Zone, ZonePlanner, load_zones, locate, save_zones

from .helpers import make_pose, timeline

SOFA = Zone("沙發前", "客廳", (0.0, 0.5, 0.5, 1.0))
TABLE = Zone("餐桌旁", "餐廳", (0.6, 0.5, 1.0, 1.0))
RUG = Zone("地毯", "客廳", (0.1, 0.6, 0.3, 0.9))  # 位於沙發前區域內


def event_at(x, y, frame_size=(1000, 800)):
    return FallEvent(1.0, "2026-11-06T10:00:00", (x, y), 3.0, False, 0.0, frame_size)


class Locate(unittest.TestCase):
    def test_inside_zone(self):
        self.assertEqual(locate([SOFA, TABLE], 0.8, 0.7), (TABLE, True))

    def test_overlapping_zones_choose_the_smaller_one(self):
        self.assertEqual(locate([SOFA, RUG], 0.2, 0.7), (RUG, True))

    def test_outside_all_zones_uses_nearest(self):
        self.assertEqual(locate([SOFA, TABLE], 0.53, 0.7), (SOFA, False))
        self.assertEqual(locate([SOFA, TABLE], 0.57, 0.7), (TABLE, False))

    def test_no_zones(self):
        self.assertIsNone(locate([], 0.5, 0.5))


class Planner(unittest.TestCase):
    def test_converts_pixels_to_zone(self):
        planner = ZonePlanner([SOFA, TABLE], "home base")
        self.assertEqual(planner.choose(event_at(800, 560)), "餐廳")
        self.assertEqual(planner.choose(event_at(200, 700)), "客廳")

    def test_same_ratio_works_at_other_resolution(self):
        planner = ZonePlanner([SOFA, TABLE], "home base")
        self.assertEqual(planner.choose(event_at(1536, 756, frame_size=(1920, 1080))), "餐廳")

    def test_unknown_frame_size_uses_fallback(self):
        self.assertEqual(ZonePlanner([SOFA], "客廳").choose(event_at(800, 560, frame_size=None)), "客廳")

    def test_no_zones_uses_fallback(self):
        self.assertEqual(ZonePlanner([], "客廳").choose(event_at(800, 560)), "客廳")

    def test_unknown_position_uses_fallback_instead_of_nearest_to_corner(self):
        event = FallEvent(1.0, "2026-11-06T10:00:00", None, 3.0, True, 0.0, (1000, 800))
        self.assertEqual(ZonePlanner([SOFA, TABLE], "定點1").choose(event), "定點1")


class Files(unittest.TestCase):
    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "zones.json"
            save_zones(path, [SOFA, TABLE])
            self.assertEqual(load_zones(path), [SOFA, TABLE])
            self.assertIn("沙發前", path.read_text(encoding="utf-8"))

    def test_missing_file_means_no_zones(self):
        self.assertEqual(load_zones("not_exists.json"), [])

    def test_location_defaults_to_zone_name(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "zones.json"
            path.write_text(json.dumps({"zones": [{"name": "客廳", "rect": [0, 0, 1, 1]}]}), encoding="utf-8")
            self.assertEqual(load_zones(path)[0].location, "客廳")

    def test_invalid_zone_is_rejected(self):
        for item in ({"name": "a", "rect": [0.5, 0, 0.2, 1]}, {"name": "a", "rect": [0, 0, 2, 1]}, {"rect": [0, 0, 1, 1]}):
            with self.subTest(item=item), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "zones.json"
                path.write_text(json.dumps({"zones": [item]}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_zones(path)


class FakeVideo:
    def __init__(self, images, is_live):
        self.images, self.is_live, self.source = images, is_live, "site.jpg"

    def frames(self):
        for index, image in enumerate(self.images):
            yield Frame(image, index / 15, index)


class SetupFrames(unittest.TestCase):
    def test_photo_or_video_keeps_showing_the_first_frame(self):
        from itertools import islice

        from fall_response.zones import setup_frames

        frames = list(islice(setup_frames(FakeVideo(["first", "second"], is_live=False)), 5))
        self.assertEqual([frame.image for frame in frames], ["first"] * 5)

    def test_live_camera_keeps_updating(self):
        from fall_response.zones import setup_frames

        frames = list(setup_frames(FakeVideo(["a", "b"], is_live=True)))
        self.assertEqual([frame.image for frame in frames], ["a", "b"])

    def test_unreadable_file_reports_error(self):
        from fall_response.zones import setup_frames

        with self.assertRaises(ValueError):
            setup_frames(FakeVideo([], is_live=False))


class FakeImage:
    shape = (800, 1000, 3)


class FakeEstimator:
    def __init__(self, poses):
        self.poses = poses

    def estimate(self, frame):
        return self.poses[frame.index]


class RecordingRobot:
    confirms_arrival = True

    def __init__(self):
        self.locations = []

    def go_to(self, location, timeout_s):
        self.locations.append(location)
        return True


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
        pass


class EndToEnd(unittest.TestCase):
    def test_fall_in_zone_sends_temi_to_its_location(self):
        # 合成姿勢的髖部在 x=800、y=650，畫面 1000x800，落在「餐桌旁」
        poses = [make_pose(hip_x=800) for _ in timeline(2.0)]
        steps = len(timeline(0.6))
        poses += [make_pose(hip_x=800, hip_y=400 + 250 * i / steps, angle_deg=85 * i / steps) for i in range(steps)]
        poses += [make_pose(hip_x=800, hip_y=650, angle_deg=85) for _ in timeline(6.0)]
        frames = [Frame(FakeImage(), i / 15, i) for i in range(len(poses))]

        robot = RecordingRobot()
        manager = IncidentManager(ZonePlanner([SOFA, TABLE], "home base"), robot, SilentLayer(), SilentLayer(), Notifier())
        app = FallMonitorApp(FakeEstimator(poses), FallDetector(), manager)
        app.run(frames)

        self.assertEqual(robot.locations, ["餐廳"])
        self.assertEqual(app.records[0].event.frame_size, (1000, 800))
        self.assertEqual(app.records[0].observation_point, "餐廳")


class Display(unittest.TestCase):
    def test_monitor_marks_the_zone_the_person_is_in(self):
        from fall_response.main import make_monitor_callback

        shown = []
        callback = make_monitor_callback(lambda *args, **kwargs: shown.append(kwargs) or True, zones=[SOFA, TABLE])
        update = FallUpdate(FallState.MONITORING, 0.0)
        callback(Frame(FakeImage(), 0.0, 0), make_pose(hip_x=800, hip_y=650), update)
        callback(Frame(FakeImage(), 0.1, 1), make_pose(hip_x=550, hip_y=200), update)
        self.assertEqual(shown[0]["active"], TABLE)
        self.assertIsNone(shown[1]["active"])

    def test_build_uses_zone_planner_when_zones_exist(self):
        from fall_response.main import build_incidents

        args = argparse.Namespace(simulate_voice_response=False, language="zh-TW", robot="simulated")
        manager = build_incidents(args, [SOFA])
        self.assertIsInstance(manager._planner, ZonePlanner)


if __name__ == "__main__":
    unittest.main()
