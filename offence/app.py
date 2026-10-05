import asyncio
from .pricing import rate
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import sqlite3
import time

from fastapi import FastAPI, HTTPException, Request as HttpRequest
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .backend import Fixture, LlamaCpp, Vllm
from .crypto import Identity, canonical, verify
from .discovery import Discovery, MAX_WIRE
from .lightning import LndRegtest, LndMainnet
from .models import Config
from .protocol import ProofUnavailable, Provider
from .store import Store


async def bounded_body(request):
    cached = getattr(request.state, 'offence_verified_body', None)
    if cached is not None:
        return cached
    data = bytearray()
    try:
        async with asyncio.timeout(10):
            async for block in request.stream():
                data.extend(block)
                if len(data) > MAX_WIRE:
                    raise HTTPException(413, "Message too large")
    except TimeoutError:
        raise HTTPException(408, "Request body deadline exceeded")
    try:
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("Expected object")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(400, "Invalid JSON message") from exc


def create_app(data_dir=None, config=None, wallet=None, backend=None, background=True):
    data_dir = Path(data_dir or os.getenv("OFFENCE_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = config or Config.load(data_dir / "config.json")
    identity = Identity.load(data_dir / "identity.key")
    store = Store(data_dir / "offence.sqlite", config.max_peers, config.max_storage_mb)
    if config.offer:
        store.register_model(config.offer.manifest)
    store.local_signer = identity.public
    discovery = Discovery(config, identity, store)
    if wallet is None and config.lightning == "lnd-regtest":
        wallet = LndRegtest.from_env()
    if wallet is None and config.lightning == "lnd-mainnet":
        wallet = LndMainnet.from_env()
    if wallet is None and config.lightning == 'strike':
        from .strike import Strike
        wallet = Strike.from_env(config.strike_address)
    if backend is None:
        if config.backend == "fixture":
            backend = Fixture()
        elif config.backend == "llamacpp" and config.offer:
            backend = LlamaCpp(config.backend_url, config.offer.manifest.context_tokens)
        elif config.backend == "vllm" and config.offer:
            backend = Vllm(config.backend_url, config.backend_model, config.offer.manifest.context_tokens)
    provider = Provider(config, identity, store, backend, wallet)

    async def reconcile_provider():
        while True:
            try:
                await provider.reconcile()
            except Exception:
                # Wallet unavailability must not be confused with failed settlement.
                pass
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(app):
        if wallet:
            await wallet.check_network()
        task = asyncio.create_task(discovery.run()) if background else None
        recovery = asyncio.create_task(reconcile_provider()) if background and wallet else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if recovery:
                recovery.cancel()
                with suppress(asyncio.CancelledError):
                    await recovery
            await app.state.jobs.shutdown()
            store.close()

    app = FastAPI(title="Offence Lab", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    from .limits import ResourceLimits
    app.state.provider, app.state.discovery, app.state.store = provider, discovery, store
    buckets = OrderedDict()

    @app.middleware("http")
    async def limits(request, call_next):
        if request.method in {"GET", "POST", "HEAD"}:
            host = request.client.host if request.client else "unknown"
            now = time.monotonic()
            # Quote/discovery floods must not consume delivery and recovery credits
            # for every buyer sharing a reverse proxy or Tor origin.
            category = 'quotes' if request.url.path in {'/v1/quote','/v1/credit/fund'} else ('gossip' if request.url.path == '/v1/gossip' else 'delivery')
            principal = host
            signed_paths = {'/v1/quote','/v1/stream','/v1/gossip','/v1/receipt',
                            '/v1/recover','/v1/release-key','/v1/credit/fund','/v1/credit/status'}
            if request.method == 'POST' and request.url.path in signed_paths:
                try:
                    envelope = await bounded_body(request)
                    verify(envelope)
                except HTTPException as exc:
                    return JSONResponse({'detail':exc.detail}, status_code=exc.status_code)
                except ValueError:
                    return JSONResponse({'detail':'Invalid signed request'}, status_code=400)
                request.state.offence_verified_body = envelope
                principal = envelope['signer']
            # Authenticated buyer identities do not share a proxy-IP quota.
            # Sybil resistance comes from prepaid GPU admission, not this bucket.
            bucket = (principal, category)
            capacity, refill = (256.0, 128) if principal != host and category == 'delivery' else (30.0, 2)
            credits, last = buckets.pop(bucket, (capacity, now))
            credits = min(capacity, credits + (now - last) * refill)
            buckets[bucket] = (max(0, credits - 1), now)
            if len(buckets) > 4096:
                buckets.popitem(last=False)
            if credits < 1:
                return JSONResponse({"detail": "Request rate exceeded"}, status_code=429)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        code = 412 if isinstance(exc, ProofUnavailable) else 400
        # Pydantic errors may include the submitted prompt or private backend details.
        message = "Execution proof unavailable" if code == 412 else "Request rejected"
        return JSONResponse({"detail": message}, status_code=code)

    @app.exception_handler(sqlite3.IntegrityError)
    async def duplicate(request, exc):
        return JSONResponse({"detail": "Request already used"}, status_code=409)

    def payment_status():
        return "configured" if config.lightning in {"lnd-mainnet", "strike"} and config.allow_seller_claim else "blocked"

    @app.get("/health")
    async def health():
        return {"status": "ok", "network": config.network, "production_payments": payment_status(),
                "execution_proof": "unavailable"}

    @app.get("/v1/status")
    async def status():
        return {"identity": identity.public, "network": config.network,
                "production_payments": payment_status(), "execution_proof": "unavailable",
                "backend": config.backend, "endpoint": config.endpoint,
                "wallet": config.lightning, "receiving_address": config.strike_address if config.lightning == "strike" else None,
                "settlement_mode": getattr(wallet, "settlement_mode", "preimage-v1"),
                "known_peers": store.peer_count(identity.public), "active_sessions": provider.active,
                "discovery_error": discovery.last_error, "token_totals": store.token_totals()}

    @app.get("/v1/providers")
    async def providers(model_id: str | None = None, max_msat: int | None = None, min_context: int = 0):
        ads, model_ids = [], {}
        for envelope in store.peers(config.max_peers):
            offer = envelope["body"]["offer"]
            if offer:
                from .models import Manifest
                mid = Manifest.model_validate(offer["manifest"]).model_id
                model_ids[envelope["signer"]] = mid
                if model_id and model_id != mid:
                    continue
                if max_msat is not None and rate(offer) > max_msat:
                    continue
                if offer["manifest"]["context_tokens"] < min_context:
                    continue
            elif model_id or max_msat is not None or min_context:
                continue
            ads.append(envelope)
        return {"providers": ads, "model_ids": model_ids}

    @app.post("/v1/gossip")
    async def gossip(request: HttpRequest):
        body = verify(await bounded_body(request))
        if body.get("type") != "gossip" or not isinstance(body.get("nonce"), str) or len(body["nonce"]) > 128:
            raise ValueError("Invalid gossip request")
        ads = body.get("ads")
        if not isinstance(ads, list) or len(ads) > 32:
            raise ValueError("Too many advertisements")
        for ad in ads:
            try:
                store.ingest(ad)
            except (ValueError, TypeError, KeyError):
                continue
        own = discovery.advertisement()
        # Always return our identity record to a bootstrap connection.
        others = [a for a in store.peers(16) if a["signer"] != identity.public]
        return identity.sign({"type": "gossip-reply", "nonce": body["nonce"],
                              "ads": ([own] if own else []) + others})

    @app.post("/v1/quote")
    async def quote(request: HttpRequest):
        return provider.quote(await bounded_body(request))

    @app.post('/v1/credit/fund')
    async def fund_credit(request: HttpRequest):
        return await provider.fund_credit(await bounded_body(request))

    @app.post('/v1/credit/status')
    async def credit_status(request: HttpRequest):
        return await provider.credit_status(await bounded_body(request))

    @app.post("/v1/stream")
    async def stream(request: HttpRequest):
        quote, req = provider.accept(await bounded_body(request))

        async def output():
            from contextlib import aclosing
            async with aclosing(provider.stream(quote, req)) as source:
                async for message in source:
                    yield canonical(message) + b"\n"
        from .gateway import ManagedStream
        return ManagedStream(output(), lambda: provider.release(quote["body"]["session"]),
                             media_type="application/x-ndjson")

    @app.post('/v1/release-key')
    async def release_key(request: HttpRequest):
        return await provider.release_key(await bounded_body(request))

    @app.post("/v1/receipt")
    async def receipt(request: HttpRequest):
        store.receipt(await bounded_body(request))
        return {"accepted": True}

    @app.post("/v1/recover")
    async def recover(request: HttpRequest):
        envelope = await bounded_body(request)
        body = verify(envelope)
        if body.get("type") != "recover" or type(body.get("issued")) is not int or abs(body["issued"] - time.time()) > 60:
            raise ValueError("Invalid recovery request")
        records = store.recover(body["session"], envelope["signer"], body.get("offset"))
        return identity.sign({"type": "recovery", "session": body["session"], **records,
                              "evidence": []})

    @app.get("/v1/track-record")
    async def track_record():
        return identity.sign({"type": "track-record", "issued": int(time.time()), **store.evidence()})

    @app.get("/v1/evidence")
    async def evidence():
        if not config.share_receipts:
            raise HTTPException(403, "Public receipt sharing is disabled")
        return identity.sign({"type": "evidence", "packages": store.evidence_packages()})

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return (Path(__file__).parent / "dashboard.html").read_text()

    @app.get("/dashboard.js")
    async def dashboard_js():
        from fastapi.responses import Response
        return Response((Path(__file__).parent / "dashboard.js").read_text(), media_type="application/javascript")

    from .gateway import install_gateway
    install_gateway(app, config, identity, store, data_dir / "buyer", bounded_body)
    # Last registered is outermost, including body reads and early rejections.
    app.add_middleware(ResourceLimits)
    return app
