import json
import pytest
from wodata.interruption_history import (load_history, history_path, same_venue,
                                        unavailable_history, write_history)


def test_identity_policy_does_not_match_event_names():
    assert same_venue({'country': ' UK ', 'location': 'Silverstone'}, {'country': 'uk', 'location': 'silverstone'})
    assert not same_venue({'event_name': 'British GP'}, {'event_name': 'British GP'})
    assert not same_venue({'circuit_id': '1', 'country': 'UK', 'location': 'Silverstone'},
                          {'circuit_id': '2', 'country': 'UK', 'location': 'Silverstone'})


def test_missing_legacy_and_invalid_contract_degrade_safely(tmp_path):
    assert load_history(2026, 15, tmp_path)['status'] == 'unavailable'
    path = history_path(2026, 15, tmp_path); path.parent.mkdir(parents=True)
    for value in [{'schema_version': 0}, {'races': []}, None, 'legacy']:
        path.write_text(json.dumps(value))
        assert load_history(2026, 15, tmp_path)['status'] == 'unavailable'
    path.write_text('{broken')
    assert load_history(2026, 15, tmp_path)['status'] == 'unavailable'


def test_atomic_round_trip_and_target_mismatch(tmp_path):
    value = unavailable_history(2026, 15, 'not prepared')
    write_history(value, tmp_path)
    assert load_history(2026, 15, tmp_path) == value
    history_path(2026, 15, tmp_path).write_text(json.dumps(unavailable_history(2026, 14, 'wrong target')))
    assert load_history(2026, 15, tmp_path)['status'] == 'unavailable'


def test_source_adapter_preserves_original_objects_and_avoids_telemetry(tmp_path, monkeypatch):
    import fastf1
    import pandas as pd
    from types import SimpleNamespace
    from wodata.interruption_history import load_race_source
    track = pd.DataFrame({'Time': [pd.Timedelta(seconds=1)], 'Status': ['4']})
    timing_stream = pd.DataFrame([(0, '1', 1, 'LAP 1'), (15, '1', 1, 'LAP 2')],
                                 columns=['Time', 'Driver', 'Position', 'GapToLeader'])
    calls = []
    timing_calls = []
    session = SimpleNamespace(track_status=track, session_status=track,
        laps=pd.DataFrame(), session_start_time=pd.Timedelta(seconds=1),
        race_control_messages=pd.DataFrame(), t0_date=pd.Timestamp('2025-01-01'),
        api_path='/fixture/Race/', load=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(fastf1.Cache, 'enable_cache', lambda path: None)
    monkeypatch.setattr(fastf1, 'get_session', lambda *args: session)
    from fastf1 import _api
    def timing_data(path):
        timing_calls.append(path)
        return pd.DataFrame(), timing_stream
    monkeypatch.setattr(_api, 'timing_data', timing_data)
    source = load_race_source(2025, 1, data_root=tmp_path)
    assert source['track_status'] is track
    assert source['session_status'] is track
    assert source['timing_stream'] is timing_stream
    assert timing_calls == ['/fixture/Race/']
    assert source['track_status_integrity']['status'] == 'unknown'
    assert source['track_status_integrity']['evidence'] is None
    assert calls == [{'laps': True, 'telemetry': False, 'weather': False, 'messages': True}]


@pytest.mark.parametrize('patch', [
    {'safety_car': None}, {'coverage': None}, {'red_flag': {'count': 0}},
])
def test_malformed_nested_records_degrade_without_consumer_crashes(tmp_path, patch):
    race = {'season': 2025, 'round_number': 1, 'event_id': '2025-01',
        'event_identity': {'country': 'UK', 'location': 'Silverstone'},
        'coverage': {'status': 'complete', 'reasons': []},
        'safety_car': {'occurred': False, 'count': 0, 'observed_count': 0, 'occurrences': []},
        'red_flag': {'occurred': False, 'count': 0, 'observed_count': 0, 'occurrences': []}}
    race.update(patch)
    payload = unavailable_history(2026, 1, '')
    payload['target']['event_identity'] = {'country': 'UK', 'location': 'Silverstone'}
    payload['races'] = [race]
    path = history_path(2026, 1, tmp_path); path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))
    assert load_history(2026, 1, tmp_path)['races'] == []


def test_missing_laps_in_source_adapter_preserves_known_transitions(tmp_path):
    from types import SimpleNamespace
    import pandas as pd
    from wodata.interruption_history import load_race_source
    track = pd.DataFrame({'Time': [pd.Timedelta(seconds=1)], 'Status': ['4']})
    source = load_race_source(2025, 1, data_root=tmp_path,
        session_loader=lambda *args: SimpleNamespace(track_status=track, session_status=track,
            session_start_time=pd.Timedelta(seconds=1)))
    assert source['track_status'] is track
    assert source['laps'] is None
    assert source['source_availability_notes']


