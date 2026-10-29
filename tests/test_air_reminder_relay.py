from pathlib import Path
from importlib.machinery import SourceFileLoader
import importlib.util
import sys
import os
from dataclasses import replace
import pytest
from core.reminder_upsert import ReminderUpsertAdapter, ConflictError
from test_reminder_upsert import FakeBackend

CONFIG = Path(os.environ.get('AIR_RELAY_CONFIG', str(Path.home() / 'claude-config')))
sys.path.insert(0,str(CONFIG))
spec = importlib.util.spec_from_loader('air_reminder_relay', SourceFileLoader('air_reminder_relay', str(CONFIG/'bin/life-ops')))
relay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = relay
spec.loader.exec_module(relay)

def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(relay,'STATE_ROOT',tmp_path/'state')
    backend=FakeBackend()
    service=ReminderUpsertAdapter(backend,lock_path=tmp_path/'adapter.lock')
    payload={'idempotency_key':'air-test','list_name':'AI','title':'Synthetic','notes':'Fixture only','due_at':'2026-09-30T16:00:00Z'}
    def call(action, values=None):
        return relay.canary_step('reminder.'+action, values or payload, reminder_adapter=service)
    return backend,call,payload

def test_relay_roundtrip_and_original_create_refused(tmp_path,monkeypatch):
    backend,call,payload=fixture(tmp_path,monkeypatch)
    created=call('create'); ident=created['external_id']
    assert call('create')['replayed'] is True
    updated=call('update',{**payload,'title':'Updated','external_id':ident})
    assert updated['title']=='Updated'
    with pytest.raises(relay.RelayError,match='replay differs'):
        call('create')
    assert backend.records[ident].title=='Updated'
    assert call('undo')['ok'] is True
    assert not backend.records

@pytest.mark.parametrize('action',['create','update','undo'])
def test_relay_preserves_manual_change(tmp_path,monkeypatch,action):
    backend,call,payload=fixture(tmp_path,monkeypatch)
    ident=call('create')['external_id']
    backend.records[ident]=replace(backend.records[ident], title='Manual')
    before=dict(backend.records)
    with pytest.raises((relay.RelayError,ConflictError)):
        call(action)
    assert backend.records==before

def test_relay_does_not_recreate_missing(tmp_path,monkeypatch):
    backend,call,payload=fixture(tmp_path,monkeypatch)
    call('create');backend.records.clear()
    with pytest.raises(relay.RelayError,match='missing or changed'):
        call('create')
    assert not backend.records
