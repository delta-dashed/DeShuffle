"""Meeting recorder core: per-speaker tracks, DAVE frame decryption, mixdown.

Kept free of Discord command code so it can be unit-tested without credentials.
"""
from __future__ import annotations

import json
import logging
import struct
import subprocess
import time
import wave
from datetime import datetime
from pathlib import Path

log = logging.getLogger("reshuffle.recorder")

RATE = 48000
OPUS_SILENCE = b"\xf8\xff\xfe"
_patched = False


def patch_voice_recv_for_dave() -> None:
    """discord.ext.voice_recv removes only the transport encryption.

    Since Discord made end-to-end voice encryption (DAVE) mandatory, every opus
    frame is additionally encrypted per sender. discord.py 2.7 keeps the DAVE
    session on the voice connection state; decrypt each received frame with it
    before opus decoding. Frames that cannot be decrypted yet become silence.
    """
    global _patched
    if _patched:
        return
    from discord.ext.voice_recv import reader as vr_reader

    original_init = vr_reader.AudioReader.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        transport = self.decryptor.decrypt_rtp
        voice_client = self.voice_client

        def decrypt_rtp(packet):
            data = transport(packet)
            state = getattr(voice_client, "_connection", None)
            session = getattr(state, "dave_session", None)
            if session is None or not getattr(state, "dave_protocol_version", 0) or data == OPUS_SILENCE:
                return data
            user_id = voice_client._ssrc_to_id.get(packet.ssrc)
            if user_id is None:
                return OPUS_SILENCE
            try:
                if session.can_passthrough(user_id):
                    return data
                import davey
                return session.decrypt(user_id, davey.MediaType.audio, data)
            except Exception as exc:  # keys not agreed yet or a damaged frame
                log.debug("DAVE decrypt failed for %s: %s", user_id, exc)
                return OPUS_SILENCE

        self.decryptor.decrypt_rtp = decrypt_rtp

    vr_reader.AudioReader.__init__ = init
    _patched = True


class Track:
    """Mono 48 kHz WAV for one speaker, padded with silence to stay in sync."""

    def __init__(self, path: Path, start: float):
        self.path = path
        self.writer = wave.open(str(path), "wb")
        self.writer.setnchannels(1)
        self.writer.setsampwidth(2)
        self.writer.setframerate(RATE)
        self.start = start
        self.frames = 0
        self.voiced = 0

    def write(self, pcm_stereo: bytes, now: float) -> None:
        count = len(pcm_stereo) // 4
        expected = int((now - self.start) * RATE)
        gap = expected - self.frames - count
        if gap > RATE // 5:  # Discord sends nothing while a person is silent
            self.writer.writeframes(b"\x00\x00" * gap)
            self.frames += gap
        mono = bytearray(count * 2)
        loud = 0
        for i in range(count):
            left, right = struct.unpack_from("<hh", pcm_stereo, i * 4)
            value = (left + right) // 2
            struct.pack_into("<h", mono, i * 2, value)
            if value > 900 or value < -900:
                loud += 1
        self.writer.writeframes(bytes(mono))
        self.frames += count
        if loud > count // 20:
            self.voiced += count

    def close(self) -> None:
        self.writer.close()


class Session:
    def __init__(self, channel_id: int, channel_name: str, guild_name: str, root: Path):
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.guild_name = guild_name
        self.began = time.time()
        self.stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        self.dir = root / f"{self.stamp} {channel_id}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tracks: dict[int, Track] = {}
        self.people: dict[int, dict] = {}
        self.empty_since: float | None = None
        self.voice_client = None
        self.board: dict | None = None  # shared ExcaliDash board posted for this meeting

    def feed(self, user_id: int, name: str, pcm: bytes) -> None:
        track = self.tracks.get(user_id)
        if track is None:
            track = self.tracks[user_id] = Track(self.dir / f"{user_id}.wav", self.began)
        track.write(pcm, time.time())
        self.seen(user_id, name)

    def seen(self, user_id: int, name: str) -> None:
        person = self.people.setdefault(user_id, {"name": name, "joined": round(time.time() - self.began)})
        person["name"] = name
        person.pop("left", None)

    def gone(self, user_id: int) -> None:
        if user_id in self.people:
            self.people[user_id]["left"] = round(time.time() - self.began)

    def finish(self) -> Path | None:
        """Close tracks, write meta.json and mix everything into one m4a. Returns it."""
        for track in self.tracks.values():
            track.close()
        duration = round(time.time() - self.began)
        meta = {"channel": self.channel_name, "channel_id": self.channel_id, "guild": self.guild_name,
                "started": self.stamp, "duration": duration, "people": {}}
        if self.board:
            meta.update(board=self.board.get("id"), board_url=self.board.get("url"),
                        board_name=self.board.get("name"))
        for user_id, person in self.people.items():
            track = self.tracks.get(user_id)
            meta["people"][str(user_id)] = dict(person, spoke_sec=round(track.voiced / RATE) if track else 0,
                                                track=f"{user_id}.wav" if track else None)
        (self.dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        if not self.tracks or duration < 60:
            return None
        out = self.dir / "mix.m4a"
        inputs: list[str] = []
        for user_id in self.tracks:
            inputs += ["-i", str(self.dir / f"{user_id}.wav")]
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex",
                   f"amix=inputs={len(self.tracks)}:normalize=0:duration=longest,alimiter=limit=0.95",
                   "-c:a", "aac", "-b:a", "96k", str(out)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            log.error("mixdown failed: %s", result.stderr[-400:])
            return None
        return out
