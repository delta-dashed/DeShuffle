"""Board settings and history in <data dir>/board.json (owner-only permissions).

The file holds the board password, so it is written with mode 600 and only
public_status() is ever shown to people.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Moscow"
KEEP_BOARDS = 30
MEETING_REUSE_SEC = 3 * 3600


def _parse_time(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class BoardStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ---------- settings ----------
    def configure(self, url: str, login: str, password: str, meeting_boards: bool = True) -> None:
        self.data.update(url=url, login=login.strip(), password=password, meeting_boards=bool(meeting_boards))
        self.save()

    def clear(self) -> None:
        for key in ("url", "login", "password"):
            self.data.pop(key, None)
        self.save()

    @property
    def configured(self) -> bool:
        return bool(self.data.get("url"))

    @property
    def meeting_boards(self) -> bool:
        return self.configured and self.data.get("meeting_boards", True)

    def credentials(self) -> tuple[str, str, str]:
        return self.data.get("url", ""), self.data.get("login", ""), self.data.get("password", "")

    @property
    def timezone(self):
        try:
            return ZoneInfo(self.data.get("timezone") or DEFAULT_TIMEZONE)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo("UTC")

    def public_status(self) -> dict:
        """Everything safe to show: never the password."""
        return {"url": self.data.get("url", ""), "login": self.data.get("login", ""),
                "password_set": bool(self.data.get("password")),
                "meeting_boards": self.meeting_boards, "boards": len(self.data.get("boards", []))}

    # ---------- history ----------
    def remember(self, board: dict, *, guild_id: int | None, channel_id: int | None,
                 meeting: bool = False, now: float | None = None) -> dict:
        entry = dict(board, guild_id=guild_id, channel_id=channel_id, meeting=meeting,
                     created=round(now if now is not None else time.time()))
        boards = self.data.setdefault("boards", [])
        boards.append(entry)
        del boards[:-KEEP_BOARDS]
        self.save()
        return entry

    def recent(self, guild_id: int | None, limit: int = 10, now: float | None = None) -> list[dict]:
        """Newest boards of this server whose edit link is still valid."""
        now = now if now is not None else time.time()
        alive = []
        for entry in reversed(self.data.get("boards", [])):
            if guild_id is not None and entry.get("guild_id") not in (None, guild_id):
                continue
            expires = _parse_time(entry.get("expires"))
            if expires is not None and expires <= now:
                continue
            alive.append(entry)
            if len(alive) >= limit:
                break
        return alive

    def meeting_board(self, channel_id: int, now: float | None = None) -> dict | None:
        """A board made for a meeting in this channel recently (bot restart mid-call)."""
        now = now if now is not None else time.time()
        for entry in self.recent(None, limit=KEEP_BOARDS, now=now):
            if entry.get("meeting") and entry.get("channel_id") == channel_id \
                    and now - entry.get("created", 0) < MEETING_REUSE_SEC:
                return entry
        return None


def default_name(channel_name: str | None, now: datetime) -> str:
    return f"{channel_name or 'Доска'} {now:%d.%m.%Y %H:%M}"


def expires_text(board: dict, tz) -> str:
    stamp = _parse_time(board.get("expires"))
    if stamp is None:
        return ""
    return datetime.fromtimestamp(stamp, tz).strftime("%d.%m %H:%M")


def board_message(board: dict, tz, *, meeting: bool = False) -> str:
    head = "🧩 Доска для этой встречи" if meeting else "🧩 Доска"
    lines = [f"{head}: {board['url']}"]
    until = expires_text(board, tz)
    details = f"«{board.get('name', '')}» — рисовать и писать можно без регистрации"
    lines.append(details + (f", ссылка действует до {until}" if until else "") + ".")
    return "\n".join(lines)


def list_message(boards: list[dict], tz) -> str:
    if not boards:
        return "Пока нет досок с действующей ссылкой. Создайте новую: /board"
    lines = ["Последние доски:"]
    for entry in boards:
        until = expires_text(entry, tz)
        lines.append(f"• «{entry.get('name', '')}» — {entry['url']}" + (f" (до {until})" if until else ""))
    return "\n".join(lines)
