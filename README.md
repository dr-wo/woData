# woData

Shared data-root and cache-path helpers for the dr-wo Formula 1 analysis tools.

## Team and driver colours

`wodata.colours` is the shared source of truth for team and driver plotting
colours across the dr-wo packages. The current values come from the 2026
FastF1 live-timing `DriverList` `TeamColour` field. FastF1 assigns each driver
their team's colour, so the driver map is derived from the same team palette.

```python
from wodata.colours import get_driver_colour, get_team_colour

assert get_team_colour("McLaren") == "#F47600"
assert get_driver_colour("NOR") == "#F47600"
```

Both British (`TEAM_COLOURS`, `DRIVER_COLOURS`) and American
(`TEAM_COLORS`, `DRIVER_COLORS`) spellings are exported. `F1_TEAM_COLORS`
also includes legacy aliases used by historic data and existing plots.

When a new season changes teams, drivers, or colours, update
`src/wodata/colours.py` from a complete FastF1 `DriverList` and update
`tests/test_colours.py`. Consumer packages should import the shared mappings
instead of defining local palettes.

## Live timing

The optional `wodata.live` package provides authenticated FastF1 live ingestion,
mandatory raw recording, deterministic replay, completed-lap state reduction, and
sector-level timing when the live feed includes `TimingData.Lines[*].Sectors`.

```bash
pip install -e ../Fast-F1
pip install -e ".[live]"
```

Recordings use the shared data root under
`wodata/live_timing/schema_v1/year=.../round=.../session=...`.

`LiveTimingController.start_live()` records every canonical raw message to
`live_timing_raw.txt` before reduction. The same file can be passed to
`ReplayLiveTimingSource` for deterministic debugging. If a recording starts late
or the feed omits a lap/sector time, the reducer keeps the data it has and marks
later cumulative timing unknown after internal gaps instead of fabricating a
continuous race clock.

### Archived session replay

Archive replay is a separate message source and deliberately uses the same
`LiveTimingMessage` contract, controller queue, and `LiveSessionStore` reducers as
SignalR. It does not load final FastF1 lap tables and has no replay-only analysis
path. Existing raw `.txt` replay files remain supported.

```bash
# Download selected per-topic streams and write a manifest.
python -m wodata.live.replay.cli download \
  --year 2025 --event Hungary --session R \
  --output-root ~/.wodata/live_archive

# Build a versioned, deterministic JSONL replay atomically.
python -m wodata.live.replay.cli build \
  --archive ~/.wodata/live_archive/schema_v1/year=2025/round=14/session=R

# Replay through the shared reducer at 20x.
python -m wodata.live.replay.cli play \
  ~/.wodata/live_archive/schema_v1/year=2025/round=14/session=R/replay/merged_replay_v1.jsonl \
  --speed 20

# Deterministic robustness run with missing and delayed data.
python -m wodata.live.replay.cli play merged_replay_v1.jsonl --speed 0 \
  --exclude-topic TimingAppData --delay-topic TrackStatus:15 \
  --drop-rate 0.02 --random-seed 42
```

For equal timestamps the builder orders `SessionInfo`, `DriverList`,
`SessionStatus`, `TrackStatus`, `LapCount`, `TimingAppData`, `TimingData`, race
control, weather, then heartbeat, preserving original order within each topic.
This is stable and reproducible, but it approximates cross-topic ordering because
the archive cannot recover the original SignalR network arrival order.

Programmatic playback supports `speed=0`, start/end session-time bounds,
pause/resume/stop, topic exclusion, drop-after, delay, and seeded random
drop/duplication. Faults are applied in memory; the source replay is never changed.

For a dictionary-configured workflow like the `woStrategy` scripts, edit
`SCRIPT_CONFIG` in `src/wodata/script/fetch_replay_data.py` and run:

```bash
PYTHONPATH=src ../.venv/bin/python -m wodata.script.fetch_replay_data
```

