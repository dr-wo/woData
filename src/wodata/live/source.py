from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from .models import LiveConnectionStatus, LiveTimingMessage


class LiveTimingSource(Protocol):
    def run(
        self,
        emit_message: Callable[[LiveTimingMessage], None],
        emit_status: Callable[[LiveConnectionStatus], None],
    ) -> None: ...

    def stop(self) -> None: ...
