"""Money and supplier admission regressions. All wallets are local simulations."""
import asyncio
import json
import time

import httpx
import pytest

from conftest import request
from offence import spending
from offence.app import create_app
from offence.buyer_app import BuyerSettings, create_buyer_app
from offence.client import Buyer
from offence.crypto import Identity, digest
from offence.discovery import Discovery
from offence.store import Store
from test_buyer_app import setup, chat
from test_discovery import ad


def test_stopped_quotes_release_budget_and_crash_recovery_needs_no_sql_edit(tmp_path, monkeypatch):
    now = 100000
    monkeypatch.setattr(spending.time, 'time', lambda: now)
    spending.reserve(tmp_path, 'one', 7000, 1000, 16000)
    spending.reserve(tmp_path, 'two', 7000, 1000, 16000)
    now += 3 * 86400
    spending.recover_stopped(tmp_path)
    spending.reserve(tmp_path, 'three', 16000, 0, 16000)


def test_only_dispatched_exposure_remains_reserved_until_wallet_resolution(tmp_path, monkeypatch):
    now = 100000
    monkeypatch.setattr(spending.time, 'time', lambda: now)
    spending.reserve(tmp_path, 'session', 8000, 2000, 10000)
    (tmp_path/'session.0.attempt.json').write_text(json.dumps({'amount_msat':100,'fee_limit_msat':10}))
    (tmp_path/'session.1.attempt.json').write_text(json.dumps({'amount_msat':200,'fee_limit_msat':10}))
    (tmp_path/'session.0.payment.json').write_text(json.dumps({'amount_msat':100,'fee_msat':2}))
    spending.settle(tmp_path, 'session')
    now += 3 * 86400
    with pytest.raises(ValueError, match='Daily'):
        spending.reserve(tmp_path, 'too-much', 9690, 0, 10000)
    (tmp_path/'session.1.failed.json').write_text('{"status":"FAILED"}')
    spending.recover_stopped(tmp_path)
    spending.reserve(tmp_path, 'allowed', 9898, 0, 10000)


async def test_failed_legacy_stream_releases_unused_quote(config, manifest, tmp_path):
    class Backend:
        async def stream(self, *args):
            raise TimeoutError('backend unavailable')
            yield
    class Wallet:
        network = 'regtest'
    config.backend = 'none'
    config.lightning = 'lnd-regtest'
    config.allow_seller_claim = True
    config.offer.output_msat_per_token = 1000
    app = create_app(tmp_path/'provider', config, backend=Backend(), wallet=Wallet(), background=False)
    buyer = Buyer(Identity(), tmp_path/'buyer', Wallet())
    for _ in range(3):
        with pytest.raises(ValueError, match='stream failed'):
            async for _ in buyer.run('http://provider', app.state.provider.identity.public,
                    manifest.model_id, 'hello', 8, 8000, allow_lab=True, assurance='lab-unverified',
                    fee_limit_msat=0, total_fee_limit_msat=0, daily_limit_msat=16000,
                    transport=httpx.ASGITransport(app=app)):
                pass
    spending.reserve(tmp_path/'buyer', 'new', 16000, 0, 16000)


async def test_cancel_releases_unused_amount_but_keeps_attempt(tmp_path):
    buyer = Buyer(Identity(), tmp_path)
    async def source(*args, reservation, **kwargs):
        spending.reserve(tmp_path, 'cancelled', 8000, 0, 8000)
        reservation['session'] = 'cancelled'
        buyer.save('cancelled.0.attempt.json', {'amount_msat':100, 'fee_limit_msat':0})
        yield 'paid'
        await asyncio.Event().wait()
    buyer._run = source
    stream = buyer.run()
    assert await anext(stream) == 'paid'
    await stream.aclose()
    spending.reserve(tmp_path, 'remaining', 7900, 0, 8000)
    with pytest.raises(ValueError):
        spending.reserve(tmp_path, 'excess', 1, 0, 8000)


