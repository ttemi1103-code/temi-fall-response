import unittest

from fall_response.fall_detector import FallDetector, FallState
from fall_response.models import Landmark, Pose

from .helpers import FPS, TORSO, make_pose, timeline


class Scenario:
    """依序播放合成動作，記錄送出的跌倒事件。"""

    def __init__(self):
        self.detector = FallDetector()
        self.now = 0.0
        self.events = []

    def play(self, seconds, pose_at):
        for offset in timeline(seconds):
            update = self.detector.update(pose_at(offset / seconds), self.now + offset)
            if update.event:
                self.events.append(update.event)
        self.now += seconds
        return self

    def stand(self, seconds=2.0, hip_y=400.0):
        return self.play(seconds, lambda _: make_pose(hip_y=hip_y))

    def move(self, seconds, hip_from, hip_to, angle_from=0.0, angle_to=0.0):
        return self.play(
            seconds,
            lambda p: make_pose(
                hip_y=hip_from + (hip_to - hip_from) * p,
                angle_deg=angle_from + (angle_to - angle_from) * p,
            ),
        )

    def hold(self, seconds, hip_y, angle):
        return self.play(seconds, lambda _: make_pose(hip_y=hip_y, angle_deg=angle))


class FallScenarios(unittest.TestCase):
    def test_fast_fall_is_detected_after_confirm_time(self):
        s = Scenario().stand().move(0.6, 400, 400 + 2.5 * TORSO, 0, 85)
        self.assertEqual(s.events, [])
        s.hold(4.0, 400 + 2.5 * TORSO, 85)
        self.assertEqual(len(s.events), 1)
        self.assertFalse(s.events[0].occluded)
        self.assertEqual(s.events[0].position_px[1], 400 + 2.5 * TORSO)

    def test_event_records_time_since_fall_started(self):
        s = Scenario().stand().move(0.6, 400, 650, 0, 85).hold(4.0, 650, 85)
        self.assertGreaterEqual(s.events[0].onset_delay_s, 3.0)
        self.assertLess(s.events[0].onset_delay_s, 4.0)

    def test_only_one_event_until_reset(self):
        s = Scenario().stand().move(0.6, 400, 650, 0, 85).hold(10.0, 650, 85)
        self.assertEqual(len(s.events), 1)
        self.assertEqual(s.detector.state, FallState.FALLEN)
        s.detector.reset()
        s.hold(10.0, 650, 85)
        self.assertEqual(len(s.events), 1, "持續躺著不應重複警報")

    def test_fall_then_occluded_still_alerts(self):
        s = Scenario().stand().move(0.6, 400, 650, 0, 85)
        s.play(4.0, lambda _: None)
        self.assertEqual(len(s.events), 1)
        self.assertTrue(s.events[0].occluded)

    def test_tracking_lost_during_fall_is_still_detected(self):
        s = Scenario().stand().play(0.6, lambda _: None).hold(4.0, 650, 85)
        self.assertEqual(len(s.events), 1)

    def test_fall_below_speed_threshold_is_detected(self):
        s = Scenario().stand().move(1.5, 400, 520, 0, 85).hold(4.0, 520, 85)
        self.assertEqual(len(s.events), 1)

    def test_upper_body_only_learns_standing_even_with_arms_out(self):
        def upper_body(**kwargs):
            points = list(make_pose(left_wrist=(0.5, 2.0), **kwargs).landmarks)
            for index in range(25, 33):
                points[index] = Landmark(points[index].x, points[index].y, 0.0)
            return Pose(tuple(points))

        s = Scenario().play(3.0, lambda _: upper_body())
        self.assertEqual(s.detector.state, FallState.MONITORING)
        s.play(0.6, lambda p: upper_body(hip_y=400 + 250 * p, angle_deg=85 * p))
        s.play(4.0, lambda _: upper_body(hip_y=650, angle_deg=85))
        self.assertEqual(len(s.events), 1)

    def test_hips_hidden_during_fall_still_detected_from_head_and_shoulders(self):
        def hips_hidden(pose):
            points = list(pose.landmarks)
            for index in (23, 24):
                points[index] = Landmark(points[index].x, points[index].y, 0.0)
            return Pose(tuple(points))

        s = Scenario().stand()
        s.play(0.6, lambda p: hips_hidden(make_pose(hip_y=400 + 250 * p, angle_deg=30 * p)))
        s.play(4.0, lambda _: hips_hidden(make_pose(hip_y=650, angle_deg=30)))
        self.assertEqual(len(s.events), 1)
        self.assertTrue(s.events[0].occluded)

    def test_struggling_delays_alert_but_never_cancels_it(self):
        s = Scenario().stand().move(0.6, 400, 650, 0, 85)
        struggle = lambda p: make_pose(hip_y=650, hip_x=300 + 60 * ((int(p * 20) % 2) * 2 - 1), angle_deg=85)
        s.play(5.0, struggle)
        self.assertEqual(s.events, [], "還在掙扎時應該延後判定")
        self.assertEqual(s.detector.state, FallState.CONFIRMING)
        s.play(4.0, struggle)
        self.assertEqual(len(s.events), 1, "超過上限仍要判定")

    def test_stops_moving_then_alerts(self):
        s = Scenario().stand().move(0.6, 400, 650, 0, 85)
        s.play(4.0, lambda p: make_pose(hip_y=650, hip_x=300 + 60 * ((int(p * 16) % 2) * 2 - 1), angle_deg=85))
        self.assertEqual(s.events, [])
        s.hold(2.0, 650, 85)
        self.assertEqual(len(s.events), 1)

    def test_fall_then_getting_up_is_cancelled(self):
        s = Scenario().stand().move(0.6, 400, 650, 0, 85).hold(1.0, 650, 85)
        s.move(2.0, 650, 400, 85, 0).stand(5.0)
        self.assertEqual(s.events, [])
        self.assertEqual(s.detector.state, FallState.MONITORING)


