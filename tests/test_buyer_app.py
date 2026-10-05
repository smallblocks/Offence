import json
import time

import httpx
import pytest

from offence.app import create_app
from offence.backend import Fixture
from offence.buyer_app import BuyerSettings, create_buyer_app, private_write
from offence.crypto import Identity
from offence.models import RoutingPolicy
from offence.routing import Router


class ChatFixture(Fixture):
    def stream_chat(self, messages, max_tokens):
        return self.stream('', max_tokens)


def setup(tmp_path, config, manifest, **settings):
    provider = create_app(tmp_path/'provider', config, backend=ChatFixture(), background=False)
    app = create_buyer_app(tmp_path/'buyer', background=False, transport=httpx.ASGITransport(app=provider))
    state = app.state.buyer
    state['settings'] = BuyerSettings(approved_origins=['http://provider'], seeds=[], model_ids=[manifest.model_id],
        assurance='lab-unverified', max_output_tokens=8, trusted_providers=[provider.state.provider.identity.public], **settings)
    now = int(time.time())
    ad = provider.state.provider.identity.sign({'type':'advertisement','network':config.network,
        'issued':now,'expires':now+120,'sequence':1,'endpoint':'http://provider',
        'offer':{**config.offer.model_dump(),'text_chat':True}})
    app.state.store.ingest(ad)
    owner=(tmp_path/'buyer/owner.key').read_text(); agent=(tmp_path/'buyer/agent.key').read_text()
    client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8787',
                            headers={'Authorization':'Bearer '+agent})
    return app,client,provider,owner


def chat(**kwargs):
    return {'model':'auto','messages':[{'role':'user','content':'Hello'}],'max_tokens':8,**kwargs}


