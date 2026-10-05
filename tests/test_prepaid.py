"""Prepaid tests use an in-memory wallet, never a Lightning network."""
import hashlib
import json
import time

import httpx
import pytest

from conftest import request
from offence.app import create_app
from offence.client import Buyer
from offence.crypto import Identity, digest, verify
from offence.spending import recover_stopped


class Wallet:
    network = 'regtest'
    def __init__(self):
        self.invoices = {}
        self.paid = set()
        self.payments = 0
    async def invoice(self, key, amount, commitment, expiry):
        ph = hashlib.sha256(key).hexdigest()
        self.invoices[ph] = (key, amount, commitment)
        return 'sim:'+ph
    async def pay(self, invoice, ph, amount, commitment, fee):
        key, expected, hashed = self.invoices[ph]
        assert amount == expected and hashed == commitment and invoice == 'sim:'+ph
        self.paid.add(ph)
        self.payments += 1
        return key
    async def settled(self, ph, amount):
        return ph in self.paid and self.invoices[ph][1] == amount
    async def track(self, ph, amount, fee):
        if ph not in self.paid: return {'status':'UNKNOWN'}
        return {'status':'SUCCEEDED','preimage':self.invoices[ph][0].hex(),'fee_msat':0}


def setup_prepaid(tmp_path, config, backend=None):
    config.prepaid_compute = True
    config.offer.output_msat_per_token = 10
    wallet = Wallet()
    app = create_app(tmp_path/'provider', config, backend=backend, wallet=wallet, background=False)
    buyer = Buyer(Identity(), tmp_path/'buyer', wallet)
    return app, buyer, wallet


async def purchase(app, buyer, manifest, tokens=8):
    return [x async for x in buyer.run('http://provider', app.state.provider.identity.public,
        manifest.model_id, 'hello', tokens, tokens*10, allow_lab=True, allow_prepaid_compute=True,
        fee_limit_msat=0, total_fee_limit_msat=0, daily_limit_msat=10000, detailed=True,
        transport=httpx.ASGITransport(app=app))]


