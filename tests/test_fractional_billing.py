"""Fractional rates and prepaid funding, using simulated wallets only."""
from fractions import Fraction
import json

import httpx
import pytest

from conftest import request
from test_prepaid import Wallet
from offence.app import create_app
from offence.client import Buyer
from offence.crypto import Identity, digest
from offence.models import Pricing
from offence.pricing import charge, rate, cent_deposit, ceil_fraction


@pytest.mark.parametrize('value', ['0.00025', '0.0025', '0.00000001', '1.234567890123456789'])
def test_chunk_partition_never_changes_total(value):
    pricing = Pricing(mode='sats-per-token', sats_per_token=value)
    contract = {'output_msat_per_token':pricing.token_price(),
                'output_msat_per_token_exact':pricing.exact_token_price()}
    exact = Fraction(value)*1000
    for count in [1, 5, 128, 1024, 32768]:
        expected = ceil_fraction(exact*count)
        assert charge(contract, count) == expected
        for step in [1, 3, 8, 128]:
            total = sum(charge(contract, min(n+step, count))-charge(contract, n)
                        for n in range(0, count, step))
            assert total == expected
        assert 0 <= expected - exact*count < 1


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '1/3', '1e-20', '0.'+'1'*65])
def test_wire_prices_are_bounded_decimal_strings(value):
    with pytest.raises(ValueError):
        rate({'output_msat_per_token':1, 'output_msat_per_token_exact':value})


def test_one_cent_rounds_up_to_whole_sats():
    assert cent_deposit('100000') == 10000
    assert cent_deposit('90000') == 12000
    assert cent_deposit('125000') == 8000


def setup(tmp_path, config, batch=1, mainnet=True):
    config.offer.output_msat_per_token = 1
    config.offer.output_msat_per_token_exact = '0.25'
    config.offer.batch_tokens = batch
    config.prepaid_compute = True
    wallet = Wallet()
    if mainnet:
        wallet.network = 'mainnet'
        config.lightning = 'lnd-mainnet'
        config.allow_seller_claim = True
        config.pricing = Pricing(mode='sats-per-token', sats_per_token='0.00025', usd_per_btc='90000')
    app = create_app(tmp_path/'provider', config, wallet=wallet, background=False)
    buyer = Buyer(Identity(), tmp_path/'buyer', wallet)
    return app, buyer, wallet


async def buy(app, buyer, manifest, **changes):
    args = dict(assurance='seller-claim', allow_prepaid_compute=True, funding_limit_msat=12000,
                fee_limit_msat=0, total_fee_limit_msat=0, daily_limit_msat=50000,
                detailed=True, transport=httpx.ASGITransport(app=app))
    args.update(changes)
    return [x async for x in buyer.run('http://provider', app.state.provider.identity.public,
             manifest.model_id, 'hello', 8, 2, **args)]


async def test_cent_credit_small_chunks_reuse_and_recovery(tmp_path, config, manifest):
    app, buyer, wallet = setup(tmp_path, config)
    events = await buy(app, buyer, manifest)
    p = app.state.provider
    assert [e['amount_msat'] for e in events if e['type']=='delta'] == [1,0,0,0,1]
    assert wallet.payments == 1
    assert next(iter(wallet.invoices.values()))[1] == 12000
    assert p.credits.balance(buyer.identity.public) == 11998
    credit = json.loads(next(buyer.directory.glob('*.credit.json')).read_text())
    assert credit['charged_msat'] == 2 and credit['deposited_msat'] == 12000
    assert not credit['pending']
    recovered = await buyer.recover_prepaid_output('http://provider', p.identity.public,
        credit['session'], transport=httpx.ASGITransport(app=app))
    assert recovered['output_tokens'] == 5 and recovered['spent_msat'] == 2
    await buy(app, buyer, manifest)
    assert wallet.payments == 1
    assert p.store.token_totals()["paid_tokens"] == 10
    assert p.credits.balance(buyer.identity.public) == 11996
    # Zero-increment chunks still have private keys, never public free keys.
    batches = list(p.store.db.execute('SELECT envelope FROM batches'))
    assert all(json.loads(row[0])['body']['free_key'] is None for row in batches)


