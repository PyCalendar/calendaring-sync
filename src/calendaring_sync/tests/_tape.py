# SPDX-FileCopyrightText: 2026 calendaring-sync contributors
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP exchanges with a server, recorded once and replayed by the unit tests.

The integration tests run through a recording tape too, to change the
server between two of the adapter's requests and to read what it sent.

A :class:`Tape` takes the place of a client library's HTTP session.
Recording, it passes every request to the real session and keeps the
exchange; replaying, it answers each request with the next exchange and
checks that the request is the one recorded: its method and path, and a
JSON body when the request had one, since every JMAP call goes to one
path. Everything above the session, the client library's parsing and
errors included, runs as it does against a server. Several clients can
share one tape, so the order of their requests is kept.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from niquests.exceptions import HTTPError
from niquests.structures import CaseInsensitiveDict

#: Where the recordings live, a folder for each protocol.
DATA = Path(__file__).parent / "data"
# Response headers worth keeping; the rest (dates, server names, lengths)
# only change between recordings.
_KEPT_HEADERS = frozenset(
    {"content-type", "etag", "schedule-tag", "location", "dav", "www-authenticate"}
)
# The first status a server answers a failed request with.
_FIRST_ERROR = 400


@dataclass(frozen=True)
class Exchange:
    """One request and the server's answer to it.

    ``request`` is the request's JSON body, keys sorted, or empty when it
    had none.
    """

    method: str
    path: str
    status: int
    headers: dict[str, str]
    body: str
    request: str = ""


class _Reply:
    """An HTTP response, as a client library reads one."""

    def __init__(self, exchange: Exchange) -> None:
        self.status_code = exchange.status
        self.headers = CaseInsensitiveDict(exchange.headers)
        self.content = exchange.body.encode("utf-8")
        self.text = exchange.body
        self.reason = self.reason_phrase = ""

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= _FIRST_ERROR:
            raise HTTPError(f"{self.status_code} from a recording")


@dataclass
class _Hook:
    method: str
    marker: str
    callback: Callable[[], object]


def _json_body(kwargs: dict[str, Any]) -> str:
    """A request's JSON body, keys sorted, or empty."""
    return json.dumps(kwargs["json"], sort_keys=True) if "json" in kwargs else ""


class Tape:
    """Recorded exchanges, played back in order or recorded from a real session.

    Args:
        protocol: The protocol the exchanges speak, which names their folder.
        exchanges: The exchanges to replay; ``None`` to record.
        name: The recording the exchanges come from, ``<server>-<scenario>``.
    """

    def __init__(
        self, protocol: str, exchanges: list[Exchange] | None = None, name: str | None = None
    ) -> None:
        self.protocol = protocol
        self.name = name
        self.recording = exchanges is None
        self.exchanges: list[Exchange] = [] if exchanges is None else list(exchanges)
        self._played = 0
        self._hooks: list[_Hook] = []

    @classmethod
    def load(cls, protocol: str, name: str) -> Tape:
        """Load the recording ``name`` of ``protocol`` for replay."""
        data = json.loads((DATA / protocol / f"{name}.json").read_text(encoding="utf-8"))
        return cls(protocol, [Exchange(**exchange) for exchange in data["exchanges"]], name)

    def save(self, name: str, about: dict[str, str]) -> None:
        """Write the recording, with ``about`` saying where it came from."""
        folder = DATA / self.protocol
        folder.mkdir(parents=True, exist_ok=True)
        data = {**about, "exchanges": [asdict(exchange) for exchange in self.exchanges]}
        text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        (folder / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")

    def session(self, real: Any, *, asynchronous: bool = False) -> Any:
        """An HTTP session that sends its requests through the tape, in place of ``real``."""
        return (_AsyncSession if asynchronous else _Session)(self, real)

    def before(self, method: str, marker: str, callback: Callable[[], object]) -> None:
        """Run ``callback`` once, before the next ``method`` request whose body holds ``marker``.

        A test uses it to change the server while the adapter is between
        two requests; the callback's own requests go on the tape too.
        """
        self._hooks.append(_Hook(method, marker, callback))

    def forget_hooks(self) -> None:
        """Drop every callback still waiting, such as one for a request that must never go out."""
        self._hooks.clear()

    @property
    def finished(self) -> bool:
        """Whether every recorded exchange was played."""
        return self._played == len(self.exchanges)

    def _fire(self, method: str, body: bytes | str | None) -> None:
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body or ""
        for hook in self._hooks:
            if hook.method == method and hook.marker in text:
                self._hooks.remove(hook)
                hook.callback()
                return

    def _play(self, method: str, url: str, request: str) -> _Reply:
        path = urlsplit(url).path
        if self._played == len(self.exchanges):
            raise AssertionError(f"Unrecorded request: {method} {path}")
        exchange = self.exchanges[self._played]
        if (exchange.method, exchange.path) != (method, path):
            raise AssertionError(
                f"Request {self._played}: {method} {path}, "
                f"recorded {exchange.method} {exchange.path}"
            )
        if exchange.request and exchange.request != request:
            raise AssertionError(f"Request {self._played}: {request}, recorded {exchange.request}")
        self._played += 1
        return _Reply(exchange)

    def _keep(self, method: str, url: str, request: str, response: Any) -> None:
        headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in _KEPT_HEADERS}
        body = response.content.decode("utf-8") if response.content else ""
        self.exchanges.append(
            Exchange(method, urlsplit(url).path, response.status_code, headers, body, request)
        )


def _method_and_url(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str]:
    """The method and URL of a session request, given by position or by keyword."""
    method = kwargs["method"] if "method" in kwargs else args[0]
    url = kwargs["url"] if "url" in kwargs else args[1]
    return str(method).upper(), str(url)


class _Session:
    """A synchronous client's HTTP session, through a tape."""

    def __init__(self, tape: Tape, real: Any) -> None:
        self._tape = tape
        self._real = real

    def _start(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[str, str, str]:
        """Fire the hooks a request is due, and return its method, URL and JSON body."""
        method, url = _method_and_url(args, kwargs)
        request = _json_body(kwargs)
        self._tape._fire(method, kwargs.get("data") or kwargs.get("content") or request)
        return method, url, request

    def request(self, *args: Any, **kwargs: Any) -> Any:
        method, url, request = self._start(args, kwargs)
        if not self._tape.recording:
            return self._tape._play(method, url, request)
        response = self._real.request(*args, **kwargs)
        self._tape._keep(method, url, request, response)
        return response

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        if self._tape.recording:
            self._real.close()


class _AsyncSession(_Session):
    """An asynchronous client's HTTP session, through a tape."""

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        method, url, request = self._start(args, kwargs)
        if not self._tape.recording:
            return self._tape._play(method, url, request)
        response = await self._real.request(*args, **kwargs)
        self._tape._keep(method, url, request, response)
        return response

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)

    async def close(self) -> None:  # type: ignore[override]
        await self._close("close")

    async def aclose(self) -> None:
        """Close the session, as an httpx client does."""
        await self._close("aclose")

    async def _close(self, name: str) -> None:
        if self._tape.recording:
            await getattr(self._real, name)()
