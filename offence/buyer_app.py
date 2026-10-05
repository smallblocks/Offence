"""Loopback buyer application. Owner policy and agent authority are separate."""
import asyncio
from contextlib import asynccontextmanager, aclosing, suppress
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import Field, model_validator

from .app import bounded_body
from .client import Buyer
from .pricing import charge
from .crypto import Identity
from .discovery import Discovery, peer_url
from .gateway import ChatRequest, ManagedStream
from .limits import ResourceLimits
from .models import Config, GatewayConfig, RoutingPolicy, Strict
from .routing import Router
from .store import Store


class BuyerSettings(Strict):
    seeds: list[str] = Field(default_factory=lambda: ['https://offence.ai'], max_length=32)
    approved_origins: list[str] = Field(default_factory=lambda: ['https://offence.ai'], max_length=32)
    model_ids: list[str] = Field(default_factory=list, max_length=32)
    providers: list[str] = Field(default_factory=list, max_length=128)
    trusted_providers: list[str] = Field(default_factory=list, max_length=128)
    strategy: Literal['cheapest', 'fastest', 'preferred-model', 'balanced'] = 'cheapest'
    privacy: Literal['any', 'trusted-only'] = 'trusted-only'
    allow_unknown_suppliers: bool = False
    assurance: Literal['required', 'seller-claim', 'lab-unverified'] = 'required'
    wallet: Literal['disabled', 'lnd-regtest', 'lnd-mainnet', 'nwc-mainnet'] = 'disabled'
    wallet_managed_fees: bool = False
    allow_provider_key_release: bool = False
    allow_prepaid_compute: bool = False
    max_price_msat: int = Field(default=0, ge=0, le=10**9)
    request_limit_msat: int = Field(default=0, ge=0, le=10**12)
    daily_limit_msat: int = Field(default=0, ge=0, le=10**12)
    fee_per_batch_msat: int = Field(default=0, ge=0, le=10**9)
    total_fee_limit_msat: int = Field(default=0, ge=0, le=10**12)
    max_output_tokens: int = Field(default=256, ge=1, le=32768)
    daily_output_tokens: int = Field(default=10000, ge=1, le=10000000)
    min_context_tokens: int = Field(default=1, ge=1, le=10000000)
    max_latency_ms: int | None = Field(default=None, ge=1, le=3600000)
    request_deadline_s: int = Field(default=120, ge=1, le=3600)
    max_concurrent: int = Field(default=2, ge=1, le=8)
    tor_proxy: str | None = Field(default=None, max_length=512)

    @model_validator(mode='after')
    def coherent(self):
        # Validate identity lists even before a model is selected.
        RoutingPolicy(alias='auto', model_ids=self.model_ids or ['0'*64], providers=self.providers,
                      trusted_providers=self.trusted_providers, privacy=self.privacy if self.trusted_providers else 'any')
        for origin in self.approved_origins:
            peer_url(origin, self.approved_origins)
        for seed in self.seeds:
            peer_url(seed, self.approved_origins)
        if self.tor_proxy:
            from urllib.parse import urlsplit
            p = urlsplit(self.tor_proxy)
            if p.scheme not in ('socks5', 'socks5h') or not p.hostname or p.username or p.password or p.path or p.query or p.fragment:
                raise ValueError('Expected SOCKS origin without credentials')
        if self.wallet in ('lnd-mainnet', 'nwc-mainnet') and self.assurance != 'seller-claim':
            raise ValueError('Mainnet requires explicit seller-claim acceptance')
        if self.wallet == 'nwc-mainnet' and (not self.wallet_managed_fees or self.fee_per_batch_msat or self.total_fee_limit_msat):
            raise ValueError('NWC requires explicit wallet-managed fees with no local fee-cap claim')
        if self.wallet == 'lnd-regtest' and self.assurance != 'lab-unverified':
            raise ValueError('Regtest requires explicit lab acceptance')
        if self.wallet == 'disabled' and any((self.max_price_msat, self.request_limit_msat, self.daily_limit_msat,
                                             self.fee_per_batch_msat, self.total_fee_limit_msat)):
            raise ValueError('Configure a wallet before enabling spending')
        if self.max_price_msat and (not self.request_limit_msat or not self.daily_limit_msat):
            raise ValueError('Paid inference requires request and daily limits')
        if self.max_price_msat and (1 if self.allow_prepaid_compute else self.max_output_tokens) * self.fee_per_batch_msat > self.total_fee_limit_msat:
            raise ValueError('Fee budget must cover the selected payment mode')
        if self.request_limit_msat + self.total_fee_limit_msat > self.daily_limit_msat:
            raise ValueError('Daily limit must cover a request and its fee reservation')
        return self

    @property
    def network(self):
        return 'offence-v1' if self.wallet in ('lnd-mainnet', 'nwc-mainnet') else 'offence-lab-v1'

    def node_config(self):
        policies = [RoutingPolicy(alias='auto', model_ids=self.model_ids, providers=(self.providers or self.trusted_providers) if not self.allow_unknown_suppliers else self.providers,
            trusted_providers=self.trusted_providers, strategy=self.strategy, privacy=self.privacy,
            max_output_tokens=self.max_output_tokens, min_context_tokens=self.min_context_tokens)] if self.model_ids and (self.privacy != 'trusted-only' or self.trusted_providers) else []
        return Config(seeds=self.seeds, allowed_private_peers=self.approved_origins, tor_proxy=self.tor_proxy,
                      gateway=GatewayConfig(policies=policies))