async def test_nonstream_partial_is_returned_retrievable_and_cools_route(tmp_path, config, manifest):
    app, client, supplier, owner = setup(tmp_path, config, manifest)
    async def interrupted(*args, **kwargs):
        yield {'type':'delta','text':'paid text','token_count':2,'amount_msat':100,'session':'test'}
        raise httpx.ReadTimeout('private detail')
    app.state.buyer['client'].run = interrupted
    async with client:
        result = await client.post('/v1/chat/completions', json=chat())
        assert result.status_code == 502
        body = result.json()
        assert body['partial_output'] == 'paid text'
        assert body['offence']['spent_msat'] == 100
        assert body['offence']['complete'] is False
        path = '/v1/purchases/' + body['id']
        assert (await client.get(path, headers={'Authorization':''})).status_code == 401
        recovered = (await client.get(path)).json()
        assert recovered['partial_output'] == 'paid text' and not recovered['complete']
        assert (await client.post('/v1/chat/completions', json=chat())).status_code == 409
    app.state.store.close()
    restarted = create_buyer_app(tmp_path/'buyer', background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url='http://127.0.0.1:8787',
            headers={'Authorization':'Bearer '+(tmp_path/'buyer/agent.key').read_text()}) as client:
        assert (await client.get(path)).json()['partial_output'] == 'paid text'


async def test_quote_flood_behind_one_proxy_cannot_exhaust_delivery_bucket(tmp_path, config, manifest):
    app = create_app(tmp_path, config, background=False)
    p, buyer = app.state.provider, Identity()
    req = request(buyer, p.identity.public, manifest)
    q = p.quote(req)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://provider') as client:
        for _ in range(40):
            await client.post('/v1/quote', json=request(Identity(), p.identity.public, manifest))
        assert p.active == 0
        response = await client.post('/v1/stream', json=buyer.sign({'type':'accept','session':q['body']['session'],
            'quote_hash':digest(q),'quote':q,'request':req}))
        assert response.status_code == 200
        assert json.loads(response.text.splitlines()[-1])['body']['type'] == 'end'


def test_acceptance_replay_after_restart_and_capacity_are_enforced(tmp_path, config, manifest):
    p = create_app(tmp_path, config, background=False).state.provider
    buyer = Identity()
    req = request(buyer, p.identity.public, manifest)
    q = p.quote(req)
    accepted = buyer.sign({'type':'accept','session':q['body']['session'],'quote_hash':digest(q),'quote':q,'request':req})
    p.accept(accepted)
    p.store.close()
    p = create_app(tmp_path, config, background=False).state.provider
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        p.accept(accepted)
    assert not p.active
    assert p.store.db.execute('SELECT count(*) FROM admission').fetchone()[0] == 1


def test_cache_rotation_preserves_owner_pinned_keys(tmp_path):
    store = Store(tmp_path/'db', max_peers=4)
    trusted = Identity()
    store.protected_signers = {trusted.public}
    store.ingest(ad(trusted))
    for _ in range(40):
        store.ingest(ad(Identity()))
    real = Identity()
    assert store.ingest(ad(real))
    keys = {p['signer'] for p in store.peers()}
    assert real.public in keys and trusted.public in keys and len(keys) == 4


async def test_saturated_gossip_cannot_starve_configured_seed(tmp_path, config):
    config.seeds = ['https://offence.ai']
    config.allowed_private_peers = config.seeds
    store = Store(tmp_path/'db')
    for _ in range(100):
        store.ingest(ad(Identity()))
    discovery = Discovery(config, Identity(), store)
    contacted = []
    async def exchange(endpoint, expected=None):
        contacted.append(endpoint)
    discovery.exchange = exchange
    await discovery.tick()
    assert 'https://offence.ai' in contacted


async def test_old_unrestricted_policy_does_not_silently_authorize_unknown_suppliers(tmp_path, config, manifest):
    app, client, supplier, owner = setup(tmp_path, config, manifest)
    settings = app.state.buyer['settings'].model_dump()
    settings.pop('allow_unknown_suppliers')  # Existing pre-upgrade policy.
    settings.update(privacy='any', trusted_providers=[])
    app.state.buyer['settings'] = BuyerSettings.model_validate(settings)
    async with client:
        assert (await client.post('/v1/chat/completions', json=chat())).status_code == 412
    assert supplier.state.provider.store.evidence()['sessions'] == {}


