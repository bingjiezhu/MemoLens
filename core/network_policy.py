"""Process-scoped outbound network policy for MemoLens production runtimes.

The offline profile is deliberately conservative: only literal loopback IP
targets and Unix-domain sockets are admitted.  Host names (including
``localhost``) are rejected before DNS so an offline run never relies on the
resolver or on a missing provider credential to remain private.

The observation snapshot contains counters only.  It never stores a host,
address, path, credential, request payload, or media-derived value.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


NETWORK_PROFILE_ENV = "MEMOLENS_NETWORK_PROFILE"
NETWORK_PROFILES = frozenset({"online", "offline"})


class NetworkPolicyError(RuntimeError):
    """Stable fail-closed error raised before a forbidden network action."""

    code = "network_offline_target_denied"


_LOCK = threading.Lock()
_COUNTERS = {
    "allowed_loopback": 0,
    "allowed_unix_socket": 0,
    "blocked_dns": 0,
    "blocked_connect": 0,
    "blocked_datagram": 0,
    "blocked_provider_request": 0,
}
_AUDIT_HOOK_INSTALLED = False
_PROXY_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)


def network_profile(environment: Mapping[str, str] | None = None) -> str:
    source = os.environ if environment is None else environment
    value = str(source.get(NETWORK_PROFILE_ENV, "online")).strip().lower()
    if value not in NETWORK_PROFILES:
        raise NetworkPolicyError(
            "MemoLens network profile is invalid; outbound networking is disabled."
        )
    return value


def offline_networking_enabled() -> bool:
    return network_profile() == "offline"


def configure_offline_process_network_environment() -> None:
    """Remove inherited proxy routes and pin loopback bypasses offline."""

    if not offline_networking_enabled():
        return
    for key in _PROXY_ENV_KEYS:
        os.environ.pop(key, None)
    no_proxy = "127.0.0.0/8,::1"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def _increment(counter: str) -> None:
    with _LOCK:
        _COUNTERS[counter] += 1


def reset_network_policy_observations() -> None:
    """Reset counters for an isolated verification run."""

    with _LOCK:
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def network_policy_observation() -> dict[str, object]:
    """Return a path-, host-, credential-, and payload-free observation."""

    with _LOCK:
        counters = dict(_COUNTERS)
    return {
        "object": "memolens.network_policy_observation",
        "schema_version": "1",
        "profile": network_profile(),
        "scope": {
            "process": "python",
            "instrumentation": [
                "provider-send-preflight",
                "python-audit-socket-connect",
                "python-audit-socket-getaddrinfo",
                "python-audit-socket-sendto",
            ],
            "os_packet_capture": False,
            "child_process_network_observed": False,
        },
        "counters": counters,
    }


def _literal_loopback(value: object) -> bool:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii", "strict")
        except UnicodeDecodeError:
            return False
    if not isinstance(value, str) or not value:
        return False
    candidate = value.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def network_target_enabled(url: str) -> bool:
    """Return whether a service may choose this target without recording a send.

    Service-level fallback selection uses this pure predicate so an offline
    production journey does not count a deliberately skipped provider as a
    network attempt.  The deeper preflight/audit guards remain the fail-safe.
    """

    if not offline_networking_enabled():
        return True
    configure_offline_process_network_environment()
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and _literal_loopback(parsed.hostname)
    )


def local_model_load_kwargs() -> dict[str, bool]:
    """Freeze transformer loads to the local cache in offline profile."""

    return {"local_files_only": True} if offline_networking_enabled() else {}


def require_offline_safe_host(
    host: object,
    *,
    denied_counter: str = "blocked_connect",
) -> None:
    """Admit a literal loopback host or fail before DNS/connect/send."""

    if not offline_networking_enabled():
        return
    configure_offline_process_network_environment()
    if _literal_loopback(host):
        _increment("allowed_loopback")
        return
    _increment(denied_counter)
    raise NetworkPolicyError(
        "Offline MemoLens denied a non-loopback network target before transmission."
    )


def require_offline_safe_url(url: str) -> None:
    """Admit a loopback HTTP(S) URL or fail before a provider request."""

    if not offline_networking_enabled():
        return
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        _increment("blocked_provider_request")
        raise NetworkPolicyError(
            "Offline MemoLens denied an invalid network target before transmission."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is None and parsed.netloc.endswith(":")
    ):
        _increment("blocked_provider_request")
        raise NetworkPolicyError(
            "Offline MemoLens denied an invalid network target before transmission."
        )
    require_offline_safe_host(
        parsed.hostname,
        denied_counter="blocked_provider_request",
    )


def _socket_target(args: tuple[Any, ...]) -> object | None:
    if len(args) < 2:
        return None
    address = args[1]
    if isinstance(address, tuple) and address:
        return address[0]
    if isinstance(address, (str, bytes)):
        # A string address on socket connect/sendto is a Unix-domain path.
        return address
    return None


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    if event not in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        return
    if not offline_networking_enabled():
        return
    if event == "socket.getaddrinfo":
        host = args[0] if args else None
        require_offline_safe_host(host, denied_counter="blocked_dns")
        return
    target = _socket_target(args)
    if isinstance(args[1] if len(args) > 1 else None, (str, bytes)):
        _increment("allowed_unix_socket")
        return
    require_offline_safe_host(
        target,
        denied_counter="blocked_datagram" if event == "socket.sendto" else "blocked_connect",
    )


def install_python_network_audit_hook() -> None:
    """Install the one-way CPython socket audit guard once per process."""

    global _AUDIT_HOOK_INSTALLED
    # Validate before installation so an unknown profile cannot silently fall
    # back to online operation.
    network_profile()
    configure_offline_process_network_environment()
    with _LOCK:
        if _AUDIT_HOOK_INSTALLED:
            return
        sys.addaudithook(_audit_hook)
        _AUDIT_HOOK_INSTALLED = True


__all__ = [
    "NETWORK_PROFILE_ENV",
    "NETWORK_PROFILES",
    "NetworkPolicyError",
    "configure_offline_process_network_environment",
    "install_python_network_audit_hook",
    "local_model_load_kwargs",
    "network_policy_observation",
    "network_profile",
    "network_target_enabled",
    "offline_networking_enabled",
    "require_offline_safe_host",
    "require_offline_safe_url",
    "reset_network_policy_observations",
]
