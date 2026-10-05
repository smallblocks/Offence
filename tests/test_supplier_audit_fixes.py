"""Supplier audit regressions, with temporary state and no live wallet/GPU."""
import asyncio
import gzip
import time

import httpx
import pytest

from conftest import request
from offence.app import create_app
from offence.crypto import Identity
from offence.discovery import read_json
from offence.limits import ResourceLimits
from offence.wire import identity_bytes
from test_prepaid import setup_prepaid


def resource_guard(app):
    app.middleware_stack = app.build_middleware_stack()
    item = app.middleware_stack
    while not isinstance(item, ResourceLimits):
        item = item.app
    return item


async def test_complete_asgi_stack_limits_unread_signed_bodies(tmp_path, config):
    app = create_app(tmp_path, config, background=False)
    guard = resource_guard(app)
    gate = asyncio.Event()
    entered = 0
    statuses = []
    async def receive():
        nonlocal entered
        entered += 1
        await gate.wait()
        return {'type':'http.request','body':b'{}','more_body':False}
    async def send(message):
        if message['type'] == 'http.response.start':
            statuses.append(message['status'])
    scope = {'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1',
             'method':'POST','scheme':'http','path':'/v1/quote','raw_path':b'/v1/quote',
             'query_string':b'','root_path':'','headers':[],'client':('127.0.0.1',1),'server':('test',80)}
    tasks = [asyncio.create_task(app(scope,receive,send)) for _ in range(40)]
    try:
        for _ in range(100):
            if entered == 32 and statuses.count(503) == 8:
                break
            await asyncio.sleep(.001)
        assert entered == guard.active == 32
        assert statuses.count(503) == 8
    finally:
        gate.set()
        await asyncio.gather(*tasks)
        app.state.store.close()
    assert guard.active == 0


async def test_malformed_signatures_spend_pre_auth_budget_without_blocking_delivery(tmp_path, config):
    app = create_app(tmp_path, config, background=False)
    guard = resource_guard(app)
    # Exhaust only admission from this proxy, including malformed requests.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        statuses = [(await client.post('/v1/quote',json={})).status_code for _ in range(100)]
        assert 400 in statuses and 429 in statuses
        assert (await client.get('/health')).status_code == 200
    assert guard.active == 0
    app.state.store.close()


def funding_request(app, buyer, manifest):
    q = app.state.provider.quote(request(buyer, app.state.provider.identity.public, manifest,
            max_total_msat=80, allow_prepaid_compute=True))
    return buyer.sign({'type':'fund-credit','quote':q,'issued':int(time.time())})


async def test_invoice_replay_reuses_voucher_across_supplier_restart(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    funding = funding_request(app, buyer.identity, manifest)
    first = await app.state.provider.fund_credit(funding)
    assert await app.state.provider.fund_credit(funding) == first
    app.state.store.close()
    app = create_app(tmp_path/'provider',config,wallet=wallet,background=False)
    assert await app.state.provider.fund_credit(funding) == first
    assert len(wallet.invoices) == 1
    assert app.state.provider.active == 0
    app.state.store.close()


async def test_uncertain_invoice_creation_never_reissued_after_restart(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    funding = funding_request(app, buyer.identity, manifest)
    calls = []
    async def lost_response(*args):
        calls.append(True)
        raise TimeoutError('Outcome unknown')
    wallet.invoice = lost_response
    with pytest.raises(TimeoutError):
        await app.state.provider.fund_credit(funding)
    app.state.store.close()
    app = create_app(tmp_path/'provider',config,wallet=wallet,background=False)
    with pytest.raises(ValueError,match='unresolved'):
        await app.state.provider.fund_credit(funding)
    assert calls == [True]
    app.state.store.close()


async def test_cancelled_invoice_creation_retains_attempt(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    funding = funding_request(app,buyer.identity,manifest)
    entered = asyncio.Event()
    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()
    wallet.invoice = blocked
    task = asyncio.create_task(app.state.provider.fund_credit(funding))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ValueError,match='unresolved'):
        await app.state.provider.fund_credit(funding)
    assert not app.state.provider.funding_lock.locked()
    app.state.store.close()


async def test_invoice_budget_survives_identity_rotation_and_restart(tmp_path, config, manifest):
    config.max_requests_per_hour = 2
    app, _, wallet = setup_prepaid(tmp_path, config)
    for _ in range(2):
        await app.state.provider.fund_credit(funding_request(app,Identity(),manifest))
    app.state.store.close()
    app = create_app(tmp_path/'provider',config,wallet=wallet,background=False)
    with pytest.raises(ValueError,match='hourly invoice'):
        await app.state.provider.fund_credit(funding_request(app,Identity(),manifest))
    assert len(wallet.invoices) == 2
    app.state.store.close()


async def test_legacy_recovery_refuses_truncation_and_pagination_recovers_all(tmp_path, config, manifest):
    app, buyer, _ = setup_prepaid(tmp_path,config)
    p = app.state.provider
    q = funding_request(app,buyer.identity,manifest)['body']['quote']
    session = q['body']['session']
    p.store.session(session,buyer.identity.public,q)
    for seq in range(20):
        p.store.batch(session,seq,{'sequence':seq,'padding':'x'*60000})
    def message(**extra):
        return buyer.identity.sign({'type':'recover','session':session,'issued':int(time.time()),**extra})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.post('/v1/recover',json=message())).status_code == 400
        offset, seen = 0, []
        while offset is not None:
            response = await client.post('/v1/recover',json=message(offset=offset))
            assert response.status_code == 200 and len(response.content) < 300000
            body = response.json()['body']
            assert body['evidence'] == []
            seen.extend(x['sequence'] for x in body['batches'])
            offset = body['next_offset']
        assert seen == list(range(20))
    app.state.store.close()


@pytest.mark.parametrize('encoding',['gzip','deflate','br','gzip, identity'])
async def test_encoded_peer_response_rejected_before_any_body_read(encoding):
    touched = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            touched.append(True)
            yield gzip.compress(b' '*(8*1024*1024))
    async def transport(request):
        return httpx.Response(200,headers={'Content-Encoding':encoding},stream=Body())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        async with client.stream('GET','https://peer.test') as response:
            with pytest.raises(ValueError,match='Compressed'):
                await read_json(response)
    assert touched == []


async def test_identity_reader_keeps_bounded_json_and_plain_stream_compatibility():
    response = httpx.Response(200,json={'ok':True},request=httpx.Request('GET','https://peer.test'))
    assert await read_json(response) == {'ok':True}
    response = httpx.Response(200,content=b'plain',headers={'Content-Encoding':'identity'})
    assert b''.join([chunk async for chunk in identity_bytes(response)]) == b'plain'
