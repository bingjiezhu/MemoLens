from __future__ import annotations

import threading
import secrets
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

from core.config import Settings


_RUNTIME_GENERATION_PREFIX = "runtime_generation_"
_BOOTSTRAP_REQUEST_ID = re.compile(r"\Alb_[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_DATABASE_UUID = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_RUNTIME_GENERATION_ID = re.compile(r"\Aruntime_generation_[0-9a-f]{64}\Z")


def _mint_runtime_generation_id() -> str:
    """Mint an opaque identity that callers cannot inject into a bundle."""

    return f"{_RUNTIME_GENERATION_PREFIX}{secrets.token_hex(32)}"


@dataclass(frozen=True)
class RuntimeHealthIdentity:
    """Path-free identity frozen into the signed health-v2 projection."""

    mode: str
    bootstrap_request_id: str | None
    candidate_binding_sha256: str | None
    database_uuid: str | None
    schema_version: int | None
    runtime_generation_id: str | None

    @classmethod
    def bootstrap_candidate(
        cls,
        *,
        request_id: str,
        binding_sha256: str,
    ) -> RuntimeHealthIdentity:
        if (
            _BOOTSTRAP_REQUEST_ID.fullmatch(request_id) is None
            or _SHA256.fullmatch(binding_sha256) is None
        ):
            raise ValueError("invalid bootstrap candidate health identity")
        return cls(
            mode="bootstrap_candidate",
            bootstrap_request_id=request_id,
            candidate_binding_sha256=binding_sha256,
            database_uuid=None,
            schema_version=None,
            runtime_generation_id=None,
        )

    @classmethod
    def active(
        cls,
        *,
        database_uuid: str,
        schema_version: int,
        runtime_generation_id: str,
        bootstrap_request_id: str | None = None,
        candidate_binding_sha256: str | None = None,
    ) -> RuntimeHealthIdentity:
        if (
            _DATABASE_UUID.fullmatch(database_uuid) is None
            or type(schema_version) is not int
            or schema_version < 1
            or _RUNTIME_GENERATION_ID.fullmatch(runtime_generation_id) is None
            or (bootstrap_request_id is None) != (candidate_binding_sha256 is None)
            or (
                bootstrap_request_id is not None
                and _BOOTSTRAP_REQUEST_ID.fullmatch(bootstrap_request_id) is None
            )
            or (
                candidate_binding_sha256 is not None
                and _SHA256.fullmatch(candidate_binding_sha256) is None
            )
        ):
            raise ValueError("invalid active runtime health identity")
        return cls(
            mode="active",
            bootstrap_request_id=bootstrap_request_id,
            candidate_binding_sha256=candidate_binding_sha256,
            database_uuid=database_uuid,
            schema_version=schema_version,
            runtime_generation_id=runtime_generation_id,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "bootstrap_request_id": self.bootstrap_request_id,
            "candidate_binding_sha256": self.candidate_binding_sha256,
            "database_uuid": self.database_uuid,
            "schema_version": self.schema_version,
            "runtime_generation_id": self.runtime_generation_id,
        }


@dataclass(frozen=True)
class RuntimeBundle:
    """One internally consistent MemoLens runtime generation."""

    settings: Settings
    extensions: Mapping[str, object]
    generation_id: str = field(
        init=False,
        default_factory=_mint_runtime_generation_id,
    )

    @classmethod
    def freeze(
        cls,
        settings: Settings,
        extensions: Mapping[str, object],
    ) -> RuntimeBundle:
        return cls(
            settings=settings,
            extensions=MappingProxyType(dict(extensions)),
        )

    def extension(self, name: str) -> object:
        return self.extensions[name]


@dataclass
class _RuntimeState:
    bundle: RuntimeBundle
    references: int = 0
    retired: bool = False
    shutdown_started: bool = False


class RuntimeLease:
    """An idempotently releasable reference to one runtime generation."""

    def __init__(self, manager: RuntimeManager, state: _RuntimeState):
        self._manager = manager
        self._state: _RuntimeState | None = state

    @property
    def bundle(self) -> RuntimeBundle:
        state = self._state
        if state is None:
            raise RuntimeError("Runtime lease has already been released.")
        return state.bundle

    @property
    def generation_id(self) -> str:
        """Return the exact generation pinned by this lease."""

        return self.bundle.generation_id

    def release(self) -> None:
        state = self._state
        if state is None:
            return
        self._state = None
        self._manager._release(state)

    def __enter__(self) -> RuntimeBundle:
        return self.bundle

    def __exit__(self, *_: object) -> None:
        self.release()


class RuntimeManager:
    """Atomically swap runtimes while deferring retirement until leases drain."""

    def __init__(self, retire: Callable[[RuntimeBundle], None]):
        self._retire = retire
        self._lock = threading.Lock()
        self._current: _RuntimeState | None = None

    def acquire(self) -> RuntimeLease:
        with self._lock:
            state = self._current
            if state is None:
                raise RuntimeError("MemoLens runtime is not configured.")
            state.references += 1
        return RuntimeLease(self, state)

    @property
    def current_bundle(self) -> RuntimeBundle:
        """Compatibility/introspection snapshot; requests must use ``acquire``."""

        with self._lock:
            state = self._current
            if state is None:
                raise RuntimeError("MemoLens runtime is not configured.")
            return state.bundle

    @property
    def current_bundle_or_none(self) -> RuntimeBundle | None:
        """Return a non-leased snapshot for health/introspection only."""

        with self._lock:
            state = self._current
            return state.bundle if state is not None else None

    @property
    def current_generation_id(self) -> str:
        """Return the generation currently offered to new leases."""

        return self.current_bundle.generation_id

    def swap(self, bundle: RuntimeBundle) -> RuntimeBundle | None:
        retired: _RuntimeState | None = None
        with self._lock:
            previous = self._current
            # Re-adopting the same immutable bundle must not create a retired
            # state that can later shut down extensions still serving as the
            # current generation.
            if previous is not None and previous.bundle is bundle:
                return previous.bundle
            self._current = _RuntimeState(bundle=bundle)
            if previous is not None:
                previous.retired = True
                if previous.references == 0 and not previous.shutdown_started:
                    previous.shutdown_started = True
                    retired = previous
        if retired is not None:
            self._retire(retired.bundle)
        return previous.bundle if previous is not None else None

    def _release(self, state: _RuntimeState) -> None:
        retired: RuntimeBundle | None = None
        with self._lock:
            if state.references <= 0:
                raise RuntimeError("Runtime lease reference count underflow.")
            state.references -= 1
            if state.retired and state.references == 0 and not state.shutdown_started:
                state.shutdown_started = True
                retired = state.bundle
        if retired is not None:
            self._retire(retired)
