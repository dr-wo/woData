"""Versioned descriptive circuit traffic artifacts; no analysis or model fitting."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from . import get_data_root
from .contracts import canonical_hash

SCHEMA_VERSION = 'schema_v1'


def circuit_key(circuit: str) -> str:
    normalized = ' '.join(str(circuit).casefold().split())
    if not normalized:
        raise ValueError('A circuit/location identity is required')
    slug = re.sub(r'[^a-z0-9]+', '-', normalized).strip('-') or 'circuit'
    return slug + '-' + hashlib.sha256(normalized.encode()).hexdigest()[:8]


def circuit_traffic_root(*, circuit, target_season, data_root=None):
    return (get_data_root(data_root).resolve() / 'wostrategy' / 'circuit_traffic_history' /
            SCHEMA_VERSION / f'circuit={circuit_key(circuit)}' / f'target_season={int(target_season)}')


def circuit_traffic_source_root(*, circuit, data_root=None):
    return (get_data_root(data_root).resolve() / 'wostrategy' / 'circuit_traffic_sources' /
            SCHEMA_VERSION / f'circuit={circuit_key(circuit)}')


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + '\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_circuit_traffic(result, *, data_root=None):
    """Persist the complete record-based artifact and atomically point to it.

    Old content-addressed results remain readable. JSON preserves null missingness,
    boolean provenance, histogram arrays and the minimum's source interaction IDs.
    """
    value = dict(result)
    value['schema_version'] = SCHEMA_VERSION
    identity = canonical_hash(value)
    root = circuit_traffic_root(circuit=value['circuit'], target_season=value['target_season'], data_root=data_root)
    destination = root / 'artifacts' / identity / 'result.json'
    _atomic_json(destination, value)
    _atomic_json(root / 'latest.json', {'artifact_id': identity, 'schema_version': SCHEMA_VERSION})
    return destination


def read_circuit_traffic(*, circuit, target_season, data_root=None, expected_recipe_id=None):
    root = circuit_traffic_root(circuit=circuit, target_season=target_season, data_root=data_root)
    pointer = json.loads((root / 'latest.json').read_text())
    identity = pointer['artifact_id']
    if not re.fullmatch(r'[a-f0-9]{64}', identity):
        raise ValueError('Invalid traffic artifact identity')
    path = root / 'artifacts' / identity / 'result.json'
    result = json.loads(path.read_text())
    if canonical_hash(result) != identity:
        raise ValueError('Traffic artifact content hash mismatch')
    if (result['schema_version'] != SCHEMA_VERSION or
            circuit_key(result['circuit']) != circuit_key(circuit) or
            result['target_season'] != int(target_season) or
            result['years'] != list(range(int(target_season)-8, int(target_season)))):
        raise ValueError('Traffic artifact schema/circuit/window mismatch')
    if expected_recipe_id is not None and result['recipe_id'] != expected_recipe_id:
        raise ValueError('Traffic artifact calculation identity is stale; explicitly prepare history again')
    return result, path


def write_circuit_traffic_session(*, circuit, year, round_number, laps, race_control, metadata, data_root=None):
    """Store reusable prepared timing inputs under woData-owned source paths."""
    root = circuit_traffic_source_root(circuit=circuit, data_root=data_root) / f'year={int(year)}' / f'round={int(round_number)}'
    root.mkdir(parents=True, exist_ok=True)
    laps.to_pickle(root / 'laps.pkl')
    race_control.to_csv(root / 'race_control.csv', index=False)
    _atomic_json(root / 'metadata.json', dict(metadata))
    return root


def read_circuit_traffic_session(path):
    import pandas as pd
    root = Path(path)
    paths = [root / name for name in ['laps.pkl','race_control.csv','metadata.json']]
    return dict(laps=pd.read_pickle(paths[0]), race_control=pd.read_csv(paths[1]),
        coverage=json.loads(paths[2].read_text()),
        identity={'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}})
