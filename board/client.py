"""Minimal ExcaliDash client: sign in, create a board, open it for editing by link.

ExcaliDash 0.6.x accepts account API keys only for /drawings and /collections
themselves; /drawings/<id>/link-shares answers 403 to them. A link that lets
people without an account edit the board therefore needs a signed-in session:
the client signs in with a login and password (cookie session plus the CSRF
token the backend requires for every write). When sign-in is switched off on
the board (single-user mode) no credentials are needed at all.

Errors reach Discord users, so BoardError messages are short Russian sentences
and never contain the password.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

LINK_DAYS = 7
TIMEOUT = 20


class BoardError(Exception):
    """A user-facing problem with the board (message is shown in Discord)."""


def normalize_url(url: str) -> str:
    """https://host:8443/, https://host:8443/api -> https://host:8443"""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise BoardError("Адрес доски должен начинаться с https:// (или http://).")
    return url


class AiohttpTransport:
    """The only part that talks to the network; tests replace it."""

    def __init__(self, timeout: float = TIMEOUT):
        self.timeout = timeout
        self._session = None

    async def request(self, method: str, url: str, *, headers: dict | None = None,
                      json: Any = None) -> tuple[int, Any]:
        import aiohttp

        if self._session is None or self._session.closed:
            # unsafe=True keeps cookies for hosts given as an IP address too
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                timeout=aiohttp.ClientTimeout(total=self.timeout))
        async with self._session.request(method, url, headers=headers, json=json,
                                         allow_redirects=False) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                data = None
            return response.status, data

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


def _message(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or "")
    return ""


def _csrf_rejected(status: int, data: Any) -> bool:
    return status == 403 and isinstance(data, dict) and "CSRF" in str(data.get("error", ""))


