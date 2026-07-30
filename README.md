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