async def test_local_app_discovery_selection_stream_and_totals(tmp_path, config, manifest):
    app,c,provider,owner=setup(tmp_path,config,manifest)
    async with c:
        assert (await c.get('/v1/models')).json()['data'][0]['id']=='auto'
        assert (await c.get('/v1/providers')).json()['providers'][0]['model_id']==manifest.model_id
        r=await c.post('/v1/chat/completions',json=chat(stream=True))
        assert r.status_code==200,r.text
        assert r.text.endswith('data: [DONE]\n\n')
        records=[json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: {')]
        assert ''.join(x['choices'][0]['delta'].get('content','') for x in records)=='This is a lab fixture.'
        state=(await c.get('/admin/state',headers={'Authorization':'Bearer '+owner})).json()
        assert state['received_tokens']==5 and state['received_output_msat']==0
        assert state['active']==0 and provider.state.provider.active==0
    app.state.store.close()
    restarted=create_buyer_app(tmp_path/'buyer',background=False)
    assert restarted.state.store.db.execute('SELECT tokens FROM buyer_totals').fetchone()[0]==5
    restarted.state.store.close()


async def test_agent_cannot_read_owner_state_or_change_policy(tmp_path,config,manifest):
    app,c,provider,owner=setup(tmp_path,config,manifest)
    async with c:
        assert (await c.get('/admin/state')).status_code==401
        assert (await c.put('/admin/settings',json={})).status_code==401
        assert (await c.post('/admin/refresh',json={})).status_code==401
        assert (await c.get('/v1/models',headers={'Authorization':'Bearer '+owner})).status_code==401
        assert (await c.get('/v1/models',headers={'Authorization':''})).status_code==401
        assert (await c.get('/openapi.json')).status_code==404
        for headers in [{'Host':'attacker.example:8787'},{'Origin':'https://attacker.example'}, {'Sec-Fetch-Site':'cross-site'}]:
            assert (await c.get('/v1/models',headers=headers)).status_code==403
        html=await c.get('/')
        assert owner not in html.text
        assert 'frame-ancestors' in html.headers['Content-Security-Policy']
    app.state.store.close()


@pytest.mark.parametrize('override',[{'tools':[]},{'endpoint':'http://internal'}, {'max_tokens':9},
    {'messages':[{'role':'tool','content':'read files'}]}, {'model':'unapproved'}])
async def test_invalid_agent_requests_never_start_supplier(tmp_path,config,manifest,override):
    app,c,provider,owner=setup(tmp_path,config,manifest)
    async with c:
        assert (await c.post('/v1/chat/completions',json=chat(**override))).status_code in (400,404)
    assert not provider.state.store.evidence()['sessions']
    app.state.store.close()


async def test_policy_never_relaxes_and_spending_stays_disabled(tmp_path,config,manifest):
    app,c,provider,owner=setup(tmp_path,config,manifest)
    async with c:
        s=app.state.buyer['settings']
        for change in [{'model_ids':['a'*64]}, {'privacy':'trusted-only','trusted_providers':['a'*64]},
                       {'max_latency_ms':100}, {'assurance':'required'}]:
            app.state.buyer['settings']=BuyerSettings.model_validate({**s.model_dump(),**change})
            assert (await c.post('/v1/chat/completions',json=chat())).status_code in (409,412)
        app.state.buyer['settings']=s
        bad={**s.model_dump(),'max_price_msat':1,'request_limit_msat':8,'daily_limit_msat':8}
        assert (await c.put('/admin/settings',json=bad,headers={'Authorization':'Bearer '+owner})).status_code==400
    assert not provider.state.store.evidence()['sessions']
    app.state.store.close()


async def test_owner_settings_persist_and_active_requests_block_changes(tmp_path,config,manifest):
    app,c,provider,owner=setup(tmp_path,config,manifest)
    settings=app.state.buyer['settings'].model_dump()
    async with c:
        app.state.buyer['active'].add('test')
        r=await c.put('/admin/settings',json=settings,headers={'Authorization':'Bearer '+owner})
        assert r.status_code==409
        app.state.buyer['active'].clear()
        assert (await c.put('/admin/settings',json=settings,headers={'Authorization':'Bearer '+owner})).status_code==200
    app.state.store.close()
    restarted=create_buyer_app(tmp_path/'buyer',background=False)
    assert restarted.state.buyer['settings'].model_ids==[manifest.model_id]
    restarted.state.store.close()


async def test_daily_token_quota_is_durable(tmp_path,config,manifest):
    app,c,provider,owner=setup(tmp_path,config,manifest,daily_output_tokens=8)
    async with c:
        assert (await c.post('/v1/chat/completions',json=chat())).status_code==200
        assert (await c.post('/v1/chat/completions',json=chat())).status_code==429
    app.state.store.close()
    from offence.store import Store
    store=Store(tmp_path/'buyer/discovery.sqlite')
    with pytest.raises(ValueError):store.reserve_gateway('again',8,8)
    store.close()


def test_paid_policy_validation():
    with pytest.raises(ValueError): BuyerSettings(wallet='lnd-mainnet')
    with pytest.raises(ValueError): BuyerSettings(wallet='lnd-mainnet',assurance='seller-claim',max_price_msat=1)
    with pytest.raises(ValueError): BuyerSettings(wallet='lnd-mainnet',assurance='seller-claim',max_price_msat=1,
        request_limit_msat=100,daily_limit_msat=1000,fee_per_batch_msat=1,total_fee_limit_msat=255)


async def test_wallet_connection_failure_preserves_policy(tmp_path, config, manifest):
    app=create_buyer_app(tmp_path/'buyer',background=False,wallet_factory=lambda mode: (_ for _ in ()).throw(ValueError('private credential error')))
    owner=(tmp_path/'buyer/owner.key').read_text()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8787',headers={'Authorization':'Bearer '+owner}) as c:
        r=await c.put('/admin/settings',json=BuyerSettings(wallet='lnd-mainnet',assurance='seller-claim').model_dump())
        assert r.status_code==412 and 'private credential' not in r.text
        assert app.state.buyer['settings'].wallet=='disabled'
    app.state.store.close()


def test_paid_price_network_and_latency_filters(tmp_path,config,manifest):
    from offence.store import Store
    store=Store(tmp_path/'routing.sqlite')
    s=BuyerSettings(seeds=[],model_ids=[manifest.model_id],approved_origins=['http://provider'],strategy='cheapest', privacy='any', allow_unknown_suppliers=True)
    now=int(time.time()); ids=[]
    for price in [10,20]:
        identity=Identity();ids.append(identity.public)
        store.ingest(identity.sign({'type':'advertisement','network':'offence-v1','issued':now,'expires':now+120,
            'sequence':1,'endpoint':'http://provider','offer':{**config.offer.model_dump(),'text_chat':True,'output_msat_per_token':price}}))
    router=Router(s.node_config(),store)
    assert router.select('auto',8,max_price_msat=20,network='offence-v1').provider==ids[0]
    for kwargs in [dict(max_price_msat=9),dict(max_price_msat=20,network='offence-lab-v1'),dict(max_price_msat=20,max_latency_ms=200)]:
        with pytest.raises(ValueError):router.select('auto',8,**kwargs)
    store.record_route(ids[1],manifest.model_id,100,True)
    assert router.select('auto',8,max_price_msat=20,max_latency_ms=200).provider==ids[1]
    store.close()

async def test_interrupted_paid_stream_reports_partial_delivery_and_retains_totals(tmp_path,config,manifest):
    app,c,provider,owner=setup(tmp_path,config,manifest)
    async def interrupted(*args,**kwargs):
        yield {'type':'delta','text':'partial','token_count':2,'amount_msat':100,'session':'test'}
        raise httpx.ReadTimeout('private backend detail')
    app.state.buyer['client'].run=interrupted
    async with c:
        r=await c.post('/v1/chat/completions',json=chat(stream=True))
        assert r.status_code==200
        assert 'partial' in r.text and 'provider_error' in r.text
        assert '[DONE]' not in r.text and 'private backend detail' not in r.text
        assert not app.state.buyer['active']
        state=(await c.get('/admin/state',headers={'Authorization':'Bearer '+owner})).json()
        assert state['received_tokens']==2 and state['received_output_msat']==100
    app.state.store.close()


async def test_unconfigured_wallet_refuses_paid_quote_before_acceptance(tmp_path,config,manifest):
    from offence.client import Buyer
    config.offer.output_msat_per_token=5
    app=create_app(tmp_path/'provider',config,backend=ChatFixture(),background=False)
    app.state.provider.wallet=object()
    buyer=Buyer(Identity(),tmp_path/'buyer')
    with pytest.raises(ValueError,match='before acceptance'):
        async for _ in buyer.run('http://provider',app.state.provider.identity.public,manifest.model_id,'hi',8,40,
            allow_lab=True,transport=httpx.ASGITransport(app=app)):
            pass
    assert not app.state.provider.active
    assert app.state.store.evidence()['sessions']=={}

async def test_mainnet_mode_budget_and_price_pin_with_simulated_wallet(tmp_path, manifest):
    # ASGI buffers streams, so settlement is auto-confirmed here. The separate
    # real LND smoke covers interactive pay-before-next-batch behavior.
    import hashlib
    from offence.models import Config,Offer,Pricing
    class Wallet:
        network='mainnet'
        def __init__(self):self.invoices={};self.payments=0
        async def check_network(self):pass
        async def invoice(self,key,amount,commitment,expiry):
            ph=hashlib.sha256(key).hexdigest();self.invoices[ph]=(key,amount,commitment);return 'test:'+ph
        async def wait(self,*args):return True
        async def settled(self,*args):return self.payments > 0
        async def pay(self,invoice,ph,amount,commitment,fee):
            key,expected,hashed=self.invoices[ph]
            assert amount==expected and commitment==hashed
            self.payments+=1;return key
    wallet=Wallet()
    cfg=Config(backend='vllm',backend_url='http://fixture',backend_model='fixture',allow_seller_claim=True,
        lightning='lnd-mainnet', pricing=Pricing(mode='sats-per-token', sats_per_token='1', usd_per_btc='125000'),
        offer=Offer(manifest=manifest,output_msat_per_token=1000,batch_tokens=2))
    supplier=create_app(tmp_path/'provider',cfg,wallet=wallet,backend=ChatFixture(),background=False)
    root=tmp_path/'buyer';root.mkdir()
    settings=BuyerSettings(seeds=[],approved_origins=['http://provider'],model_ids=[manifest.model_id],
        wallet='lnd-mainnet',assurance='seller-claim',max_price_msat=1000,request_limit_msat=8000,
        daily_limit_msat=8000,max_output_tokens=8,allow_prepaid_compute=True,trusted_providers=[supplier.state.provider.identity.public])
    private_write(root/'buyer-settings.json',settings.model_dump_json())
    app=create_buyer_app(root,background=False,transport=httpx.ASGITransport(app=supplier),wallet_factory=lambda _:wallet)
    now=int(time.time())
    app.state.store.ingest(supplier.state.provider.identity.sign({'type':'advertisement','network':'offence-v1',
        'issued':now,'expires':now+120,'sequence':1,'endpoint':'http://provider',
        'offer':{**cfg.offer.model_dump(),'text_chat':True}}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8787',
             headers={'Authorization':'Bearer '+(root/'agent.key').read_text()}) as c:
            cfg.offer.output_msat_per_token=2000
            cfg.offer.output_msat_per_token_exact="2000"
            assert (await c.post('/v1/chat/completions',json=chat())).status_code==502
            assert wallet.payments==0
            # Remove the local failure cooldown before testing a corrected offer.
            with app.state.store.db:app.state.store.db.execute('DELETE FROM routing_stats')
            cfg.offer.output_msat_per_token=1000
            cfg.offer.output_msat_per_token_exact="1000"
            r=await c.post('/v1/chat/completions',json=chat())
            assert r.status_code==200,r.text
            assert r.json()['offence']['spent_msat']==5000 and wallet.payments==1
            r=await c.post('/v1/chat/completions',json=chat())
            assert r.status_code==502 and wallet.payments==1