class ExcaliDash:
    def __init__(self, url: str, login: str = "", password: str = "", *, transport=None):
        self.base = normalize_url(url)
        self.login_name = (login or "").strip()
        self._password = password or ""
        self.transport = transport or AiohttpTransport()
        self._csrf: tuple[str, str] | None = None  # (header name, token)
        self._signed_in = False
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:  # never show the password
        return f"ExcaliDash({self.base!r}, login={self.login_name!r})"

    # ---------- links ----------
    def shared_url(self, drawing_id: str) -> str:
        return f"{self.base}/shared/{drawing_id}"

    # ---------- low level ----------
    async def _send(self, method: str, path: str, *, json: Any = None, csrf: bool = False) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if csrf:
            if self._csrf is None:
                await self._fetch_csrf()
            name, token = self._csrf
            headers[name] = token
        try:
            return await self.transport.request(method, f"{self.base}/api{path}", headers=headers, json=json)
        except asyncio.TimeoutError:
            raise BoardError("Доска не ответила вовремя. Попробуйте позже.") from None
        except Exception as exc:  # aiohttp.ClientError, DNS, TLS...
            raise BoardError(f"Доска недоступна ({type(exc).__name__}).") from None

    async def _fetch_csrf(self) -> None:
        status, data = await self._send("GET", "/csrf-token")
        if status != 200 or not isinstance(data, dict) or not data.get("token"):
            raise BoardError(self._status_text(status, data, "не выдала CSRF-токен"))
        self._csrf = (str(data.get("header") or "x-csrf-token"), str(data["token"]))

    @staticmethod
    def _status_text(status: int, data: Any, what: str) -> str:
        if status in (301, 302, 307, 308):
            return "Доска перенаправляет запрос — проверьте адрес (нужен https:// и тот же адрес, что в браузере)."
        if status == 429:
            return "Доска просит подождать: слишком много запросов. Попробуйте через несколько минут."
        if status >= 500:
            return f"Доска ответила ошибкой {status}. Попробуйте позже."
        detail = _message(data)
        return f"ExcaliDash {what} (ответ {status}{': ' + detail if detail else ''})."

    async def auth_status(self) -> dict:
        status, data = await self._send("GET", "/auth/status")
        if status != 200 or not isinstance(data, dict):
            raise BoardError(self._status_text(status, data, "не ответила на запрос состояния — проверьте адрес"))
        return data

    async def sign_in(self) -> None:
        info = await self.auth_status()
        if info.get("authOnboardingRequired"):
            raise BoardError("На доске не завершена первичная настройка: откройте её в браузере и выберите режим входа.")
        if not info.get("authEnabled", info.get("enabled", True)):
            self._signed_in = True  # single-user mode: every request acts as the owner
            return
        if info.get("bootstrapRequired"):
            raise BoardError("На доске ещё не создан администратор — сначала настройте вход на самой доске.")
        if not self.login_name or not self._password:
            raise BoardError("На доске включён вход: укажите в /board_setup логин и пароль учётной записи для бота.")
        body = {"identifier": self.login_name, "password": self._password}
        status, data = await self._send("POST", "/auth/login", json=body, csrf=True)
        if _csrf_rejected(status, data):  # stale token: fetch a fresh one once
            self._csrf = None
            status, data = await self._send("POST", "/auth/login", json=body, csrf=True)
        if status == 401:
            raise BoardError("Доска не приняла логин или пароль бота.")
        if status != 200:
            raise BoardError(self._status_text(status, data, "не пустила бота"))
        user = (data or {}).get("user") or {}
        if user.get("mustResetPassword"):
            raise BoardError("Учётной записи бота нужно сменить пароль: войдите под ней в браузере, "
                             "смените пароль и повторите /board_setup.")
        self._signed_in = True

    async def _call(self, method: str, path: str, *, json: Any = None, what: str = "не выполнила запрос") -> Any:
        write = method not in ("GET", "HEAD")
        for attempt in range(2):
            if not self._signed_in:
                await self.sign_in()
            status, data = await self._send(method, path, json=json, csrf=write)
            if status == 401 and attempt == 0:  # access token expired (15 min by default)
                self._signed_in = False
                continue
            if attempt == 0 and _csrf_rejected(status, data):
                self._csrf = None
                continue
            if status == 403 and isinstance(data, dict) and data.get("code") == "MUST_RESET_PASSWORD":
                raise BoardError("Учётной записи бота нужно сменить пароль: войдите под ней в браузере.")
            if 200 <= status < 300:
                return data
            raise BoardError(self._status_text(status, data, what))
        raise BoardError("Доска не приняла вход бота.")

    # ---------- high level ----------
    async def check(self) -> dict:
        """Sign in and count boards; raises BoardError with the reason."""
        async with self._lock:
            self._signed_in = False
            await self.sign_in()
            data = await self._call("GET", "/drawings?limit=1", what="не отдала список досок")
        return {"boards": (data or {}).get("totalCount")}

    async def create_board(self, name: str, *, link_days: int = LINK_DAYS, now: datetime | None = None) -> dict:
        """Create an empty board and open it for editing by link. Returns id, name, url, expires."""
        async with self._lock:
            drawing = await self._call("POST", "/drawings",
                                       json={"name": name[:255] or "Доска", "elements": [], "appState": {}},
                                       what="не создала доску")
            drawing_id = (drawing or {}).get("id")
            if not drawing_id:
                raise BoardError("Доска не вернула номер новой доски.")
            expires = (now or datetime.now(timezone.utc)) + timedelta(days=link_days)
            share = await self._call("POST", f"/drawings/{drawing_id}/link-shares",
                                     json={"permission": "edit",
                                           "expiresAt": expires.astimezone(timezone.utc).isoformat()},
                                     what="не открыла доску по ссылке")
            expires_at = ((share or {}).get("share") or {}).get("expiresAt") or expires.isoformat()
        return {"id": drawing_id, "name": drawing.get("name") or name, "url": self.shared_url(drawing_id),
                "expires": expires_at}

    async def close(self) -> None:
        await self.transport.close()
