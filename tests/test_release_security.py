import json
import time

import httpx
import pytest

from offence.app import create_app
from offence.crypto import Identity, canonical
from offence.models import Advertisement, Config
from offence.store import Store, MAX_ADVERTISEMENT_BYTES, MAX_PEER_LIST_BYTES


def advertisement(identity, config, size=0):
    offer = config.offer.model_dump()
    offer['manifest']['sources'] = ['https://example.com/' + 'a' * size]
    now = int(time.time())
    return identity.sign(Advertisement(issued=now, expires=now+180, sequence=1,
        endpoint='https://8.8.8.8', offer=offer).model_dump())


def test_remote_advertisements_cannot_amplify_unbounded_peer_responses(tmp_path, config):
    store = Store(tmp_path/'db', max_peers=16)
    with pytest.raises(ValueError, match='byte limit'):
        store.ingest(advertisement(Identity(), config, MAX_ADVERTISEMENT_BYTES))
    for _ in range(10):
        store.ingest(advertisement(Identity(), config, 60000))
    assert store.peer_count('') == 10
    assert len(canonical(store.peers(16))) <= MAX_PEER_LIST_BYTES
    assert 0 < len(store.peers(16)) < 10
    store.close()


def test_remote_cache_flood_cannot_suppress_local_advertisement(tmp_path, config):
    store = Store(tmp_path/'db', max_peers=1)
    store.ingest(advertisement(Identity(), config))
    local = Identity()
    store.local_signer = local.public
    assert store.ingest(advertisement(local, config))
    assert store.peers()[0]['signer'] == local.public
    assert not store.ingest(advertisement(Identity(), config))
    store.close()


async def test_read_flood_is_rate_limited_and_schema_not_exposed(tmp_path, config):
    app = create_app(tmp_path, config, background=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/openapi.json')).status_code == 404
        codes = [(await client.get('/v1/providers')).status_code for _ in range(35)]
        assert 429 in codes
    app.state.store.close()


def test_fractional_mainnet_rate_retains_precision():
    data = {'lightning':'strike','strike_address':'example@strike.me','allow_seller_claim':True,
            'pricing':{'mode':'sats-per-token','sats_per_token':'0.001847639063708904'}}
    assert Config.model_validate(data).pricing.exact_token_price() == '1.847639063708904000'
    data['lightning'] = 'disabled'
    assert Config.model_validate(data).pricing.sats_per_token == '0.001847639063708904'


async def test_fee_shortfall_refuses_before_acceptance_or_backend_work(tmp_path, config, manifest):
    from offence.client import Buyer
    class Backend:
        async def stream(self, *args):
            raise AssertionError('GPU must not be called')
            yield {}
    class Wallet:
        network = 'regtest'
    config.offer.output_msat_per_token = 1
    app = create_app(tmp_path/'provider', config, backend=Backend(), wallet=Wallet(), background=False)
    buyer = Buyer(Identity(), tmp_path/'buyer', Wallet())
    with pytest.raises(ValueError, match='worst-case batch count'):
        async for _ in buyer.run('http://test', app.state.provider.identity.public,
                manifest.model_id, 'hello', 8, 8, allow_lab=True, fee_limit_msat=10,
                total_fee_limit_msat=79, transport=httpx.ASGITransport(app=app)):
            pass
    assert app.state.provider.active == 0
    assert not list((tmp_path/'buyer').glob('*.attempt.json'))
    app.state.store.close()
