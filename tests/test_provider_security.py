import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from conftest import request
from offence.app import create_app
from offence.backend import Fixture, Vllm
from offence.crypto import Identity, digest
from offence.gateway import ManagedStream
from offence.limits import ResourceLimits
from offence.models import Config
from offence.runtime import prepare
from offence.store import Store


def accept_work(p, manifest):
    buyer = Identity()
    req = request(buyer, p.identity.public, manifest)
    q = p.quote(req)
    return p.accept(buyer.sign({'type':'accept','session':q['body']['session'],
        'quote_hash':digest(q),'quote':q,'request':req}))


def test_work_quota_survives_restart_and_identity_rotation(tmp_path, config, manifest):
    config.max_work_tokens_per_hour = manifest.context_tokens
    app = create_app(tmp_path, config, background=False)
    accept_work(app.state.provider, manifest)
    app.state.store.close()
    app = create_app(tmp_path, config, background=False)
    with pytest.raises(ValueError, match='hourly work'):
        accept_work(app.state.provider, manifest)
    assert app.state.provider.active == 0


def test_quote_flood_does_not_reserve_work_or_evict_portable_quote(tmp_path, config, manifest, monkeypatch):
    config.max_requests_per_hour = 1
    p = create_app(tmp_path, config, background=False).state.provider
    buyer = Identity()
    req = request(buyer, p.identity.public, manifest)
    q = p.quote(req)
    for _ in range(200):
        p.quote(request(Identity(), p.identity.public, manifest))
    assert len(p.pending) == 128 and p.active == 0
    assert p.store.db.execute('SELECT count(*) FROM admission').fetchone()[0] == 0
    assert p.store.evidence()['sessions'] == {}
    p.accept(buyer.sign({'type':'accept','session':q['body']['session'],
        'quote_hash':digest(q),'quote':q,'request':req}))
    p.release(q['body']['session'])
    with pytest.raises(ValueError, match='hourly work'):
        accept_work(p, manifest)
    monkeypatch.setattr(p.store, 'storage_available', lambda: False)
    with pytest.raises(ValueError, match='storage'):
        accept_work(p, manifest)


async def test_disconnect_closes_backend_and_releases_gpu_slot(tmp_path, config, manifest):
    closed = []
    class Backend:
        async def stream(self, *args):
            try:
                yield {'token_ids': [1, 2], 'text': 'partial'}
                await asyncio.Event().wait()
            finally:
                closed.append(True)
    p = create_app(tmp_path, config, backend=Backend(), background=False).state.provider
    b = Identity()
    quote = p.quote(request(b, p.identity.public, manifest))
    q, req = p.accept(b.sign({'type': 'accept', 'session': quote['body']['session'], 'quote_hash': digest(quote)}))
    stream = p.stream(q, req)
    assert (await anext(stream))['body']['type'] == 'batch'
    await stream.aclose()
    assert closed == [True] and p.active == 0


async def test_generation_deadline_closes_stalled_backend(tmp_path, config, manifest):
    config.offer.generation_deadline_s = 1
    closed = []
    class Backend:
        async def stream(self, *args):
            try:
                await asyncio.Event().wait()
                yield {}
            finally:
                closed.append(True)
    p = create_app(tmp_path, config, backend=Backend(), background=False).state.provider
    b = Identity()
    quote = p.quote(request(b, p.identity.public, manifest))
    q, req = p.accept(b.sign({'type': 'accept', 'session': quote['body']['session'], 'quote_hash': digest(quote)}))
    result = [x async for x in p.stream(q, req)]
    assert result[-1]['body']['type'] == 'error' and p.active == 0 and closed


async def test_slow_response_consumer_is_cancelled():
    closed = []
    async def app(scope, receive, send):
        try:
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'hello'})
        finally:
            closed.append(True)
    async def send(message):
        await asyncio.Event().wait()
    limited = ResourceLimits(app, send_timeout=.01)
    await limited({'type': 'http'}, None, send)
    assert limited.active == 0 and closed


async def test_header_failure_releases_reservation():
    released = []
    async def body():
        yield b'hello'
    async def send(message):
        raise RuntimeError('client gone')
    response = ManagedStream(body(), lambda: released.append(True))
    with pytest.raises(RuntimeError):
        await response({'type': 'http', 'asgi': {'spec_version': '2.4'}}, None, send)
    assert released == [True]


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://gpu/admin', 'http://user:pass@gpu',
                                  'http://gpu?url=http://other', 'http://gpu#x'])
def test_backend_origin_cannot_contain_paths_or_credentials(url):
    with pytest.raises(ValueError):
        Config(backend='vllm', backend_url=url, backend_model='fixed')


def test_runtime_migration_preserves_identity_and_refuses_symlinks(tmp_path):
    (tmp_path / 'identity.key').write_bytes(b'stored-identity')
    (tmp_path / 'offence.sqlite').write_bytes(b'database')
    prepare(tmp_path, os.getuid(), os.getgid())
    prepare(tmp_path, os.getuid(), os.getgid())
    assert (tmp_path / 'runtime/identity.key').read_bytes() == b'stored-identity'
    assert not (tmp_path / 'identity.key').exists()
    outside = tmp_path / 'outside'
    outside.write_text('unchanged')
    (tmp_path / 'runtime/link').symlink_to(outside)
    with pytest.raises(ValueError, match='Unsafe'):
        prepare(tmp_path, os.getuid(), os.getgid())
    assert outside.read_text() == 'unchanged'


def test_runtime_migration_never_overwrites_identity(tmp_path):
    (tmp_path / 'runtime').mkdir()
    (tmp_path / 'runtime/identity.key').write_text('new')
    (tmp_path / 'identity.key').write_text('old')
    with pytest.raises(ValueError, match='Conflicting'):
        prepare(tmp_path, os.getuid(), os.getgid())
    assert (tmp_path / 'identity.key').read_text() == 'old'