def canonical(*, complete=False):
    from copy import deepcopy
    lap = {'lap': 1, 'status': 'approximate', 'method': 'raw_leader_lap_boundaries'}
    fragment = {'source_index': 1, 'first_observed_seconds': 20, 'start_seconds': 20,
        'start_lap': lap, 'start_status': 'observed_transition', 'end_seconds': 30,
        'end_lap': deepcopy(lap), 'end_status': 'observed_transition', 'end_track_status': '1'}
    occurrence = {**deepcopy(fragment), 'sequence': 1, 'track_status': '4',
        'boundary_ambiguous': False, 'fragments': [fragment],
        'exit_proof': {'track_status': '1', 'observed_seconds': 30, 'source_index': 2}}
    identity = {'country': 'UK', 'location': 'Silverstone'}
    race = {'season': 2025, 'round_number': 1, 'event_id': '2025-01', 'event_identity': identity,
        'provenance': {'source': 'fixture_original_track_status', 'timeline_sha256': '0' * 64,
            'lap_source': 'raw timing', 'layout_policy': 'retained venue', 'source_availability_notes': []},
        'coverage': {'status': 'complete' if complete else 'incomplete',
            'reasons': [] if complete else ['integrity unknown'],
            'official_start_seconds': 10, 'race_end_seconds': 100,
            'track_status_integrity': {'status': 'verified_complete' if complete else 'unknown',
                'evidence': 'synthetic fixture with complete inventory' if complete else None},
            'start_admission': {'status': 'established', 'evidence': 'non_generated_race_lap_completion',
                'corroborated_at_seconds': 40, 'reason': 'initial race corroborated'}},
        'safety_car': {'count': 1 if complete else None, 'occurred': True, 'observed_count': 1,
            'occurrences': [occurrence]},
        'red_flag': {'count': 0 if complete else None, 'occurred': False if complete else None,
            'observed_count': 0, 'occurrences': []}}
    return {'schema_version': 1, 'semantic_role': 'historical_race_interruptions', 'status': 'available',
        'target': {'season': 2026, 'round_number': 1, 'event_identity': identity}, 'races': [race]}


@pytest.mark.parametrize('complete', [True, False])
def test_valid_complete_and_partial_canonical_contracts_round_trip(tmp_path, complete):
    value = canonical(complete=complete)
    write_history(value, tmp_path)
    assert load_history(2026, 1, tmp_path) == value


@pytest.mark.parametrize('field,value', [
    ('start_status', 'made_up'), ('end_status', 'made_up'), ('end_track_status', '3'),
    ('end_track_status', '4'), ('start_seconds', None), ('end_seconds', None),
    ('end_seconds', 19), ('first_observed_seconds', 15),
    ('start_lap', {'lap': 1, 'status': 'made_up', 'method': 'raw_leader_lap_boundaries'}),
    ('end_lap', {'lap': 2, 'status': 'unavailable', 'method': 'raw_leader_lap_boundaries'}),
])
def test_rejects_unsupported_and_contradictory_deployment_fields(field, value):
    from wodata.interruption_history import validate_history
    payload = canonical()
    event = payload['races'][0]['safety_car']['occurrences'][0]
    event[field] = value
    event['fragments'][0][field] = value
    with pytest.raises(ValueError):
        validate_history(payload, 2026, 1)


@pytest.mark.parametrize('field', ['source', 'timeline_sha256', 'lap_source', 'layout_policy', 'source_availability_notes'])
def test_rejects_missing_required_source_provenance(field):
    from wodata.interruption_history import validate_history
    payload = canonical()
    del payload['races'][0]['provenance'][field]
    with pytest.raises(ValueError):
        validate_history(payload, 2026, 1)


def test_complete_coverage_requires_integrity_evidence_and_deployment_boundaries():
    from wodata.interruption_history import validate_history
    for mutation in ['integrity', 'start', 'end']:
        payload = canonical(complete=True)
        race = payload['races'][0]
        event = race['safety_car']['occurrences'][0]
        if mutation == 'integrity':
            race['coverage']['track_status_integrity'] = {'status': 'unknown', 'evidence': None}
        else:
            for obj in (event, event['fragments'][0]):
                obj[mutation + '_seconds'] = None
                obj[mutation + '_lap'] = {'lap': None, 'status': 'unavailable', 'method': 'raw_leader_lap_boundaries'}
                obj[mutation + '_status'] = 'start_transition_unavailable' if mutation == 'start' else 'unavailable'
                if mutation == 'end':
                    obj['end_track_status'] = None
            event['boundary_ambiguous'] = mutation == 'start'
            if mutation == 'end':
                event['exit_proof'] = None
        with pytest.raises(ValueError):
            validate_history(payload, 2026, 1)


@pytest.mark.parametrize('patch', [
    {'count': 0}, {'observed_count': 0}, {'occurred': False}, {'count': 1},
])
def test_rejects_count_state_contradictions(patch):
    from wodata.interruption_history import validate_history
    payload = canonical()
    payload['races'][0]['safety_car'].update(patch)
    with pytest.raises(ValueError):
        validate_history(payload, 2026, 1)