def private_write(path, text):
    """Atomic replacement with private permissions and durable contents."""
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        if os.name != 'nt':
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def local_key(path):
    if not path.exists():
        private_write(path, secrets.token_urlsafe(32))
    value = path.read_text().strip()
    if len(value) < 32:
        raise ValueError('Invalid local access key')
    return value


def create_buyer_app(directory, port=8787, background=True, transport=None, wallet_factory=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / 'purchases').mkdir(exist_ok=True, mode=0o700)
    settings_path = directory / 'buyer-settings.json'
    settings = BuyerSettings.model_validate_json(settings_path.read_text()) if settings_path.exists() else BuyerSettings()
    owner_key, agent_key = local_key(directory / 'owner.key'), local_key(directory / 'agent.key')
    identity = Identity.load(directory / 'identity.key')
    store = Store(directory / 'discovery.sqlite')
    with store.db:
        store.db.execute('CREATE TABLE IF NOT EXISTS buyer_totals (id INTEGER PRIMARY KEY, tokens INTEGER NOT NULL, msat INTEGER NOT NULL)')
        store.db.execute('INSERT OR IGNORE INTO buyer_totals VALUES (1,0,0)')
    origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}

    def make_wallet(mode, connection=None):
        if mode == 'disabled':
            return None
        if wallet_factory:
            return wallet_factory(mode)
        if mode == 'nwc-mainnet':
            from .nwc import NwcWallet
            return NwcWallet(connection if connection is not None else (directory / 'wallet.nwc').read_text())
        from .lightning import LndMainnet, LndRegtest
        return (LndMainnet if mode == 'lnd-mainnet' else LndRegtest).from_env()

    state = {'settings': settings, 'wallet': None, 'wallet_ready': settings.wallet == 'disabled',
             'discovery_error': None, 'active': set(), 'refresh_lock': asyncio.Lock(),
             'client': Buyer(identity, directory / 'purchases'), 'changing': False}

    async def close_wallet(wallet):
        if wallet and hasattr(wallet, 'close'):
            with suppress(Exception):
                await wallet.close()

    def unresolved():
        return any(not p.with_name(p.name.removesuffix('.attempt.json')+'.payment.json').exists()
                   and not p.with_name(p.name.removesuffix('.attempt.json')+'.failed.json').exists()
                   for p in (directory / 'purchases').glob('*.attempt.json'))

    async def refresh():
        async with state['refresh_lock']:
            cfg = state['settings'].node_config()
            store.protected_signers = set(state['settings'].providers) | set(state['settings'].trusted_providers)
            discovery = Discovery(cfg, identity, store)
            await discovery.tick()
            state['discovery_error'] = discovery.last_error

    async def worker():
        while True:
            try:
                await refresh()
            except (ValueError, OSError):
                state['discovery_error'] = 'Discovery unavailable'
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(app):
        from .spending import recover_stopped
        recover_stopped(directory / 'purchases')
        try:
            wallet = make_wallet(state['settings'].wallet)
            if wallet:
                async with asyncio.timeout(45):
                    await wallet.check_network()
                    await Buyer(identity, directory / 'purchases', wallet).reconcile_payments()
                    from .spending import recover_stopped
                    recover_stopped(directory / 'purchases')
            state['wallet'], state['wallet_ready'] = wallet, True
            state['client'] = Buyer(identity, directory / 'purchases', wallet)
        except Exception:
            state['wallet_ready'] = False
            await close_wallet(locals().get('wallet'))
        task = asyncio.create_task(worker()) if background else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            store.close()
            await close_wallet(state['wallet'])

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(ResourceLimits, max_active=16)
    app.state.buyer, app.state.store = state, store

    @app.middleware('http')
    async def local_only(request, call_next):
        # Loopback bind plus exact Host/Origin checks prevent DNS rebinding and web drive-by requests.
        if ('http://' + request.headers.get('host', '')) not in origins:
            return JSONResponse({'detail': 'Loopback host required'}, 403)
        origin = request.headers.get('origin')
        if (origin and origin not in origins) or request.headers.get('sec-fetch-site') == 'cross-site':
            return JSONResponse({'detail': 'Foreign browser origin rejected'}, 403)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def auth(request, owner=False):
        expected = owner_key if owner else agent_key
        if not hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer '+expected).encode()):
            raise HTTPException(401, 'Owner authorization required' if owner else 'Agent authorization required')

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({'detail': 'Invalid settings or request; check the documented limits'}, 400)

    @app.get('/', response_class=HTMLResponse)
    async def home():
        return (Path(__file__).parent / 'buyer.html').read_text()

    @app.get('/buyer.js')
    async def js():
        return Response((Path(__file__).parent / 'buyer.js').read_text(), media_type='application/javascript')

    @app.get('/buyer.css')
    async def css():
        return Response((Path(__file__).parent / 'buyer.css').read_text(), media_type='text/css')

    @app.get('/admin/state')
    async def admin_state(request: Request):
        auth(request, True)
        totals = store.db.execute('SELECT tokens,msat FROM buyer_totals WHERE id=1').fetchone()
        return {'settings': state['settings'].model_dump(), 'agent_key': agent_key,
                'base_url': f'http://127.0.0.1:{port}/v1', 'wallet_ready': state['wallet_ready'],
                'nwc_saved': (directory / 'wallet.nwc').exists(),
                'supplier_credit':state['client'].credit_balances(),
                'active': len(state['active']), 'known_peers': store.peer_count(identity.public),
                'discovery_error': state['discovery_error'],
                'received_tokens': totals[0], 'received_output_msat': totals[1],
                'execution_verified': False}

    @app.put('/admin/settings')
    async def update_settings(request: Request):
        auth(request, True)
        if state['active'] or state['changing']:
            raise HTTPException(409, 'Wait for active purchases before changing policy')
        new = BuyerSettings.model_validate(await bounded_body(request))
        if state['active'] or state['changing']:
            raise HTTPException(409, 'Buyer is busy')
        state['changing'] = True
        wallet = None
        try:
            wallet = make_wallet(new.wallet)
            if wallet:
                for path in (directory / 'purchases').glob('*.attempt.json'):
                    if (path.with_name(path.name.removesuffix('.attempt.json')+'.payment.json').exists()
                            or path.with_name(path.name.removesuffix('.attempt.json')+'.failed.json').exists()):
                        continue
                    attempt = json.loads(path.read_text())
                    if (attempt.get('network', 'regtest') != wallet.network
                            or attempt.get('wallet_identity') != getattr(wallet, 'identity', None)):
                        raise ValueError('Pending payments belong to another wallet')
                async with asyncio.timeout(45):
                    await wallet.check_network()
                    await Buyer(identity, directory / 'purchases', wallet).reconcile_payments()
                    from .spending import recover_stopped
                    recover_stopped(directory / 'purchases')
            old = state['wallet']
            private_write(settings_path, new.model_dump_json(indent=2))
            state['settings'], state['wallet'], state['wallet_ready'] = new, wallet, True
            state['client'] = Buyer(identity, directory / 'purchases', wallet)
            await close_wallet(old)
        except Exception:
            if wallet is not state['wallet']:
                await close_wallet(wallet)
            raise HTTPException(412, 'Wallet unavailable, pending payments belong to another wallet, or settings could not be saved') from None
        finally:
            state['changing'] = False
        return {'saved': True}

    @app.post('/admin/wallet/connect')
    async def connect_wallet(request: Request):
        auth(request, True)
        body = await bounded_body(request)
        if not isinstance(body, dict) or set(body) != {'connection'}:
            raise HTTPException(400, 'Supply a wallet connection')
        from .nwc import validate_connection
        connection = validate_connection(body['connection'])
        if state['active'] or state['changing']:
            raise HTTPException(409, 'Wait for active purchases before connecting a wallet')
        path = directory / 'wallet.nwc'
        if unresolved() and (not path.exists() or path.read_text() != connection):
            raise HTTPException(409, 'Recover pending payments before replacing this wallet')
        if state['settings'].wallet != 'disabled':
            raise HTTPException(409, 'Pause payments before replacing the wallet')
        state['changing'] = True
        wallet = None
        try:
            wallet = make_wallet('nwc-mainnet', connection)
            async with asyncio.timeout(45):
                await wallet.check_network()
                await Buyer(identity, directory / 'purchases', wallet).reconcile_payments()
                from .spending import recover_stopped
                recover_stopped(directory / 'purchases')
            private_write(path, connection)
        except Exception:
            raise HTTPException(412, 'Connection failed. Check mainnet, get_info, pay_invoice and lookup_invoice permissions') from None
        finally:
            await close_wallet(wallet)
            state['changing'] = False
        return {'connected': True, 'spending_enabled': False}

    @app.post('/admin/wallet/disconnect')
    async def disconnect_wallet(request: Request):
        auth(request, True)
        if state['active'] or state['changing']:
            raise HTTPException(409, 'Wait for active purchases before disconnecting')
        state['changing'] = True
        try:
            new = BuyerSettings.model_validate({**state['settings'].model_dump(), 'wallet': 'disabled',
                'wallet_managed_fees': False, 'max_price_msat': 0, 'request_limit_msat': 0,
                'daily_limit_msat': 0, 'fee_per_batch_msat': 0, 'total_fee_limit_msat': 0})
            private_write(settings_path, new.model_dump_json(indent=2))
            old = state['wallet']
            state['settings'], state['wallet'], state['wallet_ready'] = new, None, True
            state['client'] = Buyer(identity, directory / 'purchases')
            retained = unresolved()
            if not retained:
                (directory / 'wallet.nwc').unlink(missing_ok=True)
            await close_wallet(old)
        finally:
            state['changing'] = False
        return {'disconnected': True, 'credential_retained_for_recovery': retained}

    async def recover_outputs():
        for path in sorted((directory/'purchases').glob('chatcmpl-*.output.json')):
            saved = json.loads(path.read_text())
            session = saved.get('supplier_session')
            if saved.get('complete') or not session:
                continue
            credit_path = directory/'purchases'/(session+'.credit.json')
            if not credit_path.exists():
                continue
            credit = json.loads(credit_path.read_text())
            if credit['pending']:
                continue
            # Local routing records, never URLs embedded in supplier output.
            endpoint = peer_url(credit['endpoint'], state['settings'].approved_origins)
            if credit['charged_msat']:
                output = await state['client'].recover_prepaid_output(endpoint, credit['provider'], session,
                    transport=transport, tor_proxy=state['settings'].tor_proxy if '.onion' in endpoint else None)
                added_tokens = max(0,output['output_tokens']-saved['output_tokens'])
                added_msat = max(0,output['spent_msat']-saved['spent_msat'])
                saved.update(output)
                # Persist output first. Totals are informational, never a payment
                # authorization; a crash here must not discard recovered text.
                saved['compute_charge_msat'] = credit['charged_msat']
                state['client'].save(path.name, saved)
                with store.db:
                    store.db.execute('UPDATE buyer_totals SET tokens=tokens+?,msat=msat+? WHERE id=1',
                                     (added_tokens,added_msat))
            else:
                saved['compute_charge_msat'] = 0
                state['client'].save(path.name,saved)

    @app.post('/admin/payments/recover')
    async def recover_payments(request: Request):
        auth(request, True)
        if state['active'] or state['changing']:
            raise HTTPException(409, 'Wait for active purchases before recovery')
        if not state['wallet'] or not state['wallet_ready']:
            raise HTTPException(412, 'Connect the original wallet before recovery')
        state['changing'] = True
        try:
            async with asyncio.timeout(45):
                results = await state['client'].reconcile_payments()
                credits = await state['client'].recover_credits(state['settings'].approved_origins,
                    transport=transport, tor_proxy=state['settings'].tor_proxy)
                await recover_outputs()
            from .spending import recover_stopped
            recover_stopped(directory / 'purchases')
            return {'payments': results, 'credits':credits, 'payments_sent': 0}
        except Exception:
            raise HTTPException(502, 'Recovery incomplete; unknown payments remain reserved') from None
        finally:
            state['changing'] = False

    @app.post('/admin/refresh')
    async def refresh_now(request: Request):
        auth(request, True)
        if state['refresh_lock'].locked():
            raise HTTPException(409, 'Discovery is already running')
        await refresh()
        return {'known_peers': store.peer_count(identity.public), 'error': state['discovery_error']}

    def offers():
        rows = []
        for envelope in store.peers(512):
            from .models import Advertisement
            ad = Advertisement.model_validate(envelope['body'])
            if not ad.offer or ad.expires <= time.time():
                continue
            offer = ad.offer
            rows.append({'provider': envelope['signer'], 'endpoint': ad.endpoint, 'network': ad.network,
                'model_id': offer.manifest.model_id, 'name': offer.manifest.name,
                'context_tokens': offer.manifest.context_tokens, 'output_msat_per_token': offer.output_msat_per_token,
                'output_msat_per_token_exact': offer.output_msat_per_token_exact,
                'max_output_tokens': offer.max_output_tokens, 'available': offer.available,
                'text_chat': offer.text_chat, 'execution_verified': False,
                'local_observations': store.route_stats(envelope['signer'], offer.manifest.model_id)})
        return rows

    @app.get('/admin/providers')
    async def owner_providers(request: Request):
        auth(request, True)
        return {'providers': offers()}

    @app.get('/v1/providers')
    async def agent_providers(request: Request):
        auth(request)
        return {'providers': offers()}

    @app.get('/v1/models')
    async def models(request: Request):
        auth(request)
        s = state['settings']
        return {'object': 'list', 'data': [{'id': 'auto', 'object': 'model', 'created': 0,
            'owned_by': 'local-buyer-policy', 'offence': {'model_ids': s.model_ids, 'strategy': s.strategy,
            'privacy': s.privacy, 'max_price_msat': s.max_price_msat, 'execution_verified': False,
            'capabilities': ['text-chat', 'streaming']}}] if s.model_ids else []}

    @app.get('/v1/purchases')
    async def purchase_list(request: Request):
        auth(request)
        paths = sorted((directory/'purchases').glob('chatcmpl-*.output.json'),
                       key=lambda path:path.stat().st_mtime, reverse=True)[:128]
        return {'purchases':[{'id':json.loads(path.read_text())['id']} for path in paths]}

    @app.get('/v1/purchases/{request_id}')
    async def purchase_output(request_id: str, request: Request):
        auth(request)
        import re
        if not re.fullmatch(r'chatcmpl-[0-9a-f]{32}', request_id):
            raise HTTPException(404, 'Unknown purchase')
        path = directory / 'purchases' / (request_id + '.output.json')
        if not path.exists():
            raise HTTPException(404, 'Unknown purchase')
        return json.loads(path.read_text())

    @app.post('/v1/chat/completions')
    async def chat(request: Request):
        auth(request)
        req = ChatRequest.model_validate(await bounded_body(request))
        if state['changing']:
            raise HTTPException(409, 'Owner is changing the wallet or policy')
        s = state['settings']  # Immutable policy reference for this request.
        store.protected_signers = set(s.providers) | set(s.trusted_providers)
        if req.model != 'auto' or not s.model_ids:
            raise HTTPException(404, 'Choose models in the buyer app, then use model auto')
        if s.assurance == 'required':
            raise HTTPException(412, 'Execution proofs unavailable; owner policy refuses inference')
        if not state['wallet_ready']:
            raise HTTPException(412, 'Wallet not ready; save settings after restoring the wallet')
        if len(state['active']) >= s.max_concurrent:
            raise HTTPException(429, 'Buyer concurrency limit reached')
        if sum(len(m.content.encode()) for m in req.messages) > 16384 or req.max_tokens > s.max_output_tokens:
            raise HTTPException(400, 'Request exceeds owner context or output limit')
        if not s.allow_unknown_suppliers and not (s.providers or s.trusted_providers):
            raise HTTPException(412, 'Approve supplier keys independently before sending prompts')
        router = Router(s.node_config(), store)
        try:
            route = router.select('auto', req.max_tokens, max_price_msat=s.max_price_msat,
                                  network=s.network, max_latency_ms=s.max_latency_ms)
        except ValueError:
            raise HTTPException(409, 'No supplier satisfies the saved policy; no prompt sent')
        # Bind the ceiling to the selected advertised price, not just a larger owner cap.
        raw = store.db.execute('SELECT envelope FROM peers WHERE signer=?', (route.provider,)).fetchone()[0]
        advertised_offer = json.loads(raw)['body']['offer']
        amount_limit = charge(advertised_offer, req.max_tokens)
        if amount_limit > s.request_limit_msat:
            raise HTTPException(412, 'Requested output exceeds the per-request spending cap')
        rid = 'chatcmpl-' + secrets.token_hex(16)
        try:
            store.reserve_gateway(rid, req.max_tokens, s.daily_output_tokens)
        except ValueError:
            raise HTTPException(429, 'Daily token reservation limit reached')
        state['active'].add(rid)
        buyer = state['client']
        quoted = {}
        source = buyer.run(route.endpoint, route.provider, route.model_id, '', req.max_tokens, amount_limit,
            allow_lab=s.assurance == 'lab-unverified', assurance=s.assurance, transport=transport,
            tor_proxy=s.tor_proxy if '.onion' in route.endpoint else None, detailed=True,
            messages=[m.model_dump() for m in req.messages], fee_limit_msat=s.fee_per_batch_msat,
            total_fee_limit_msat=s.total_fee_limit_msat, daily_limit_msat=s.daily_limit_msat,
            allow_provider_key_release=s.allow_provider_key_release,
            allow_prepaid_compute=s.allow_prepaid_compute, on_quote=quoted.update, funding_limit_msat=s.request_limit_msat)
        started, first_ms, succeeded, released = time.monotonic(), None, False, False
        tokens, spent = 0, 0
        received = []
        def release():
            nonlocal released
            if not released:
                released = True
                state['active'].discard(rid)
                # This is a local retry cooldown, not public blame or reputation.
                # Wallet failures also need a pause instead of repeated attempts.
                store.record_route(route.provider, route.model_id, first_ms, succeeded)
        def save_output():
            buyer.save(rid + '.output.json', {'id': rid, 'partial_output': ''.join(received),
                'complete': succeeded, 'provider': route.provider, 'model_id': route.model_id,
                'output_tokens': tokens, 'spent_msat': spent, 'routing_fees_included': False,
                'supplier_session':quoted.get('session'),
                'funding_floor_msat':quoted.get('funding_floor_msat'),
                'funding_max_msat':quoted.get('funding_max_msat'),
                'funding_usd_per_btc':quoted.get('funding_usd_per_btc'),
                'minimum_compute_msat':quoted.get('minimum_compute_msat',0),
                'compute_charge_msat':max(spent,quoted.get('minimum_compute_msat',0)) if succeeded else None})
        async def events():
            nonlocal first_ms, succeeded, tokens, spent
            try:
                async with aclosing(source), asyncio.timeout(s.request_deadline_s):
                    async for event in source:
                        if event['type'] == 'delta':
                            if first_ms is None:
                                first_ms = int((time.monotonic()-started)*1000)
                            received.append(event['text'])
                            tokens += event['token_count']
                            spent += event['amount_msat']
                            with store.db:
                                store.db.execute('UPDATE buyer_totals SET tokens=tokens+?,msat=msat+? WHERE id=1',
                                                 (event['token_count'], event['amount_msat']))
                        else:
                            succeeded = True
                        save_output()
                        yield event
            finally:
                release()
        stream = events()
        try:
            first = await anext(stream)
        except Exception:
            await stream.aclose()
            release()
            save_output()
            return JSONResponse(status_code=502, content={'id':rid, 'partial_output':''.join(received),
                'error':{'type':'incomplete_delivery','message':'Purchase stopped. Deposits and credit reservations require recovery before retrying.'},
                'offence':{'output_tokens':tokens,'spent_msat':spent,'compute_charge_msat':None,
                    'minimum_compute_msat':quoted.get('minimum_compute_msat',0),'complete':False}})
        async def all_events():
            yield first
            async for event in stream:
                yield event
        created = int(time.time())
        def result_chunk(delta, finish=None):
            return {'id': rid, 'object': 'chat.completion.chunk', 'created': created, 'model': 'auto',
                    'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
        if req.stream:
            async def output():
                try:
                    yield 'data: '+json.dumps(result_chunk({'role': 'assistant'}))+'\n\n'
                    async for event in all_events():
                        chunk = result_chunk({}, event['finish_reason']) if event['type'] == 'end' else result_chunk({'content': event['text']})
                        yield 'data: '+json.dumps(chunk)+'\n\n'
                    yield 'data: [DONE]\n\n'
                except Exception:
                    yield 'data: {"error":{"type":"provider_error","message":"Delivery interrupted. Received output remains billed; inspect local payment records before retrying."}}\n\n'
                finally:
                    await stream.aclose()
            async def close():
                try:
                    await stream.aclose()
                finally:
                    release()
            return ManagedStream(output(), close, media_type='text/event-stream',
                headers={'X-Offence-Provider': route.provider, 'X-Offence-Model-ID': route.model_id})
        try:
            parts, finish = [], 'stop'
            async with aclosing(stream):
                async for event in all_events():
                    if event['type'] == 'end':
                        finish = event['finish_reason']
                    else:
                        parts.append(event['text'])
            return {'id': rid, 'object': 'chat.completion', 'created': created, 'model': 'auto',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': ''.join(parts)}, 'finish_reason': finish}],
                'offence': {'provider': route.provider, 'model_id': route.model_id, 'execution_verified': False,
                            'output_tokens': tokens, 'spent_msat': spent, 'routing_fees_included': False,
                            'compute_charge_msat':max(spent,quoted.get('minimum_compute_msat',0)),
                            'minimum_compute_msat':quoted.get('minimum_compute_msat',0)}}
        except Exception:
            return JSONResponse(status_code=502, content={
                'id': rid, 'error': {'type': 'incomplete_delivery',
                    'message': 'Delivery interrupted. Do not automatically retry billed output.'},
                'partial_output': ''.join(received),
                'offence': {'provider': route.provider, 'model_id': route.model_id,
                    'complete': False, 'output_tokens': tokens, 'spent_msat': spent,
                    'compute_charge_msat':None, 'minimum_compute_msat':quoted.get('minimum_compute_msat',0),
                    'routing_fees_included': False}})
        finally:
            release()
    return app
