from __future__ import annotations

import json

import pytest

from wodata.live.replay import TELEMETRY_TOPICS
from wodata.script.fetch_replay_data import (
    SCRIPT_CONFIG,
    default_archive_output_root,
    run,
)


def test_script_builds_and_plays_from_automatic_archive_path(tmp_path):
    archive = (
        default_archive_output_root(tmp_path)
        / "schema_v1"
        / "year=2025"
        / "round=14"
        / "session=R"
    )
    raw = archive / "raw"
    raw.mkdir(parents=True)
    (archive / "manifest.json").write_text(
        json.dumps({"available_topics": ["SessionStatus"]}), encoding="utf-8"
    )
    (raw / "SessionStatus.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "topic": "SessionStatus",
                        "session_time_seconds": 0,
                        "source_order": 0,
                        "payload": {"Status": "Finished"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config = dict(SCRIPT_CONFIG)
    config.update(
        year=2025,
        event=None,
        round_number=14,
        session="R",
        data_root=tmp_path,
        download=False,
        build=True,
        play=True,
        speed=0,
    )

    result = run(config)

    assert result.archive_dir == archive
    assert result.built is True
    assert result.played is True
    assert result.messages_emitted == 1
    assert result.replay_path.is_file()
    summary = json.loads(
        result.replay_path.with_name("last_playback_summary.json").read_text()
    )
    assert summary["status"] == "completed"
    assert summary["topic_counts"] == {"SessionStatus": 1}


def test_script_requires_an_enabled_archive_stage(tmp_path):
    config = dict(SCRIPT_CONFIG)
    config.update(data_root=tmp_path, download=False, build=False, play=False)
    with pytest.raises(ValueError, match="At least one"):
        run(config)


def test_download_resolves_session_by_round_without_event(monkeypatch, tmp_path):
    config = dict(SCRIPT_CONFIG)
    config.update(
        year=2025,
        round_number=14,
        event=None,
        data_root=tmp_path,
        download=True,
        build=False,
        play=False,
    )
    calls = []

    def fake_resolve(year, event, session):
        calls.append((year, event, session))
        from wodata.live.replay import ArchiveSession

        return ArchiveSession(year, event, "Hungarian Grand Prix", "Race", "R", "/api/")

    class Result:
        archive_dir = tmp_path / "archive"
        available_topics = ()
        missing_topics = ()
        reused = False

    monkeypatch.setattr("wodata.script.fetch_replay_data.resolve_archive_session", fake_resolve)
    monkeypatch.setattr(
        "wodata.script.fetch_replay_data.download_archive", lambda *args, **kwargs: Result()
    )

    result = run(config)

    assert calls == [(2025, 14, "R")]
    assert result.downloaded is True


def test_fetch_defaults_include_required_fastf1_telemetry_topics():
    assert tuple(SCRIPT_CONFIG["mandatory_topics"]) == TELEMETRY_TOPICS
    assert set(TELEMETRY_TOPICS).issubset(SCRIPT_CONFIG["topics"])
    assert SCRIPT_CONFIG["update_existing"] is True
