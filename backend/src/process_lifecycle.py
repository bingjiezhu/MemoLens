from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address
import signal
import threading
from types import FrameType
from typing import Protocol

from . import shutdown_runtime_extensions


class _RunnableApp(Protocol):
    extensions: dict[str, object]

    def run(self, **kwargs: object) -> object: ...


class _BackendTerminationRequested(BaseException):
    pass


def require_literal_loopback_bind_host(host: str) -> str:
    """Return a canonical literal loopback bind host or fail closed.

    Hostnames (including ``localhost``), wildcard addresses, legacy numeric
    aliases, and IPv4-mapped IPv6 are intentionally rejected so bind authority
    never depends on DNS or platform-specific address parsing.
    """

    if not isinstance(host, str):
        raise ValueError("MemoLens backend host must be a literal loopback IP address.")
    normalized = host.strip()
    try:
        address = ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "MemoLens backend host must be a literal loopback IP address."
        ) from exc
    allowed = (
        isinstance(address, IPv4Address)
        and address.is_loopback
        or isinstance(address, IPv6Address)
        and address == IPv6Address("::1")
    )
    if not allowed:
        raise ValueError("MemoLens backend host must be a literal loopback IP address.")
    return str(address)


def run_managed_backend(
    app: _RunnableApp,
    *,
    host: str,
    port: int,
    debug: bool,
) -> None:
    """Run the writer process with one graceful runner-shutdown boundary."""

    host = require_literal_loopback_bind_host(host)
    previous_handlers: dict[signal.Signals, object] = {}

    def request_termination(
        _signum: int,
        _frame: FrameType | None,
    ) -> None:
        raise _BackendTerminationRequested()

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_termination)
    try:
        try:
            app.run(
                host=host,
                port=port,
                debug=debug,
                use_reloader=False,
            )
        except _BackendTerminationRequested:
            pass
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        shutdown_runtime_extensions(app.extensions)


__all__ = ["require_literal_loopback_bind_host", "run_managed_backend"]
