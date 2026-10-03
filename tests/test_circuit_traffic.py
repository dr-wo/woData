import json
import pytest
from wodata.circuit_traffic import write_circuit_traffic,read_circuit_traffic,circuit_key


def test_storage_identity_null_and_tamper(tmp_path):
    payload={'circuit':'Monza','target_season':2026,'years':list(range(2018,2026)),
             'recipe_id':'fixed','interactions':[{'id':'one','residual_s':None,'advantage_s':-.4}],
             'groups':[], 'coverage':[]}
    path=write_circuit_traffic(payload,data_root=tmp_path)
    got,_=read_circuit_traffic(circuit='MONZA',target_season=2026,data_root=tmp_path,expected_recipe_id='fixed')
    assert got['interactions']==payload['interactions']
    original=path.read_text()
    path.write_text(original.replace('-0.4','0.4'))
    with pytest.raises(ValueError,match='hash mismatch'):
        read_circuit_traffic(circuit='Monza',target_season=2026,data_root=tmp_path)


def test_circuit_names_are_safe_and_separate():
    assert circuit_key(' Monza ')==circuit_key('monza')
    assert '/' not in circuit_key('../../Monza')
    assert circuit_key('A B')!=circuit_key('A-B')
    with pytest.raises(ValueError):circuit_key(' ')