Set `workflow` to `"fetch_replay_data"` and use the `download`, `build`, and `play`
switches in any combination. Normally only `year`, `round_number`, and `session`
identify the session; `event` can remain `None` and is only a fallback when the
round is unknown. Set `workflow` to `"live_record"` to record SignalR timing
until the session ends or Ctrl-C is pressed. With `data_root=None`, all outputs are
placed under `WODATA_ROOT`, the legacy shared-root environment setting, or the
`woData` project root. Archives go below
`wodata/live_archive/` and live recordings below `wodata/live_timing/`.

The fetch workflow includes FastF1's `CarData.z` and `Position.z` archive streams
and can add them to an existing archive without downloading topics already present.
Their decoded archive records are stored below the selected session archive as
`raw/CarData.z.json` and `raw/Position.z.json`; the deterministic merged JSONL
replay contains the same canonical messages. During reduction, `CarData.z` becomes
timestamped speed samples with inferred lap attribution and is retained in a
rolling 12-lap buffer per driver. `Position.z` becomes a separate XYZ diagnostic
buffer. The shared traffic calculation consumes the speed buffer and does not wait
for, merge, or repeatedly rebuild laps from the larger position stream.

This separation is deliberate. `CarData.z` has the speed channel needed by the
shared progress integrator, whereas raw XYZ positions require track projection and
orientation calibration before they represent along-track distance. Keeping
Position.z diagnostic avoids making replay speed and traffic availability depend
on that unresolved calibration. Because `CarData.z` has no strategy lap number,
the reducer assigns samples using the latest completed timing state and assumes
messages are processed chronologically; delayed timing near a finish line can move
a small number of samples to an adjacent lap. The bounded 12-lap buffer controls
live memory and recalculation cost, at the cost of not retaining full-race
telemetry in a live snapshot. Raw recordings and archive files remain complete.
The old `archive_replay` workflow and module names remain compatibility aliases.
Use `fetch_replay_data` for completed sessions. `live_record` only receives data while
Formula 1 is actively broadcasting the configured session and requires F1TV
authentication. The recorder opens its destination lazily on the first message and
selects an unused filename rather than replacing a non-empty capture.

## Strategy Prediction analysis contracts

`wodata.contracts` defines the schema-v1 weekend-model, strategy-search, completed-lap,
and tyre-state contracts shared by woStrategy and woPlanner. `wodata.artifacts` writes
canonical model and strategy CSV/JSON artifacts below `WODATA_ROOT/wostrategy/`, with
manifests, deterministic hashes, provenance, and append-only run history.

FastF1 data used by Strategy Prediction is also rooted in woData: raw FastF1 HTTP/parser cache
under `fastf1/raw_cache/schema_v1`, derived telemetry under `fastf1/telemetry/schema_v1`,
and enriched session laps under `fastf1/session_laps/schema_v1`.

## woStrategy cache layout

woData is a runtime dependency of woStrategy and owns its shared cache paths.
The default layout is:

```text
woData/
  fastf1/
    raw_cache/schema_v1/
    telemetry/schema_v1/
    session_laps/schema_v1/year=YYYY/round=N/session=FP1/laps.pkl
  wostrategy/
    race_performance_review/schema_v1/
      year=YYYY/round=N/session=R/
        tyre_information.csv
        race_performance_YYYY_N_R_*.csv
      year=YYYY/range=1-11/session=R/
        tyre_information.csv
        race_performance_*.csv
```

`tyre_information.csv` is the compact, canonical consumer view of tyre baseline
speed, compound delta, and degradation. It includes global and team scopes.
Detailed Monte Carlo samples, clean laps, diagnostics, and metadata deliberately
remain as separate files in the same partition because they can be much larger.

`WODATA_ROOT` may point to a different shared data root. `WOSTRATEGY_DATA_ROOT`
is still accepted as a legacy environment override. With sibling checkouts and
no override, running from woStrategy or woPlanner resolves the adjacent `woData`
directory automatically.
