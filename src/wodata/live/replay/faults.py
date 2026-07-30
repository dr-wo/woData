from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from .schema import ReplayEvent


@dataclass(frozen=True)
class ReplayFaultConfig:
    excluded_topics: frozenset[str] = frozenset()
    topic_drop_after: dict[str, float] = field(default_factory=dict)
    topic_delay_seconds: dict[str, float] = field(default_factory=dict)
    duplicate_rate: float = 0.0
    drop_rate: float = 0.0
    random_seed: int = 42

    def __post_init__(self) -> None:
        for name, value in (("duplicate_rate", self.duplicate_rate), ("drop_rate", self.drop_rate)):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if any(value < 0 for value in self.topic_delay_seconds.values()):
            raise ValueError("topic delays cannot be negative")


@dataclass(frozen=True)
class FaultStats:
    dropped: int = 0
    delayed: int = 0
    duplicated: int = 0


def apply_faults(
    events: list[ReplayEvent], config: ReplayFaultConfig
) -> tuple[list[ReplayEvent], FaultStats]:
    """Apply deterministic playback-only faults without modifying the replay file."""
    rng = random.Random(config.random_seed)
    transformed: list[tuple[float, int, int, ReplayEvent]] = []
    dropped = delayed = duplicated = 0
    for ordinal, event in enumerate(events):
        drop_after = config.topic_drop_after.get(event.topic)
        excluded = event.topic in config.excluded_topics
        expired = drop_after is not None and event.session_time_seconds >= drop_after
        random_drop = rng.random() < config.drop_rate
        if excluded or expired or random_drop:
            dropped += 1
            continue
        delay = float(config.topic_delay_seconds.get(event.topic, 0.0))
        shifted = replace(event, session_time_seconds=event.session_time_seconds + delay)
        if delay:
            delayed += 1
        transformed.append((shifted.session_time_seconds, ordinal, 0, shifted))
        if rng.random() < config.duplicate_rate:
            transformed.append((shifted.session_time_seconds, ordinal, 1, shifted))
            duplicated += 1
    transformed.sort(key=lambda item: item[:3])
    output = [replace(item[3], sequence=index) for index, item in enumerate(transformed, 1)]
    return output, FaultStats(dropped, delayed, duplicated)
