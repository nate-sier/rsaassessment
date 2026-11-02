"""Regression checks that do not access STATSports or require real athlete data."""
from pathlib import Path
import io
import json
import os
import sys
import zipfile
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workspace_portability import default_locations, backup_workspace, restore_workspace
from export_name_override import trial_export_names, partition_saved_trial_docs

ROOT = Path(__file__).resolve().parents[1]


def test_no_hardcoded_api_credential():
    backend = (ROOT / 'statsports_backend_v22.py').read_text()
    assert "API_KEY = os.environ.get('STATSPORTS_API_KEY'" in backend
    assert "API_KEY = '" not in backend
    assert 'Missing STATSports API credential' in backend


def test_cloud_paths_and_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv('STATSPORTS_CLOUD_MODE', '1')
    monkeypatch.delenv('STATSPORTS_DATA_ROOT', raising=False)
    monkeypatch.delenv('STATSPORTS_REVIEW_ROOT', raising=False)
    data, reviewed = default_locations(tmp_path)
    assert data == tmp_path / '.runtime_data' / 'automated'
    assert reviewed == tmp_path / '.runtime_data' / 'reviewed'
    monkeypatch.setenv('STATSPORTS_DATA_ROOT', str(tmp_path / 'mine'))
    assert default_locations(tmp_path)[0] == tmp_path / 'mine'


def test_workspace_backup_restore_and_identity(tmp_path):
    source = tmp_path / 'src'
    corrected = tmp_path / 'review'
    gp = source / 'statsports_all_assessments_2026-10-29' / 'cache' / 'sample.sqlite'
    gp.parent.mkdir(parents=True)
    gp.write_bytes(b'GPS-TEST-CACHE')
    cp = corrected / 'statsports_all_assessments_2026-10-29' / 'corrections' / 'trial.json'
    cp.parent.mkdir(parents=True)
    cp.write_text(json.dumps({'match_key': 'test', 'history': [{'event':'test'}]}))
    (source / 'statsports_all_assessments_2026-10-29' / 'secrets.toml').write_text('NOPE')
    blob = backup_workspace(source, corrected)
    assert b'NOPE' not in blob
    dest1, dest2 = tmp_path / 'dst', tmp_path / 'dst_review'
    assert restore_workspace(blob, dest1, dest2) == 2
    assert (dest1 / gp.relative_to(source)).read_bytes() == b'GPS-TEST-CACHE'
    assert json.loads((dest2 / cp.relative_to(corrected)).read_text())['match_key'] == 'test'
    with pytest.raises(FileExistsError):
        restore_workspace(blob, dest1, dest2)


def test_zip_path_traversal_rejected(tmp_path):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as f:
        f.writestr('automated/../other.json', '{}')
    with pytest.raises(ValueError):
        restore_workspace(b.getvalue(), tmp_path/'source', tmp_path/'review')
    assert not (tmp_path/'other.json').exists()


def test_trial_specific_names_and_stale_file():
    index = [
        {'match_key': 'a', 'player': 'Athlete', 'raw_id':'recording1','session_player_id':'player1', 'start':'2026-10-29T14:00:00'},
        {'match_key': 'b', 'player': 'Athlete', 'raw_id':'recording2','session_player_id':'player2', 'start':'2026-10-29T15:00:00'}]
    docs=[{'match_key':'a','rawDataId':'recording1','session_player_id':'player1','export_player_name':'Player A'},
          {'match_key':'b','rawDataId':'recording2','session_player_id':'player2','export_player_name':'Player B'},
          {'match_key':'historical','rawDataId':'old','session_player_id':'old','export_player_name':'Old'}]
    current, stale = partition_saved_trial_docs(index,docs)
    assert len(stale)==1
    assert trial_export_names(index,current)=={'a':'Player A','b':'Player B'}
    current[0]['rawDataId']='wrong'
    with pytest.raises(ValueError,match='recording identity'):
        trial_export_names(index,current)