def test_distinct_minimum_groups_require_a_trusted_exit_and_chronological_order():
    from copy import deepcopy
    from wodata.interruption_history import validate_history
    for mutation in ['no_exit', 'backwards']:
        payload = canonical()
        family = payload['races'][0]['safety_car']
        first = family['occurrences'][0]
        second = deepcopy(first)
        second.update(sequence=2, source_index=3, first_observed_seconds=50, start_seconds=50, end_seconds=60)
        second['fragments'][0].update(source_index=3, first_observed_seconds=50, start_seconds=50, end_seconds=60)
        second['exit_proof'].update(observed_seconds=60, source_index=4)
        family['occurrences'].append(second); family['observed_count'] = 2
        if mutation == 'no_exit':
            first['exit_proof'] = None
            for obj in (first, first['fragments'][0]):
                obj.update(end_seconds=None, end_status='ambiguous_transition', end_track_status=None,
                    end_lap={'lap': None, 'status': 'unavailable', 'method': 'raw_leader_lap_boundaries'})
            first['boundary_ambiguous'] = True
        else:
            for obj in (second, second['fragments'][0]):
                obj.update(first_observed_seconds=15, start_seconds=15)
        with pytest.raises(ValueError):
            validate_history(payload, 2026, 1)


def test_cross_family_timestamp_order_and_overlapping_known_intervals_are_rejected():
    from copy import deepcopy
    from wodata.interruption_history import validate_history
    for mutation in ['backwards', 'overlap']:
        payload = canonical()
        race = payload['races'][0]
        sc = race['safety_car']['occurrences'][0]
        rf = deepcopy(sc)
        rf.update(sequence=2, track_status='5', source_index=3, first_observed_seconds=35, start_seconds=35, end_seconds=40)
        rf['fragments'][0].update(source_index=3, first_observed_seconds=35, start_seconds=35, end_seconds=40)
        rf['exit_proof'].update(observed_seconds=40, source_index=4)
        race['red_flag'].update(occurred=True, observed_count=1, occurrences=[rf])
        if mutation == 'backwards':
            for obj in (rf, rf['fragments'][0]):
                obj.update(first_observed_seconds=15, start_seconds=15)
        else:
            for obj in (sc, sc['fragments'][0]):
                obj['end_seconds'] = 80
            sc['exit_proof']['observed_seconds'] = 80
        with pytest.raises(ValueError):
            validate_history(payload, 2026, 1)


def test_legitimate_unknown_fragments_preserve_one_minimum_and_round_trip(tmp_path):
    from copy import deepcopy
    payload = canonical()
    event = payload['races'][0]['safety_car']['occurrences'][0]
    fragment = event['fragments'][0]
    fragment.update(end_seconds=None, end_track_status=None, end_status='ambiguous_transition',
        uncertain_after_seconds=30, end_lap={'lap': None, 'status': 'unavailable', 'method': 'raw_leader_lap_boundaries'})
    second = deepcopy(fragment)
    second.update(source_index=3, first_observed_seconds=50, start_seconds=None,
        start_status='start_transition_unavailable', end_seconds=60, end_status='observed_transition',
        end_track_status='1', start_lap={'lap': None, 'status': 'unavailable', 'method': 'raw_leader_lap_boundaries'},
        end_lap={'lap': 2, 'status': 'approximate', 'method': 'raw_leader_lap_boundaries'})
    del second['uncertain_after_seconds']
    event['fragments'].append(second)
    event.update(boundary_ambiguous=True, end_seconds=60, end_status='observed_transition',
        end_track_status='1', end_lap=second['end_lap'])
    event['exit_proof'].update(observed_seconds=60, source_index=4)
    write_history(payload, tmp_path)
    assert load_history(2026, 1, tmp_path) == payload


@pytest.mark.parametrize('mutation', ['admission_after_finish', 'exit_after_finish', 'exit_before_unknown', 'negative_exit_index', 'inverted_zero_laps'])
def test_rejects_contradictory_admission_exit_and_lap_evidence(mutation):
    from wodata.interruption_history import validate_history
    payload = canonical()
    race = payload['races'][0]
    event = race['safety_car']['occurrences'][0]
    if mutation == 'admission_after_finish':
        race['coverage']['start_admission']['corroborated_at_seconds'] = 101
    elif mutation == 'negative_exit_index':
        event['exit_proof']['source_index'] = -1
    elif mutation == 'inverted_zero_laps':
        for obj in (event, event['fragments'][0]):
            obj.update(end_seconds=20, start_lap={'lap': 2, 'status': 'approximate',
                       'method': 'raw_leader_lap_boundaries'})
        event['exit_proof']['observed_seconds'] = 20
    else:
        for obj in (event, event['fragments'][0]):
            obj.update(end_seconds=None, end_status='ambiguous_transition', end_track_status=None,
                end_lap={'lap': None, 'status': 'unavailable', 'method': 'raw_leader_lap_boundaries'})
        event['fragments'][0]['uncertain_after_seconds'] = 40
        event['boundary_ambiguous'] = True
        event['exit_proof']['observed_seconds'] = 101 if mutation == 'exit_after_finish' else 30
    with pytest.raises(ValueError):
        validate_history(payload, 2026, 1)
