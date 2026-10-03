# R15 first-lap timing fixture

`r15_first_laps.jsonl` is a compact projection of
`wodata/live_timing/schema_v1/year=2026/round=15/session=R/live_timing_raw.jsonl`.
It retains original recording sequence numbers, source/receive timestamps and
timing values. No times or lap identities are synthesized.

The selected messages cover RUS (63), PIA (81), LEC (16), NOR (1), HAD (6) and
VER (3): driver metadata, Started (1163), lap-1 S2/S3 and counter, lap-2 S1
with explicit S2/S3 resets, metadata-only PreviousValue patches, lap-2 S2,
and the first RUS/PIA/LEC commits (4213/4246/4269). Unrelated drivers and
speed/telemetry fields are omitted. The original sector patch objects are
retained, including empty Value fields and fastest flags.

Source-backed ordering uses fresh gap/interval messages:

| Cut | Ahead (gap) | Behind (gap) | Gap source sequences |
| --- | --- | --- | --- |
| 3314 | LEC (+2.299) | NOR (+2.959) | 3299 / 3314 |
| 3334 | HAD (+3.401) | VER (+4.319) | 3322 / 3334 |

These demonstrate why standalone S1 durations must not be interpreted as
race elapsed time. The fixture does not contain lap-1 S1 or LastLapTime,
which were also absent from the source for these drivers.
