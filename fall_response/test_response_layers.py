import unittest

from fall_response.incident import IncidentRecord
from fall_response.models import FallEvent
from fall_response.response_layers import TAIWANESE, SimulatedVoiceClient, VoiceLayer

RECORD = IncidentRecord(FallEvent(1.0, "2026-11-04T14:32:00", (0.0, 0.0), 3.0, False), observation_point="客廳")


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

    def test_simulated_client_follows_flag(self):
        self.assertTrue(VoiceLayer(SimulatedVoiceClient(responded=True)).confirm(RECORD, 1.0).responded)
        self.assertFalse(VoiceLayer(SimulatedVoiceClient(responded=False)).confirm(RECORD, 1.0).responded)


if __name__ == "__main__":
    unittest.main()
