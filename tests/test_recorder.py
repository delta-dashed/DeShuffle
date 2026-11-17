import json
import struct
import tempfile
import time
import unittest
import wave
from pathlib import Path

from recorder.core import RATE, Session, Track


def stereo(samples: int, value: int) -> bytes:
    return struct.pack("<" + "hh" * samples, *([value, value] * samples))


class TrackTests(unittest.TestCase):
    def test_silence_is_padded_to_keep_tracks_in_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            start = time.time() - 2  # first packet arrives two seconds into the meeting
            track = Track(Path(tmp) / "a.wav", start)
            track.write(stereo(960, 5000), time.time())
            track.close()
            with wave.open(str(Path(tmp) / "a.wav")) as w:
                self.assertEqual(w.getnchannels(), 1)
                self.assertGreaterEqual(w.getnframes(), int(1.9 * RATE))

    def test_voiced_time_counts_only_loud_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = time.time()
            track = Track(Path(tmp) / "a.wav", now)
            track.write(stereo(960, 0), now)
            self.assertEqual(track.voiced, 0)
            track.write(stereo(960, 8000), now + 0.02)
            self.assertEqual(track.voiced, 960)
            track.close()


class SessionTests(unittest.TestCase):
    def test_short_session_writes_meta_without_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(1, "MasterMind", "Guild", Path(tmp))
            session.seen(10, "Kostya")
            session.feed(10, "Kostya", stereo(960, 3000))
            self.assertIsNone(session.finish())
            meta = json.loads((session.dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["people"]["10"]["name"], "Kostya")
            self.assertEqual(meta["channel"], "MasterMind")


if __name__ == "__main__":
    unittest.main()