async def test_cheaper_fake_offer_does_not_receive_trusted_prompt(tmp_path, config, manifest):
    app, client, supplier, owner = setup(tmp_path, config, manifest)
    now = int(time.time())
    fake = Identity()
    app.state.store.ingest(fake.sign({'type':'advertisement','network':config.network,
        'issued':now,'expires':now+120,'sequence':1,'endpoint':'http://provider',
        'offer':{**config.offer.model_dump(),'text_chat':True}}))
    async with client:
        result = await client.post('/v1/chat/completions', json=chat())
        assert result.status_code == 200
        assert result.json()['offence']['provider'] == supplier.state.provider.identity.public


async def test_terminal_failed_wallet_recovery_releases_money_without_resending(tmp_path):
    from offence.crypto import seal
    provider = Identity()
    sealed = seal({'groups':[]}, {'amount_msat':100}, b'a'*32)
    batch = provider.sign({'type':'batch','sealed':sealed})
    class Wallet:
        async def track(self, *args): return {'status':'FAILED'}
        async def pay(self, *args): pytest.fail('Recovery cannot send money')
    buyer = Buyer(Identity(), tmp_path, Wallet())
    spending.reserve(tmp_path, 'session', 8000, 1000, 9000)
    buyer.save('session.0.batch.json', batch)
    buyer.save('session.0.attempt.json', {'provider':provider.public, 'batch_hash':digest(batch),
        'commitment':digest(sealed), 'payment_hash':sealed['payment_hash'],
        'amount_msat':100, 'fee_limit_msat':10})
    spending.settle(tmp_path, 'session')
    assert (await buyer.reconcile_payments())[0]['status'] == 'FAILED'
    spending.recover_stopped(tmp_path)
    spending.reserve(tmp_path, 'new', 9000, 0, 9000)
    assert await buyer.reconcile_payments() == []


async def test_recovery_endpoint_is_owner_only_and_refuses_active_purchases(tmp_path, config, manifest):
    app, client, supplier, owner = setup(tmp_path, config, manifest)
    headers = {'Authorization':'Bearer '+owner}
    async with client:
        assert (await client.post('/admin/payments/recover')).status_code == 401
        app.state.buyer['active'].add('busy')
        assert (await client.post('/admin/payments/recover', headers=headers)).status_code == 409
        app.state.buyer['active'].clear()
        assert (await client.post('/admin/payments/recover', headers=headers)).status_code == 412


def test_wallet_managed_fees_are_not_counted_as_inference(tmp_path):
    spending.reserve(tmp_path, 'session', 100, 0, 100)
    (tmp_path/'session.0.attempt.json').write_text(json.dumps({'amount_msat':100,'fee_limit_msat':0}))
    (tmp_path/'session.0.payment.json').write_text(json.dumps({'amount_msat':100,'fee_msat':200}))
    spending.settle(tmp_path, 'session')
    import sqlite3
    with sqlite3.connect(tmp_path/'spending.sqlite') as db:
        assert db.execute('SELECT amount,complete FROM reservations').fetchone() == (100,1)


async def test_proxy_clients_do_not_share_authenticated_quote_quota(tmp_path, config, manifest):
    app = create_app(tmp_path, config, background=False)
    p = app.state.provider
    spammer = Identity()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://provider') as client:
        for _ in range(35):
            response = await client.post('/v1/quote',json=request(spammer,p.identity.public,manifest))
        assert response.status_code == 429
        legitimate = await client.post('/v1/quote',json=request(Identity(),p.identity.public,manifest))
        assert legitimate.status_code == 200
        forged = request(spammer,p.identity.public,manifest)
        forged['signer'] = Identity().public
        assert (await client.post('/v1/quote',json=forged)).status_code == 400
