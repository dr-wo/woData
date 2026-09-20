# Canonical pace and tyre semantics

This is the shared semantic contract for current `woData`, `woStrategy`,
`woPlanner`, and `woWeekend` artifacts. It describes existing calculations; it
does not prescribe a common additive model or move numerical ownership.

## Pace snapshots

`ModelSnapshot.semantics` is artifact-level metadata shared by every row and
sample in that snapshot. It is recorded once at the snapshot/run boundary, not
inside the Monte Carlo sample loop. New snapshots describe the observation,
filtering role, actual numerical transformation, and each quantity's unit,
independent variable, sign, frame, and reference.

Observed/source data and historical artifacts are not destructively rewritten
to add this metadata. New consumers preserve known legacy semantics when the
new provenance fields are absent.

The Race fuel coordinate is seconds per *fuel-proxy remaining lap*. Its stored
legacy unit is `s/lap`; it is not a measured `s/kg` sensitivity. Race fuel and
track coordinates are jointly sampled and can be exactly aliased after baseline
profiling. Marginal quantiles remain available but are not independent physical
measurements. Joint estimation of an effect is distinct from removing that
effect from an output, and does not by itself prohibit a future consumer from
calculating the effect. Race uses `FuelProxyLapsRemaining - FuelRef` and
`LapNumber - RaceLapRef`; a positive coefficient makes the corresponding larger
coordinate slower. FP retains its distinct run-lap and session track indices;
the contract does not force Race and FP into one physical coordinate system.

The weight-projection utility retains its existing first-order conversion:
`fuel_proxy_rate / assumed_kg_per_proxy_lap`. Its result is an assumption-
dependent equivalent `s/kg`, not an independently calibrated physical fuel
sensitivity, and the conversion does not remove fuel/track confounding. The
legacy `FuelSecondsPerKg` field is an alias only; new output declares the method,
calibration status, and confounding status alongside the value.

FP snapshots use a different coordinate system: one-based lap index within each
run and a session track index (provided directly, derived as a dense rank of
parsed session time, or relative to the minimum eligible lap). A nuisance
intercept is re-fitted for every run and candidate and is not published as an
absolute baseline. Joint FP publishes the shared fuel marginal and session-local
track, degradation, and compound-delta marginals; the single-session model
publishes the corresponding marginals for that session only.

## Tyre performance reference

Full external SOFT/MEDIUM/HARD performance inputs may use any common additive
gauge. Selecting a source reference does not alter numbers. The selection order
is:

1. use an explicitly declared reference;
2. otherwise use a unique zero;
3. for multiple zeros, prefer MEDIUM, then HARD, then SOFT;
4. with no zero, use MEDIUM as source metadata.

Partial manual overrides inherit that selected source gauge. Specified fields
are merged first, then the operational boundary subtracts the merged MEDIUM
value exactly once. It does not normalize early or re-infer the reference after
the merge. The merged value is serialized as a non-formal intermediate, so its
declared reference is not required to be zero before the operational boundary.
This common shift preserves every pairwise relative tyre delta.

New formal `relative_to_compound` outputs must contain the declared reference
at zero within `1e-9` seconds. Common-gauge manual inputs, degradation values,
and legacy artifacts with unspecified frame are not subject to that relative
output check. Operational tyre outputs declare MEDIUM as zero. Missing required
numeric/event fields still fail existing preflight; missing only new semantic or
provenance fields remains legacy-compatible.

## Uncertainty and provenance

Uncertainty metadata distinguishes joint sample banks, marginal summaries, and
fixed reference coordinates; it does not synthesize uncertainty or change the
existing numerical interval calculation. Historical
producer identity is preserved only when the artifact contains it. A new
`woWeekend` run records hashes of its immutable input/resolved config and event
metadata, plus the HEAD and dirty state of the consumer worktrees at run
finalization. Consumer identity is never presented as an unknown legacy
artifact's producer identity, and replay remains explicitly non-`as-run` with
its evidence cutoff and `algorithm_only`/`operational` classification.

Replay provenance keeps full artifact-content hashes separate from the
`effective_replay_configuration_and_control_inputs_v1` fingerprint. The latter
covers configuration/control values captured at the calculation boundary:
numeric event context, replay mode, resolved model objects and analysis options,
effective tyre coordinates, and behavior-affecting manual events. Descriptive
source labels, locator paths, timestamps, and descriptions remain separate
provenance and do not enter that fingerprint. It does not identify the
observation stream, sample bank, or complete historical as-run state.

## Deferred scope

This contract does not provide physical fuel/track decoupling, paired-contrast
or paired-uncertainty numerical correction, complete historical as-run replay,
or the future FP Fuel / Team Tyre model work. Those remain separate follow-up
work and are not implied by the metadata recorded here.
