"""Exact relay functions with local shell fixtures; no Messages or live tmux."""
import ast
import asyncio
import os
from pathlib import Path
import re
import shlex
import signal
import tempfile
import time
import types
import uuid

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'core/tools.py'


def functions():
    names = {'_tmux_relay_osascript', 'send_imessage_reliable'}
    selected = [n for n in ast.parse(SOURCE.read_text()).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    ns = dict(asyncio=asyncio, Path=Path, re=re, shlex=shlex, tempfile=tempfile,
              uuid=uuid, _TMUX='fixture-tmux', _RELAY_SESSIONS=['claude', 'main'],
              _IMESSAGE_ERROR_CODES={1, 22, 102, 1032})
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


class Result:
    def __init__(self, code=0): self.returncode = code
    async def wait(self): return self.returncode
    async def communicate(self): return b'', b''


class Relay:
    def __init__(self, monkeypatch, tmp_path, *, publish=True, startup=0, available=True):
        self.ns = functions()
        self.children, self.killed, self.roots = {}, [], []
        self.publish, self.startup, self.available = publish, startup, available
        actual_create = asyncio.create_subprocess_exec

        async def create(*args, **kwargs):
            if args[1] == 'has-session': return Result(0 if self.available else 1)
            assert args[1] == 'new-session'
            session = args[args.index('-s')+1]
            assert session.startswith('imsg-relay-') and session not in ('claude', 'main')
            if self.startup: return Result(self.startup)
            shell = shlex.split(args[-1])[2]
            # Execute the exact publication shell around a harmless shell
            # fixture in place of osascript; no alternate transport is used.
            shell = shell.replace('/usr/bin/osascript', '/bin/bash')
            if not self.publish:
                shell = shell.rsplit('/bin/mv', 1)[0] + '/usr/bin/false'
            script = next(Path(word) for word in shlex.split(shell) if word.endswith('script.scpt'))
            self.roots.append(script.parent)
            assert script.parent.stat().st_mode & 0o777 == 0o700
            assert script.stat().st_mode & 0o777 == 0o600
            self.children[session] = await actual_create('/bin/bash', '-c', shell,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True)
            return Result()

        async def kill(session):
            self.killed.append(session)
            child = self.children.get(session)
            if child:
                try: os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                await child.wait()

        proxy = types.SimpleNamespace(create_subprocess_exec=create, subprocess=asyncio.subprocess,
            wait_for=asyncio.wait_for, TimeoutError=asyncio.TimeoutError,
            get_running_loop=asyncio.get_running_loop, sleep=asyncio.sleep)
        self.ns.update(asyncio=proxy, _kill_tmux_session=kill)

    async def call(self, script, timeout=1):
        return await self.ns['_tmux_relay_osascript'](script, timeout)

    def clean(self):
        assert all(not root.exists() for root in self.roots)
        assert all(child.returncode is not None for child in self.children.values())
        assert len(self.killed) == len(self.roots)


def test_partial_output_waits_for_completion(monkeypatch, tmp_path):
    relay = Relay(monkeypatch, tmp_path)
    async def run():
        task = asyncio.create_task(relay.call('printf partial; sleep .3; printf complete'))
        await asyncio.sleep(.1)
        assert not task.done()
        root = relay.roots[0]
        assert (root/'output.tmp').read_text() == 'partial'
        assert not (root/'complete').exists()
        assert (root/'output.tmp').stat().st_mode & 0o777 == 0o600
        return await task
    assert asyncio.run(run()) == (True, 'partialcomplete')
    relay.clean()


@pytest.mark.parametrize('code', [1, 7, 127, 255])
def test_actual_exit_failure_is_not_success(monkeypatch, tmp_path, code):
    relay = Relay(monkeypatch, tmp_path)
    assert asyncio.run(relay.call(f'printf failure; exit {code}')) == (False, 'failure')
    relay.clean()


def test_empty_output_with_zero_exit_is_valid(monkeypatch, tmp_path):
    relay = Relay(monkeypatch, tmp_path)
    assert asyncio.run(relay.call('exit 0')) == (True, '')
    relay.clean()


def test_timeout_keeps_partial_output_from_becoming_receipt(monkeypatch, tmp_path):
    relay = Relay(monkeypatch, tmp_path)
    start = time.monotonic()
    ok, message = asyncio.run(relay.call('printf partial; sleep 5', .1))
    assert not ok and 'without completion receipt' in message
    assert time.monotonic()-start < 2
    relay.clean()


def test_output_publication_without_atomic_exit_receipt_is_not_success(monkeypatch, tmp_path):
    relay = Relay(monkeypatch, tmp_path, publish=False)
    ok, _ = asyncio.run(relay.call('printf complete; exit 0', .1))
    assert not ok
    relay.clean()


def test_startup_failure_cleans_only_owned_session(monkeypatch, tmp_path):
    relay = Relay(monkeypatch, tmp_path, startup=1)
    assert asyncio.run(relay.call('exit 0')) == (False, 'tmux session startup failed')
    assert len(relay.killed) == 1 and not relay.children


def test_missing_relay_does_not_create_session(monkeypatch, tmp_path):
    relay = Relay(monkeypatch, tmp_path, available=False)
    assert asyncio.run(relay.call('exit 0')) == (False, 'no tmux session for relay')
    assert not relay.children and not relay.killed


def test_cancellation_cleans_owned_process_and_private_files(monkeypatch, tmp_path):
    relay = Relay(monkeypatch, tmp_path)
    async def run():
        task = asyncio.create_task(relay.call('sleep 5'))
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(run())
    relay.clean()


@pytest.mark.parametrize('cancel', [True, False])
def test_startup_cancellation_or_timeout_reaps_client_before_session(cancel):
    ns = functions(); events = []; started = None
    class Startup:
        returncode = None
        async def communicate(self):
            started.set()
            await asyncio.Future()
        def kill(self):
            events.append('client_killed'); self.returncode = -9
        async def wait(self): events.append('client_reaped'); return self.returncode
    client = Startup()
    async def create(*args, **kwargs):
        return Result() if args[1] == 'has-session' else client
    async def cleanup(session):
        assert session.startswith('imsg-relay-')
        events.append('session_stopped')
    ns['asyncio'] = types.SimpleNamespace(create_subprocess_exec=create,
        subprocess=asyncio.subprocess, wait_for=asyncio.wait_for,
        TimeoutError=asyncio.TimeoutError, get_running_loop=asyncio.get_running_loop,
        sleep=asyncio.sleep)
    ns['_kill_tmux_session'] = cleanup
    async def run():
        nonlocal started
        started = asyncio.Event()
        task = asyncio.create_task(ns['_tmux_relay_osascript']('fixture', 1))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            assert await task == (False, 'tmux session startup timed out')
    asyncio.run(run())
    assert events == ['client_killed', 'client_reaped', 'session_stopped']


@pytest.mark.parametrize('prior', ['SMS', 'RCS', 'iMessage', None])
@pytest.mark.parametrize('outcome', ['success', 'relay_error', 'missing_row', 'sms_row', 'imessage_error'])
def test_imessage_only_never_falls_back(prior, outcome):
    ns = functions(); calls = []
    async def history(_): return prior
    async def send(buddy, body, service):
        calls.append(service)
        return outcome != 'relay_error', 'fixture'
    async def verify(*args, **kwargs):
        return {'success':('iMessage', 0), 'sms_row':('SMS', 0),
                'imessage_error':('iMessage', 1)}.get(outcome)
    ns.update(_chatdb_service_for_handle=history, _send_via_osascript_service=send,
              _chatdb_verify_send=verify, _mac_now_ns=lambda:123)
    ok, _ = asyncio.run(ns['send_imessage_reliable']('+15555550101', 'fixture',
                          _approved=True, imessage_only=True))
    assert calls == ['iMessage']
    assert ok == (outcome == 'success')


def test_legacy_default_preserves_existing_service_selection():
    ns = functions(); calls = []
    async def history(_): return 'SMS'
    async def send(buddy, body, service): calls.append(service); return True, 'fixture'
    async def verify(*args, **kwargs): return 'SMS', 0
    ns.update(_chatdb_service_for_handle=history, _send_via_osascript_service=send,
              _chatdb_verify_send=verify, _mac_now_ns=lambda:123)
    assert asyncio.run(ns['send_imessage_reliable']('+15555550101','fixture',_approved=True))[0]
    assert calls == ['SMS']