async def test_deposit_reused_and_compute_only_starts_after_confirmed_credit(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    events = await purchase(app, buyer, manifest)
    assert sum(e.get('amount_msat',0) for e in events) == 50
    assert wallet.payments == 1
    p = app.state.provider
    assert p.credits.balance(buyer.identity.public) == 30
    events = await purchase(app, buyer, manifest, tokens=2)
    assert wallet.payments == 1  # Reuse unused credit, no new invoice payment.
    assert p.credits.balance(buyer.identity.public) == 10
    assert events[-1]['compute_minimum_msat'] == 20


async def test_failed_compute_retains_minimum_and_returns_unused_allowance(tmp_path, config, manifest):
    class Broken:
        async def stream(self, *args):
            raise TimeoutError('GPU unavailable')
            yield
    app, buyer, wallet = setup_prepaid(tmp_path, config, Broken())
    with pytest.raises(ValueError, match='stream failed'):
        await purchase(app, buyer, manifest)
    assert app.state.provider.credits.balance(buyer.identity.public) == 60
    assert wallet.payments == 1
    await buyer.recover_credits(['http://provider'], transport=httpx.ASGITransport(app=app))
    recover_stopped(buyer.directory)
    record = json.loads(next(buyer.directory.glob('*.credit.json')).read_text())
    assert record['charged_msat'] == 20 and not record['pending']


async def test_voucher_replay_is_idempotent_and_another_buyer_cannot_claim(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    await purchase(app, buyer, manifest)
    voucher = json.loads(next(buyer.directory.glob('*.fund.batch.json')).read_text())
    p = app.state.provider
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://provider') as client:
        for _ in range(3):
            result = await buyer.credit_query(client, p.identity.public, voucher=voucher)
            assert result['balance_msat'] == 30
        other = Buyer(Identity(), tmp_path/'other', wallet)
        with pytest.raises(httpx.HTTPStatusError):
            await other.credit_query(client, p.identity.public, voucher=voucher)
    assert p.store.db.execute('SELECT count(*) FROM credit_deposits').fetchone()[0] == 1


def test_unpaid_acceptance_cannot_take_slots_or_work_quota(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    p = app.state.provider
    for _ in range(20):
        identity = Identity()
        req = request(identity, p.identity.public, manifest, max_total_msat=80, allow_prepaid_compute=True)
        q = p.quote(req)
        with pytest.raises(ValueError, match='Confirmed prepaid'):
            p.accept(identity.sign({'type':'accept','session':q['body']['session'],'quote_hash':digest(q),'quote':q,'request':req}))
    assert p.active == 0
    assert p.store.db.execute('SELECT count(*) FROM admission').fetchone()[0] == 0
    assert p.store.evidence()['sessions'] == {}


def test_disconnect_before_stream_still_consumes_minimum_and_restart_returns_unused(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    p = app.state.provider
    p.credits.deposit('test-confirmed-deposit', buyer.identity.public, 80)
    req = request(buyer.identity, p.identity.public, manifest, max_total_msat=80, allow_prepaid_compute=True)
    q = p.quote(req)
    p.accept(buyer.identity.sign({'type':'accept','session':q['body']['session'],'quote_hash':digest(q),'quote':q,'request':req}))
    assert p.credits.balance(buyer.identity.public) == 0
    p.store.close()
    restarted = create_app(tmp_path/'provider', config, wallet=wallet, background=False).state.provider
    assert restarted.credits.balance(buyer.identity.public) == 60
    restarted.release(q['body']['session'])
    assert restarted.credits.balance(buyer.identity.public) == 60


async def test_unsettled_invoice_and_forged_voucher_never_create_credit(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    p = app.state.provider
    req = request(buyer.identity, p.identity.public, manifest, max_total_msat=80, allow_prepaid_compute=True)
    q = p.quote(req)
    voucher = await p.fund_credit(buyer.identity.sign({'type':'fund-credit','quote':q,'issued':int(time.time())}))
    query = {'type':'credit-status','issued':int(time.time()),'nonce':'a'*64,'voucher':voucher}
    result = verify(await p.credit_status(buyer.identity.sign(query)), p.identity.public)
    assert result['balance_msat'] == 0 and p.active == 0
    voucher['body']['deposit']['amount_msat'] = 999999
    with pytest.raises(ValueError):
        await p.credit_status(buyer.identity.sign(query))


async def test_wallet_timeout_does_not_resend_or_start_gpu(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    original = wallet.pay
    async def ambiguous(*args):
        await original(*args)
        raise TimeoutError('Response lost after payment')
    wallet.pay = ambiguous
    with pytest.raises(TimeoutError):
        await purchase(app, buyer, manifest)
    assert app.state.provider.active == 0
    assert app.state.provider.store.evidence()['sessions'] == {}
    assert wallet.payments == 1
    await buyer.reconcile_payments()
    await buyer.recover_credits(['http://provider'],transport=httpx.ASGITransport(app=app))
    recover_stopped(buyer.directory)
    assert app.state.provider.credits.balance(buyer.identity.public) == 80
    assert wallet.payments == 1


async def test_hosted_receiving_wallet_credits_once_and_never_spends(tmp_path, config, manifest):
    from test_hosted_payments import HostedWallet
    config.prepaid_compute = True
    config.offer.output_msat_per_token = 10
    wallet = HostedWallet()
    app = create_app(tmp_path/'provider', config, wallet=wallet, background=False)
    buyer = Buyer(Identity(), tmp_path/'buyer', wallet)
    events = await purchase(app, buyer, manifest)
    assert wallet.payments == 1 and events[-1]['type'] == 'end'
    assert app.state.provider.credits.balance(buyer.identity.public) == 30
    assert app.state.store.token_totals()['served_tokens'] == 5
    assert app.state.store.token_totals()['paid_tokens'] == 5


async def test_public_prepaid_evidence_never_contains_output_keys(tmp_path, config, manifest):
    from offence.crypto import b64
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    await purchase(app, buyer, manifest)
    packages = app.state.store.evidence_packages()
    assert packages
    evidence = json.dumps(packages)
    for path in buyer.directory.glob('*.key.json'):
        key = bytes.fromhex(json.loads(path.read_text())['key'])
        assert b64(key) not in evidence and key.hex() not in evidence
    p = app.state.provider
    batch = packages[0]['batch']
    h = batch['body']['sealed']['header']
    with pytest.raises(ValueError, match='buyer mismatch'):
        await p.release_key(Identity().sign({'type':'prepaid-key','issued':int(time.time()),
            'session':h['session'],'sequence':h['sequence'],'batch_hash':digest(batch)}))


def test_competing_credit_reservations_cannot_overspend_or_burn_failed_admission(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    p = app.state.provider
    p.credits.deposit('confirmed', buyer.identity.public, 80)
    for attempt in range(2):
        req = request(buyer.identity, p.identity.public, manifest, max_total_msat=80, allow_prepaid_compute=True)
        q = p.quote(req)
        acceptance = buyer.identity.sign({'type':'accept','session':q['body']['session'],
            'quote_hash':digest(q),'quote':q,'request':req})
        if attempt == 0:
            p.accept(acceptance)
        else:
            with pytest.raises(ValueError, match='Confirmed prepaid'):
                p.accept(acceptance)
    assert p.active == 1
    assert p.store.db.execute('SELECT count(*) FROM admission').fetchone()[0] == 1
    assert p.store.db.execute('SELECT sum(used) FROM credit_holds').fetchone()[0] == 20


async def test_prepaid_output_recovers_after_key_response_loss_without_payment(tmp_path, config, manifest):
    app, buyer, wallet = setup_prepaid(tmp_path, config)
    original = buyer.fetch_key
    async def lost_key(*args): raise httpx.ReadTimeout('key response lost')
    buyer.fetch_key = lost_key
    with pytest.raises(httpx.ReadTimeout):
        await purchase(app, buyer, manifest)
    buyer.fetch_key = original
    record = json.loads(next(buyer.directory.glob('*.credit.json')).read_text())
    recovered = await buyer.recover_prepaid_output('http://provider', app.state.provider.identity.public,
        record['session'], transport=httpx.ASGITransport(app=app))
    assert recovered['partial_output'] == 'This is a lab fixture.'
    assert recovered['output_tokens'] == 5 and recovered['complete']
    assert wallet.payments == 1
    assert app.state.store.token_totals()['served_tokens'] == 5


async def test_large_prepaid_recovery_uses_bounded_pages(tmp_path, config, manifest):
    class Many:
        async def stream(self, prompt, maximum):
            for i in range(maximum):
                yield {'token_ids':[i], 'text':'x'}
    config.offer.batch_tokens = 1
    config.offer.max_output_tokens = 130
    app, buyer, wallet = setup_prepaid(tmp_path, config, Many())
    # More than 64 batches requires multiple signed recovery pages. Simulate loss
    # before the first key, leaving all output paid and stored at the supplier.
    original = buyer.fetch_key
    async def lost(*args): raise httpx.ReadTimeout('lost')
    buyer.fetch_key = lost
    with pytest.raises(httpx.ReadTimeout):
        await purchase(app, buyer, manifest, tokens=130)
    buyer.fetch_key = original
    record = json.loads(next(buyer.directory.glob('*.credit.json')).read_text())
    first = app.state.store.recover(record['session'],buyer.identity.public,0)
    second = app.state.store.recover(record['session'],buyer.identity.public,first['next_offset'])
    third = app.state.store.recover(record['session'],buyer.identity.public,second['next_offset'])
    assert [len(p['batches']) for p in (first,second,third)] == [64,64,2]
    assert third['next_offset'] is None
    recovered = await buyer.recover_prepaid_output('http://provider', app.state.provider.identity.public,
        record['session'], transport=httpx.ASGITransport(app=app))
    assert recovered['partial_output'] == 'x'*130 and recovered['complete']
    assert wallet.payments == 1
