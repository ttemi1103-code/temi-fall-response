import tempfile
import time
import unittest
from pathlib import Path

from fall_response.video_input import VideoSource, is_live_source, parse_source


class FakeCapture:
    def __init__(self, reads=(), opened=True, fps=10.0):
        self.reads = list(reads)
        self.opened, self.fps, self.released = opened, fps, False

    def isOpened(self):
        return self.opened

    def get(self, _prop):
        return self.fps

    def read(self):
        if not self.reads:
            time.sleep(0.01)
            return False, None
        return True, self.reads.pop(0)

    def release(self):
        self.released = True


class Sources(unittest.TestCase):
    def test_parse_source(self):
        self.assertEqual(parse_source("0"), 0)
        self.assertEqual(parse_source("http://192.168.1.5:8080/video"), "http://192.168.1.5:8080/video")

    def test_is_live(self):
        self.assertTrue(is_live_source(0))
        self.assertTrue(is_live_source("rtsp://cam/stream"))
        self.assertFalse(is_live_source("clip.mp4"))

    def test_url_credentials_are_hidden_in_logs(self):
        source = VideoSource("rtsp://user:secret@192.168.1.5/stream")
        self.assertNotIn("secret", source._safe_name())


class FileSource(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            list(VideoSource("not_exists.mp4").frames())

    def test_reads_all_frames_with_video_time(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            path = handle.name
        try:
            capture = FakeCapture(reads=["a", "b", "c"], fps=10.0)
            frames = list(VideoSource(path, capture_factory=lambda _: capture).frames())
            self.assertEqual([f.image for f in frames], ["a", "b", "c"])
            self.assertEqual([f.timestamp for f in frames], [0.0, 0.1, 0.2])
            self.assertTrue(capture.released)
        finally:
            Path(path).unlink()

    def test_unopenable_file_raises(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as handle:
            path = handle.name
        try:
            source = VideoSource(path, capture_factory=lambda _: FakeCapture(opened=False))
            with self.assertRaises(RuntimeError):
                list(source.frames())
        finally:
            Path(path).unlink()


class LiveSource(unittest.TestCase):
    def test_reconnects_after_stream_drops(self):
        captures = [FakeCapture(opened=False), FakeCapture(reads=["first"]), FakeCapture(reads=["second"])]
        source = VideoSource(0, capture_factory=lambda _: captures.pop(0), reconnect_delay_s=0.01)
        received = []
        try:
            for frame in source.frames():
                received.append(frame.image)
                if len(received) == 2:
                    break
        finally:
            source.close()
        self.assertEqual(received, ["first", "second"])


if __name__ == "__main__":
    unittest.main()