class NormalActivities(unittest.TestCase):
    def test_standing_and_walking(self):
        s = Scenario().stand(5.0)
        s.play(5.0, lambda p: make_pose(hip_x=100 + 400 * p))
        self.assertEqual(s.events, [])

    def test_squat_to_pick_up_something(self):
        s = Scenario().stand().move(1.2, 400, 480, 0, 30).hold(2.0, 480, 30)
        s.move(1.2, 480, 400, 30, 0).stand()
        self.assertEqual(s.events, [])

    def test_bending_over_briefly(self):
        s = Scenario().stand().move(1.0, 400, 420, 0, 80).hold(1.5, 420, 80)
        s.move(1.0, 420, 400, 80, 0).stand()
        self.assertEqual(s.events, [])

    def test_slowly_lying_down_for_a_nap(self):
        s = Scenario().stand().move(6.0, 400, 500, 0, 90).hold(20.0, 500, 90)
        self.assertEqual(s.events, [])

    def test_sitting_down_quickly_stays_upright(self):
        s = Scenario().stand().move(0.5, 400, 500, 0, 10).hold(10.0, 500, 10)
        self.assertEqual(s.events, [])
        self.assertEqual(s.detector.state, FallState.MONITORING)

    def test_person_leaving_frame_without_descent(self):
        s = Scenario().stand().play(10.0, lambda _: None)
        self.assertEqual(s.events, [])

    def test_tracking_gap_does_not_create_false_speed(self):
        s = Scenario().stand().play(1.0, lambda _: None).stand(3.0, hip_y=650)
        self.assertEqual(s.events, [])


class KnownLimits(unittest.TestCase):
    """記錄目前演算法的已知限制：這些情況會漏報，之後要靠實測決定是否調整。"""

    def test_very_slow_collapse_is_not_detected(self):
        s = Scenario().stand().move(10.0, 400, 650, 0, 85).hold(20.0, 650, 85)
        self.assertEqual(s.events, [])

    def test_fall_into_sitting_posture_is_not_detected(self):
        s = Scenario().stand().move(0.5, 400, 650, 0, 15).hold(20.0, 650, 15)
        self.assertEqual(s.events, [])


if __name__ == "__main__":
    unittest.main()
