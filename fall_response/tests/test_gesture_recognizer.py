import math
import unittest

from fall_response.app import run_gesture_demo
from fall_response.gesture_recognizer import Gesture, GestureRecognizer, count_reversals
from fall_response.models import LEFT_HIP, LEFT_SHOULDER, RIGHT_HIP, RIGHT_SHOULDER, Frame, Landmark, Pose

from .helpers import make_pose, timeline


def run(pose_at, seconds=3.0):
    recognizer = GestureRecognizer()
    detected = frozenset()
    for t in timeline(seconds):
        detected = recognizer.update(pose_at(t), t)
    return detected


def oscillate(t, amplitude, hz=1.0):
    return amplitude * math.sin(2 * math.pi * hz * t)


class CountReversals(unittest.TestCase):
    def test_ignores_jitter(self):
        self.assertEqual(count_reversals([0, 1, 0, 1, 0], 5), 0)

    def test_counts_back_and_forth(self):
        self.assertEqual(count_reversals([0, 10, 0, 10, 0], 5), 3)

    def test_empty(self):
        self.assertEqual(count_reversals([], 5), 0)


class Gestures(unittest.TestCase):
    def test_still_person_makes_no_gesture(self):
        self.assertEqual(run(lambda t: make_pose()), frozenset())

    def test_raise_hand_standing(self):
        def pose(t):
            return make_pose(left_wrist=(0.9, 0.3) if t < 1 else (-0.5, 0.3))

        self.assertIn(Gesture.RAISE_HAND, run(pose))

    def test_raise_hand_while_lying_on_floor(self):
        def pose(t):
            return make_pose(angle_deg=90, left_wrist=(0.5, 0.3) if t < 1 else (0.5, 1.2))

        self.assertIn(Gesture.RAISE_HAND, run(pose))

    def test_hand_already_up_is_not_a_response(self):
        self.assertNotIn(Gesture.RAISE_HAND, run(lambda t: make_pose(left_wrist=(-0.5, 0.3))))

    def test_brief_hand_flick_is_not_raise(self):
        def pose(t):
            return make_pose(left_wrist=(-0.5, 0.3) if 1.0 <= t < 1.2 else (0.9, 0.3))

        self.assertNotIn(Gesture.RAISE_HAND, run(pose))

    def test_wave(self):
        self.assertIn(
            Gesture.WAVE, run(lambda t: make_pose(left_wrist=(-0.3, 0.5 + oscillate(t, 0.3))))
        )

    def test_nod_standing_and_lying(self):
        for angle in (0, 90):
            with self.subTest(angle=angle):
                detected = run(lambda t: make_pose(angle_deg=angle, nose_offset=(oscillate(t, 0.12), 0.0)))
                self.assertIn(Gesture.NOD, detected)
                self.assertNotIn(Gesture.SHAKE_HEAD, detected)

    def test_shake_head_standing_and_lying(self):
        for angle in (0, 90):
            with self.subTest(angle=angle):
                detected = run(lambda t: make_pose(angle_deg=angle, nose_offset=(0.0, oscillate(t, 0.12))))
                self.assertIn(Gesture.SHAKE_HEAD, detected)
                self.assertNotIn(Gesture.NOD, detected)

    def test_small_head_jitter_is_ignored(self):
        detected = run(lambda t: make_pose(nose_offset=(oscillate(t, 0.02, 3), oscillate(t, 0.02, 2))))
        self.assertEqual(detected, frozenset())

    def test_whole_body_moving_is_not_a_gesture(self):
        self.assertEqual(run(lambda t: make_pose(hip_x=300 + oscillate(t, 80))), frozenset())

    def test_single_nod_is_detected(self):
        def pose(t):
            return make_pose(nose_offset=(0.1 if 1.0 <= t < 1.5 else 0.0, 0.0))

        self.assertIn(Gesture.NOD, run(pose))

    def test_upper_body_only_can_nod_and_raise_hand(self):
        def without_hips(pose):
            points = list(pose.landmarks)
            for index in (LEFT_HIP, RIGHT_HIP):
                points[index] = Landmark(points[index].x, points[index].y, 0.0)
            return Pose(tuple(points))

        nod = run(lambda t: without_hips(make_pose(nose_offset=(oscillate(t, 0.12), 0.0))))
        self.assertIn(Gesture.NOD, nod)
        raised = run(lambda t: without_hips(make_pose(left_wrist=(0.9, 0.3) if t < 1 else (-0.5, 0.3))))
        self.assertIn(Gesture.RAISE_HAND, raised)

    def test_shoulders_not_visible_detects_nothing(self):
        def hidden(t):
            points = list(make_pose(nose_offset=(oscillate(t, 0.12), 0.0)).landmarks)
            for index in (LEFT_SHOULDER, RIGHT_SHOULDER):
                points[index] = Landmark(points[index].x, points[index].y, 0.0)
            return Pose(tuple(points))

        self.assertEqual(run(hidden), frozenset())

    def test_reset_clears_previous_detection(self):
        recognizer = GestureRecognizer()
        for t in timeline(3.0):
            recognizer.update(make_pose(nose_offset=(oscillate(t, 0.12), 0.0)), t)
        recognizer.reset()
        self.assertEqual(recognizer.update(make_pose(), 10.0), frozenset())

    def test_lost_tracking_keeps_result_without_error(self):
        recognizer = GestureRecognizer()
        self.assertEqual(recognizer.update(None, 0.0), frozenset())


class PoseInFrame:
    def estimate(self, frame):
        return frame.image


class GestureDemo(unittest.TestCase):
    def test_result_clears_after_hold_time_and_can_trigger_again(self):
        times = timeline(10.0)
        nodding = lambda t: t < 2.0 or 7.0 <= t < 9.0
        frames = [
            Frame(make_pose(nose_offset=(oscillate(t, 0.12) if nodding(t) else 0.0, 0.0)), t, i)
            for i, t in enumerate(times)
        ]
        shown = []

        def record(frame, pose, gestures):
            shown.append((frame.timestamp, gestures))

        seen = run_gesture_demo(PoseInFrame(), GestureRecognizer(), frames, record, hold_s=2.0)
        self.assertIn(Gesture.NOD, seen)
        at = lambda low, high: [g for t, g in shown if low <= t < high]
        self.assertTrue(any(Gesture.NOD in g for g in at(0.0, 2.0)))
        self.assertFalse(any(g for g in at(5.5, 6.9)), "結果應該已自動清除")
        self.assertTrue(any(Gesture.NOD in g for g in at(7.0, 10.0)), "清除後要能再次偵測")


if __name__ == "__main__":
    unittest.main()
