from __future__ import annotations

import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from core.config import Settings


@dataclass(frozen=True)
class RuntimeBundle:
    """One internally consistent MemoLens runtime generation."""

    settings: Settings
    extensions: Mapping[str, object]

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

    def swap(self, bundle: RuntimeBundle) -> RuntimeBundle | None:
        retired: _RuntimeState | None = None
        with self._lock:
            previous = self._current
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