@pytest.mark.parametrize('changes', [{'funding_limit_msat':11999}, {'daily_limit_msat':11999}])
async def test_cent_cannot_bypass_funding_or_daily_caps(tmp_path, config, manifest, changes):
    app, buyer, wallet = setup(tmp_path, config)
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        await buy(app, buyer, manifest, **changes)
    assert wallet.payments == 0
    assert not app.state.provider.running
    assert app.state.store.db.execute('SELECT count(*) FROM admission').fetchone()[0] == 0


def test_legacy_buyer_refused_before_gpu_or_wallet(tmp_path, config, manifest):
    app, buyer, wallet = setup(tmp_path, config, mainnet=False)
    p = app.state.provider
    with pytest.raises(ValueError, match='cumulative fractional'):
        p.quote(request(buyer.identity,p.identity.public,manifest,max_total_msat=2,allow_prepaid_compute=True))
    assert wallet.payments == 0 and not p.running


def test_rate_change_with_same_rounded_ceiling_invalidates_quote(tmp_path, config, manifest):
    app, buyer, wallet = setup(tmp_path, config, mainnet=False)
    p = app.state.provider
    req = request(buyer.identity,p.identity.public,manifest,max_total_msat=2,
                  allow_prepaid_compute=True,fractional_billing=True)
    q = p.quote(req)
    p.credits.deposit('test',buyer.identity.public,10)
    config.offer.output_msat_per_token_exact = '0.24'
    with pytest.raises(ValueError, match='Offer changed'):
        p.accept(buyer.identity.sign({'type':'accept','session':q['body']['session'],
                 'quote_hash':digest(q),'quote':q,'request':req}))
    assert p.credits.balance(buyer.identity.public) == 10


async def test_failed_fractional_output_keeps_only_consumed_credit(tmp_path, config, manifest):
    app, buyer, wallet = setup(tmp_path, config)
    class Interrupted:
        async def stream(self, *args):
            yield {'token_ids':[1], 'text':'Paid fragment'}
            raise TimeoutError('backend stopped')
    app.state.provider.backend = Interrupted()
    with pytest.raises(ValueError, match='stream failed'):
        await buy(app, buyer, manifest)
    assert wallet.payments == 1
    assert app.state.provider.credits.balance(buyer.identity.public) == 11999
    await buyer.recover_credits(['http://provider'], transport=httpx.ASGITransport(app=app))
    record = json.loads(next(buyer.directory.glob('*.credit.json')).read_text())
    assert record['charged_msat'] == 1 and not record['pending']
    result = await buyer.recover_prepaid_output('http://provider', app.state.provider.identity.public,
        record['session'], transport=httpx.ASGITransport(app=app))
    assert result['partial_output'] == 'Paid fragment' and result['spent_msat'] == 1
    assert not result['complete']


def test_advertised_fractional_price_is_not_ranked_by_integer_ceiling(tmp_path, config, manifest):
    import time
    from offence.models import GatewayConfig, RoutingPolicy
    from offence.routing import Router
    from offence.store import Store
    store = Store(tmp_path/'store.sqlite')
    config.allowed_private_peers = ['http://supplier']
    config.gateway = GatewayConfig(policies=[RoutingPolicy(alias='auto',model_ids=[manifest.model_id],strategy='cheapest')])
    identities = sorted([Identity(),Identity()],key=lambda i:i.public)
    for identity, price in zip(identities, ['0.9','0.2']):
        store.ingest(identity.sign({'type':'advertisement','network':'offence-lab-v1',
            'issued':int(time.time()),'expires':int(time.time())+120,'sequence':1,'endpoint':'http://supplier',
            'offer':{**config.offer.model_dump(),'text_chat':True,'output_msat_per_token':1,
                     'output_msat_per_token_exact':price}}))
    assert Router(config,store).select('auto',8,max_price_msat=1).provider == identities[1].public
