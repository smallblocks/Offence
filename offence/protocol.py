"""Signed quotes and encrypted incremental delivery. No execution-proof shortcut."""
import asyncio
from contextlib import aclosing
import hashlib
import json
import secrets
import time

from .backend import batches
from .crypto import b64, digest, seal, verify, unb64
from .models import Request
from .pricing import charge, rate, cent_deposit


class ProofUnavailable(ValueError):
    pass


class Provider:
    def __init__(self, config, identity, store, backend, wallet=None):
        self.config, self.identity, self.store = config, identity, store
        self.backend, self.wallet = backend, wallet
        self.pending = {}
        self.running = set()
        self.recovery_offset = 0
        from .credits import Credits
        self.credits = Credits(store)
        self.funding_lock = asyncio.Lock()

    @property
    def active(self):
        return len(self.running)

    def release(self, session):
        self.running.discard(session)
        self.credits.close(session)

    def quote(self, envelope):
        request = Request.model_validate(verify(envelope))
        if abs(request.issued - int(time.time())) > 60:
            raise ValueError("Request timestamp outside window")
        if request.network != self.config.network:
            raise ValueError("Request network mismatch")
        if request.provider != self.identity.public:
            raise ValueError("Wrong provider")
        if request.proof_policy == "required":
            raise ProofUnavailable("Execution proof is not implemented; verified inference is blocked")
        if request.proof_policy == "lab-unverified":
            if not self.config.allow_lab_unverified or self.config.lightning in {"lnd-mainnet", "strike"}:
                raise ProofUnavailable("Lab inference disabled or mainnet selected")
        elif not self.config.allow_seller_claim:
            raise ValueError("Seller-claim inference is disabled")
        offer = self.config.offer
        if not offer or not offer.available or not self.backend:
            raise ValueError("Provider unavailable")
        if offer.manifest.model_id != request.model_id:
            raise ValueError("Model identity mismatch")
        if request.max_output_tokens > offer.max_output_tokens:
            raise ValueError("Output limit exceeds offer")
        if request.messages and not hasattr(self.backend, "stream_chat"):
            raise ValueError("Provider does not support text chat")
        maximum = charge(offer, request.max_output_tokens)
        if rate(offer).denominator != 1 and not request.fractional_billing:
            raise ValueError("Buyer must support cumulative fractional billing")
        if maximum > request.max_total_msat:
            raise ValueError("Price exceeds buyer budget")
        if maximum and self.wallet is None:
            raise ValueError("Paid lab inference requires regtest LND")
        prepaid = maximum and (self.config.prepaid_compute or rate(offer).denominator != 1 or self.config.lightning in {'lnd-mainnet','strike'})
        if prepaid and not request.allow_prepaid_compute:
            raise ValueError('Buyer must accept prepaid compute and its disclosed minimum charge')
        settlement_mode = 'prepaid-v1' if prepaid else (getattr(self.wallet, 'settlement_mode', 'preimage-v1') if maximum else 'preimage-v1')
        if settlement_mode == 'provider-key-v1' and not request.allow_provider_key_release:
            raise ValueError('Buyer must explicitly accept supplier-dependent key recovery')
        now = int(time.time())
        self.pending = {k: v for k, v in self.pending.items() if v[0]["body"]["expires"] > now}
        if self.active >= self.config.max_sessions:
            raise ValueError("Provider capacity reserved")
        session_id = digest(envelope)
        body = {"type": "quote", "network": request.network, "session": session_id,
                "buyer": envelope["signer"], "model_id": request.model_id,
                "request_hash": session_id, "expires": now + 60,
                "max_output_tokens": request.max_output_tokens, "max_total_msat": maximum,
                "output_msat_per_token": offer.output_msat_per_token, "batch_tokens": offer.batch_tokens,
                "generation_deadline_s": offer.generation_deadline_s,
                "payment_timeout_s": offer.payment_timeout_s, "proof": "unavailable",
                "payment_network": getattr(self.wallet, "network", "regtest") if maximum else "free-lab",
                "assurance": request.proof_policy, "settlement_mode": settlement_mode,
                "acceptance": "quote-request-v1"}
        if offer.output_msat_per_token_exact is not None:
            body["output_msat_per_token_exact"] = offer.output_msat_per_token_exact
        if prepaid:
            body.update(minimum_compute_msat=min(maximum, charge(offer, offer.batch_tokens)),
                        credit_balance_msat=self.credits.balance(envelope['signer']))
            if self.config.lightning in {'lnd-mainnet', 'strike'}:
                usd = self.config.pricing.usd_per_btc if self.config.pricing else None
                floor = cent_deposit(usd)
                funding_max = ((max(maximum, floor) + 999) // 1000) * 1000
                if funding_max > request.funding_limit_msat:
                    raise ValueError('One-cent funding exceeds buyer funding limit')
                body.update(funding_floor_msat=floor, funding_max_msat=funding_max, funding_usd_per_btc=usd)
        quote = self.identity.sign(body)
        # Quotes do not reserve GPU slots, hourly work, or durable session rows.
        # Legacy clients use a bounded cache. New clients return the signed quote
        # and original request, so cache churn cannot invalidate their acceptance.
        if len(self.pending) >= 128:
            self.pending.pop(next(iter(self.pending)))
        self.pending[session_id] = (quote, request)
        return quote

    async def fund_credit(self, envelope):
        body = verify(envelope)
        if (set(body) != {'type','quote','issued'} or body['type'] != 'fund-credit'
                or type(body['issued']) is not int or abs(body['issued']-time.time()) > 60):
            raise ValueError('Invalid funding request')
        q = verify(body['quote'], self.identity.public)
        if (q.get('settlement_mode') != 'prepaid-v1' or q.get('buyer') != envelope['signer']
                or q.get('expires',0) <= time.time()):
            raise ValueError('Funding requires a current prepaid quote')
        quote_hash = digest(body['quote'])
        saved = self.credits.funding_result(quote_hash, envelope['signer'])
        if saved is not None:
            return saved
        if self.funding_lock.locked():
            raise ValueError('Invoice service busy')
        amount = max(0, q['max_total_msat']-self.credits.balance(envelope['signer']))
        if amount and q.get('funding_floor_msat'):
            amount = ((max(amount, q['funding_floor_msat']) + 999) // 1000) * 1000
        if amount == 0:
            return self.identity.sign({'type':'credit-ready','buyer':envelope['signer'],
                'quote_hash':digest(body['quote']), 'amount_msat':0})
        if not self.store.storage_available():
            raise ValueError('Credit storage unavailable')
        async with self.funding_lock:
            self.credits.begin_funding(quote_hash, envelope['signer'], self.config.max_requests_per_hour)
            deposit = {'type':'credit-deposit','buyer':envelope['signer'], 'provider':self.identity.public,
                'deposit_id':secrets.token_hex(32), 'amount_msat':amount,
                'quote_hash':digest(body['quote']), 'network':q['payment_network']}
            commitment = digest(deposit)
            if getattr(self.wallet, 'settlement_mode', None) == 'provider-key-v1':
                issued = await self.wallet.create_batch_invoice(amount, commitment, 600)
                invoice, payment_hash, reference = issued['invoice'], issued['payment_hash'], issued['reference']
            else:
                preimage = secrets.token_bytes(32)
                payment_hash = hashlib.sha256(preimage).hexdigest()
                invoice = await self.wallet.invoice(preimage, amount, commitment, 600)
                reference = None
            # Persist the exact voucher for retry before returning. This bounded
            # journal reserves no GPU slots or work quota.
            voucher = self.identity.sign({'type':'credit-invoice','deposit':deposit,
                'commitment':commitment,'invoice':invoice,'payment_hash':payment_hash,
                'reference':reference})
            self.credits.finish_funding(quote_hash, voucher)
            return voucher

    async def credit_status(self, envelope):
        body = verify(envelope)
        if (set(body) - {'type','issued','nonce','voucher','session'}
                or body.get('type') != 'credit-status' or type(body.get('issued')) is not int
                or abs(body['issued']-time.time()) > 60
                or not isinstance(body.get('nonce'), str) or len(body['nonce']) != 64):
            raise ValueError('Invalid credit query')
        voucher = body.get('voucher')
        if voucher:
            v = verify(voucher, self.identity.public)
            d = v['deposit']
            if (v.get('type') != 'credit-invoice' or d['buyer'] != envelope['signer']
                    or d['provider'] != self.identity.public or digest(d) != v['commitment']):
                raise ValueError('Credit voucher buyer mismatch')
            # Never credit from a preimage alone. Confirm the supplier wallet
            # actually received this invoice, including the exact amount.
            previous = self.store.db.execute('SELECT buyer,amount FROM credit_deposits WHERE id=?', (v['payment_hash'],)).fetchone()
            if previous and previous != (envelope['signer'], d['amount_msat']):
                raise ValueError('Conflicting credit voucher')
            if d['network'] != getattr(self.wallet, 'network', 'regtest'):
                raise ValueError('Original receiving network required')
            if not previous:
                if v['reference'] is not None:
                    if getattr(self.wallet,'settlement_mode',None) != 'provider-key-v1':
                        raise ValueError('Original receiving wallet required')
                    settled = await self.wallet.settled_batch(v['reference'], v['payment_hash'], d['amount_msat'])
                else:
                    settled = await self.wallet.settled(v['payment_hash'], d['amount_msat'])
                if settled:
                    self.credits.deposit(v['payment_hash'], envelope['signer'], d['amount_msat'])
        deposit_status = {}
        if voucher:
            deposit_status = {'deposit_payment_hash':v['payment_hash'], 'deposit_credited':bool(self.store.db.execute(
                'SELECT 1 FROM credit_deposits WHERE id=?',(v['payment_hash'],)).fetchone())}
        return self.identity.sign({'type':'credit-status','buyer':envelope['signer'],
            'nonce':body['nonce'], **deposit_status, **self.credits.status(envelope['signer'], body.get('session'))})

    async def reconcile(self):
        if self.wallet is None:
            return
        pending = self.store.unsettled_batches(offset=self.recovery_offset)
        self.recovery_offset = self.recovery_offset + len(pending) if len(pending) == 32 else 0
        for session, seq, envelope in pending:
            sealed = envelope["body"]["sealed"]
            amount = sealed["header"]["amount_msat"]
            if amount and await self.batch_settled(session, seq, envelope["body"]):
                self.store.paid(session, seq)

    async def batch_settled(self, session, seq, body):
        amount = body['sealed']['header']['amount_msat']
        if body.get('settlement_mode') == 'prepaid-v1':
            return True
        if body.get('settlement_mode') != 'provider-key-v1':
            return await self.wallet.settled(body['sealed']['payment_hash'], amount)
        row = self.store.db.execute('SELECT paid FROM batches WHERE session=? AND seq=?', (session,seq)).fetchone()
        if row and row[0]:
            return True
        if getattr(self.wallet, 'settlement_mode', None) != 'provider-key-v1':
            raise ValueError('Original hosted wallet is required for reconciliation')
        saved = self.store.hosted_batch(session, seq)
        return await self.wallet.settled_batch(saved['reference'], body['invoice_payment_hash'], amount)

    async def release_key(self, envelope):
        request = verify(envelope)
        prepaid_request = request.get('type') == 'prepaid-key'
        expected = {'type','session','sequence','batch_hash','issued'} | (set() if prepaid_request else {'payment_preimage'})
        if (set(request) != expected
                or request['type'] not in {'release-key','prepaid-key'} or type(request['issued']) is not int
                or abs(request['issued']-time.time()) > 60 or type(request['sequence']) is not int):
            raise ValueError('Invalid key request')
        row = self.store.db.execute('SELECT s.buyer,b.envelope FROM sessions s JOIN batches b ON b.session=s.id WHERE s.id=? AND b.seq=?',
                                    (request['session'], request['sequence'])).fetchone()
        if not row or row[0] != envelope['signer']:
            raise ValueError('Key request buyer mismatch')
        batch = json.loads(row[1])
        if not batch or digest(batch) != request['batch_hash'] or batch['body'].get('settlement_mode') != ('prepaid-v1' if prepaid_request else 'provider-key-v1'):
            raise ValueError('Key request does not match batch')
        if not prepaid_request:
            proof = bytes.fromhex(request['payment_preimage'])
            if len(proof) != 32 or hashlib.sha256(proof).hexdigest() != batch['body']['invoice_payment_hash']:
                raise ValueError('Invalid payment proof')
        if not await self.batch_settled(request['session'], request['sequence'], batch['body']):
            raise ValueError('Payment is not confirmed credited')
        self.store.paid(request['session'], request['sequence'])
        saved = self.store.hosted_batch(request['session'], request['sequence'])
        return self.identity.sign({'type':'batch-key','session':request['session'],
            'sequence':request['sequence'],'batch_hash':digest(batch),'key':saved['key'], 'buyer':envelope['signer']})

    def accept(self, envelope):
        body = verify(envelope)
        if set(body) not in ({"type", "session", "quote_hash"}, {"type", "session", "quote_hash", "quote", "request"}) or body["type"] != "accept":
            raise ValueError("Invalid quote acceptance")
        entry = self.pending.get(body["session"])
        if 'quote' in body:
            q = verify(body['quote'], self.identity.public)
            req = body['request']
            if (req.get('signer') != envelope['signer'] or digest(req) != body['session']
                    or q.get('session') != body['session']):
                raise ValueError('Quote request binding mismatch')
            # Revalidate availability, model, budget and current offer before work.
            current = self.quote(req)
            for field in ('output_msat_per_token', 'max_total_msat', 'model_id', 'buyer',
                          'max_output_tokens', 'settlement_mode', 'payment_network', 'output_msat_per_token_exact',
                          'minimum_compute_msat', 'batch_tokens', 'funding_floor_msat', 'funding_max_msat', 'funding_usd_per_btc'):
                if q.get(field) != current['body'].get(field):
                    raise ValueError('Offer changed; request a new quote')
            entry = (body['quote'], Request.model_validate(verify(req)))
        if not entry:
            raise ValueError("Quote unavailable or already accepted")
        quote, request = entry
        if (quote["body"]["buyer"] != envelope["signer"] or digest(quote) != body["quote_hash"]
                or quote["body"]["expires"] <= time.time()):
            raise ValueError("Quote acceptance mismatch or expiry")
        if self.active >= self.config.max_sessions:
            raise ValueError('Provider capacity reserved')
        if quote['body'].get('settlement_mode') == 'prepaid-v1':
            self.credits.admit(body['session'], envelope['signer'], quote, self.config.offer.manifest.context_tokens,
                               self.config.max_requests_per_hour, self.config.max_work_tokens_per_hour)
        else:
            self.store.admit(body['session'], self.config.offer.manifest.context_tokens,
                             self.config.max_requests_per_hour, self.config.max_work_tokens_per_hour,
                             buyer=envelope['signer'], quote=quote)
        self.pending.pop(body['session'], None)
        self.running.add(body["session"])
        return quote, request

    async def stream(self, quote, request):
        q = quote["body"]
        session, previous, total, seq = q["session"], digest(quote), 0, 0
        started, first_batch_ms = time.monotonic(), None
        finish_reason = None
        prepaid = q.get('settlement_mode') == 'prepaid-v1'
        try:
            self.store.state(session, "running")
            if prepaid:
                # Persist the disclosed minimum before starting any GPU work.
                self.credits.charge(session, q['minimum_compute_msat'])
            # Includes payment waits so slow/nonpaying buyers cannot hold capacity forever.
            async with asyncio.timeout(q["generation_deadline_s"]):
                source = (self.backend.stream_chat([m.model_dump() for m in request.messages], request.max_output_tokens)
                          if request.messages else self.backend.stream(request.prompt, request.max_output_tokens))
                async def track_completion():
                    nonlocal finish_reason
                    async for group in source:
                        if group.get("finish_reason") in {"stop", "length"} and not group.get("token_ids"):
                            finish_reason = group["finish_reason"]
                        else:
                            yield group
                async with aclosing(source), aclosing(track_completion()) as tracked:
                    async for groups in batches(tracked, q["batch_tokens"]):
                        count = sum(len(group["token_ids"]) for group in groups)
                        total += count
                        if total > q["max_output_tokens"]:
                            raise ValueError("Backend exceeded quoted output budget")
                        amount = charge(q, total) - charge(q, total-count)
                        header = {"session": session, "sequence": seq, "previous": previous,
                                  "model_id": q["model_id"], "request_hash": q["request_hash"],
                                  "token_count": count, "total_tokens": total, "amount_msat": amount}
                        preimage = secrets.token_bytes(32)
                        sealed = seal({"groups": groups}, header, preimage)
                        commitment = digest(sealed)
                        hosted = None
                        if prepaid:
                            invoice = None
                            hosted = {'key':b64(preimage), 'reference':{'prepaid':True}}
                            self.credits.charge(session, max(q['minimum_compute_msat'], charge(q, total)))
                        elif amount and q.get('settlement_mode') == 'provider-key-v1':
                            issued = await self.wallet.create_batch_invoice(amount, commitment, q['payment_timeout_s'])
                            invoice = issued['invoice']
                            hosted = {'key':b64(preimage), 'reference':issued['reference']}
                        else:
                            invoice = await self.wallet.invoice(preimage, amount, commitment, q["payment_timeout_s"]) if amount else None
                        body = {"type": "batch", "sealed": sealed, "invoice": invoice,
                                "proof": "unavailable", "free_key": b64(preimage) if not amount and not prepaid else None}
                        if prepaid:
                            body['settlement_mode'] = 'prepaid-v1'
                        if hosted and not prepaid:
                            body.update(settlement_mode='provider-key-v1', invoice_payment_hash=issued['payment_hash'])
                        envelope = self.identity.sign(body)
                        # Persist before sending. Buyer retains ciphertext and can recover it.
                        self.store.batch(session, seq, envelope, hosted=hosted)
                        if first_batch_ms is None:
                            first_batch_ms = int((time.monotonic() - started) * 1000)
                        if prepaid:
                            self.store.paid(session, seq)
                        yield envelope
                        if amount and not prepaid:
                            if hosted:
                                end = time.monotonic() + q['payment_timeout_s']
                                while not await self.batch_settled(session, seq, body):
                                    if time.monotonic() >= end:
                                        raise TimeoutError('Payment deadline exceeded')
                                    await asyncio.sleep(2)
                            elif not await self.wallet.wait(sealed["payment_hash"], amount, q["payment_timeout_s"]):
                                raise TimeoutError("Payment deadline exceeded")
                            self.store.paid(session, seq)
                        previous = digest(envelope)
                        seq += 1
            self.store.state(session, "complete")
            if prepaid:
                self.credits.close(session)
            yield self.identity.sign({"type": "end", "session": session, "previous": previous,
                                      "total_tokens": total, "batches": seq,
                                      "finish_reason": finish_reason or ("length" if total == q["max_output_tokens"] else "stop"),
                                      **({'charged_msat':max(q['minimum_compute_msat'], charge(q, total))} if prepaid else {})})
        except asyncio.CancelledError:
            self.store.state(session, "interrupted")
            raise
        except GeneratorExit:
            self.store.state(session, "interrupted")
            raise
        except Exception as exc:
            self.store.state(session, "failed")
            yield self.identity.sign({"type": "error", "session": session,
                                      "code": type(exc).__name__, "previous": previous,
                                      "message": "Stream stopped; retain delivered batches and payment records"})
        finally:
            try:
                self.store.observe(session, first_batch_ms, int((time.monotonic() - started) * 1000))
            finally:
                self.release(session)
