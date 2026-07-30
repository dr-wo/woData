from __future__ import annotations

from pathlib import Path
from threading import Lock

from wodata import get_cache_path

from .models import LiveTimingMessage


def live_recording_path(
    *,
    year: int,
    round_number: int,
    session: str,
    data_root: Path | None = None,
) -> Path:
    return get_cache_path(
        namespace="wodata",
        dataset="live_timing",
        schema="schema_v1",
        year=year,
        round_number=round_number,
        session=session,
        filename="live_timing_raw.txt",
        data_root=data_root,
    )


class LiveTimingRecorder:
    def __init__(self, path: str | Path, filemode: str = "w") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open(filemode, encoding="utf-8")
        self._lock = Lock()

    def write(self, message: LiveTimingMessage) -> None:
        timestamp = (
            message.source_timestamp_utc.isoformat().replace("+00:00", "Z")
            if message.source_timestamp_utc is not None
            else ""
        )
        with self._lock:
            self._file.write(str([message.topic, message.payload, timestamp]) + "\n")
            self._file.flush()

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.flush()
                self._file.close()
