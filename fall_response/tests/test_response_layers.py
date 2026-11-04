import math
import threading
import unittest

from fall_response.incident import IncidentRecord
from fall_response.models import FallEvent, Frame
from fall_response.response_layers import BODY_INSTRUCTIONS, TAIWANESE, BodyLayer, VoiceLayer

from .helpers import make_pose, timeline

RECORD = IncidentRecord(FallEvent(1.0, "2026-11-04T14:32:00", (0.0, 0.0), 3.0, False), observation_point="客廳")


class FakeSpeaker:
    def __init__(self):
        self.said = []

    def speak(self, text, language):
        self.said.append((text, language))


class PoseFrames:
    """把合成姿勢當成影像來源：Frame.image 直接放姿勢，由 FakeEstimator 取出。"""

    def __init__(self, poses, then_block=False):
        self.poses, self.then_block = poses, then_block
        self.closed = threading.Event()

    def frames(self):
        for index, pose in enumerate(self.poses):
            yield Frame(pose, index / 15, index)
        if self.then_block:
            self.closed.wait(5)

    def close(self):
        self.closed.set()


class FakeEstimator:
    def estimate(self, frame):
        return frame.image


class FakeClient:
    def __init__(self, answer):
        self.answer, self.kwargs = answer, None

    def confirm_voice(self, **kwargs):
        self.kwargs = kwargs
        return self.answer


class Voice(unittest.TestCase):
    def test_passes_language_and_event_to_voice_group(self):
        client = FakeClient({"responded": True, "transcript": "腳真疼"})
        result = VoiceLayer(client, TAIWANESE).confirm(RECORD, 20.0)
        self.assertTrue(result.responded)
        self.assertEqual(result.details["transcript"], "腳真疼")
        self.assertEqual(client.kwargs["language"], TAIWANESE)
        self.assertEqual(client.kwargs["location"], "客廳")
        self.assertEqual(client.kwargs["timeout_s"], 20.0)

    def test_only_explicit_true_counts_as_response(self):
        for answer in ({}, {"responded": False}, {"responded": "yes"}):
            with self.subTest(answer=answer):
                self.assertFalse(VoiceLayer(FakeClient(answer)).confirm(RECORD, 1.0).responded)


class Body(unittest.TestCase):
    def layer(self, source, language="zh-TW"):
        self.speaker = FakeSpeaker()
        return BodyLayer(self.speaker, FakeEstimator(), lambda: source, language)

    def test_nod_after_instruction_is_completed(self):
        poses = [
            make_pose(angle_deg=90, nose_offset=(0.12 * math.sin(2 * math.pi * t), 0.0)) for t in timeline(3.0)
        ]
        source = PoseFrames(poses)
        result = self.layer(source).confirm(RECORD, 5.0)
        self.assertTrue(result.responded)
        self.assertEqual(result.details["gestures"], ["nod"])
        self.assertTrue(source.closed.is_set())

    def test_no_movement_is_not_completed(self):
        result = self.layer(PoseFrames([make_pose(angle_deg=90) for _ in timeline(3.0)])).confirm(RECORD, 5.0)
        self.assertFalse(result.responded)

    def test_head_shake_is_not_the_requested_action(self):
        poses = [make_pose(nose_offset=(0.0, 0.12 * math.sin(2 * math.pi * t))) for t in timeline(3.0)]
        self.assertFalse(self.layer(PoseFrames(poses)).confirm(RECORD, 5.0).responded)

    def test_stream_without_frames_ends_at_timeout(self):
        result = self.layer(PoseFrames([], then_block=True)).confirm(RECORD, 0.05)
        self.assertFalse(result.responded)

    def test_instruction_uses_selected_language(self):
        self.layer(PoseFrames([]), TAIWANESE).confirm(RECORD, 1.0)
        self.assertEqual(self.speaker.said, [(BODY_INSTRUCTIONS[TAIWANESE], TAIWANESE)])


if __name__ == "__main__":
    unittest.main()
