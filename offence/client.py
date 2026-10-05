"""Buyer verification and durable ciphertext retention before any regtest payment."""
import asyncio
from .pricing import charge, rate, cent_deposit
from contextlib import aclosing
import hashlib
import json
import os
from pathlib import Path
import secrets
import time

import httpx
from .wire import identity_bytes
from .crypto import canonical, digest, unb64, unseal, verify


class Buyer:
    def __init__(self, identity, directory: Path, wallet=None):
        self.identity, self.directory, self.wallet = identity, directory, wallet
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.record_limit = 64 * 1024 * 1024
        self.record_bytes = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())

    def save(self, name, value):
        # Ciphertexts and recovered keys are private local records. Never gossip them.
        destination = self.directory / name
        temporary = self.directory / (name + ".tmp")
        encoded = canonical(value)
        old_size = destination.stat().st_size if destination.exists() else 0
        if self.record_bytes - old_size + len(encoded) > self.record_limit:
            raise ValueError("Buyer evidence storage limit reached")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name != "nt":
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        self.record_bytes += len(encoded) - old_size

    def credit_balances(self):
        balances = {}
        def entry(provider):
            return balances.setdefault(provider, {'provider':provider,'deposited_msat':0,
                'charged_msat':0,'reserved_msat':0,'pending_sessions':0})
        for path in self.directory.glob('*.fund.credited.json'):
            name = path.name.removesuffix('.credited.json')
            voucher = json.loads((self.directory/(name+'.batch.json')).read_text())
            deposit = verify(voucher)['deposit']
            entry(voucher['signer'])['deposited_msat'] += deposit['amount_msat']
        for path in self.directory.glob('*.credit.json'):
            record = json.loads(path.read_text())
            item = entry(record['provider'])
            if record['pending']:
                item['reserved_msat'] += record['charged_msat']
                item['pending_sessions'] += 1
            else:
                item['charged_msat'] += record['charged_msat']
        for item in balances.values():
            item['available_estimate_msat'] = max(0,item['deposited_msat']-item['charged_msat']-item['reserved_msat'])
        return list(balances.values())

    async def reconcile_payments(self):
        """Recover payment keys without ever sending a payment or restarting GPU work."""
        if self.wallet is None:
            raise ValueError("Payment recovery requires a regtest wallet")
        results = []
        for path in sorted(self.directory.glob("*.attempt.json")):
            name = path.name.removesuffix(".attempt.json")
            if (self.directory / (name + ".payment.json")).exists() or (self.directory / (name + ".failed.json")).exists():
                continue
            attempt = json.loads(path.read_text())
            if attempt.get("network", "regtest") != getattr(self.wallet, "network", "regtest"):
                continue
            if attempt.get('wallet_identity') != getattr(self.wallet, 'identity', None):
                continue
            batch = json.loads((self.directory / (name + ".batch.json")).read_text())
            body = verify(batch, attempt["provider"])
            if attempt.get('kind') == 'credit-deposit':
                valid = (body.get('type') == 'credit-invoice'
                    and body['deposit']['buyer'] == self.identity.public
                    and digest(body['deposit']) == attempt['commitment']
                    and body['payment_hash'] == attempt['payment_hash']
                    and body['deposit']['amount_msat'] == attempt['amount_msat'])
            else:
                sealed = body['sealed']
                valid = (digest(sealed) == attempt['commitment']
                    and body.get('invoice_payment_hash', sealed['payment_hash']) == attempt['payment_hash']
                    and sealed['header']['amount_msat'] == attempt['amount_msat'])
            if digest(batch) != attempt['batch_hash'] or not valid:
                raise ValueError('Payment recovery evidence mismatch')
            result = await self.wallet.track(attempt["payment_hash"], attempt["amount_msat"],
                                             attempt["fee_limit_msat"])
            if result["status"] == "SUCCEEDED":
                preimage = bytes.fromhex(result["preimage"])
                # Keep the key even if the provider supplied invalid ciphertext.
                self.save(name + ".payment.json", {"preimage": preimage.hex(),
                    "amount_msat": attempt["amount_msat"], "fee_msat": result["fee_msat"]})
            elif result["status"] == "FAILED":
                self.save(name + ".failed.json", {"status": "FAILED"})
            results.append({"batch": name, "status": result["status"]})
        return results

    async def credit_query(self, client, provider, voucher=None, session=None):
        from .discovery import read_json
        nonce = secrets.token_hex(32)
        query = {'type':'credit-status','issued':int(time.time()),'nonce':nonce}
        if voucher is not None: query['voucher'] = voucher
        if session is not None: query['session'] = session
        async with client.stream('POST','/v1/credit/status',json=self.identity.sign(query)) as response:
            result = verify(await read_json(response), provider)
        if (result.get('type') != 'credit-status' or result.get('nonce') != nonce
                or result.get('buyer') != self.identity.public
                or type(result.get('balance_msat')) is not int or result['balance_msat'] < 0
                or (session is not None and result.get('session') != session)):
            raise ValueError('Invalid credit status')
        if voucher is not None and result.get('deposit_payment_hash') != voucher['body']['payment_hash']:
            raise ValueError('Credit status voucher mismatch')
        return result

    async def fund(self, client, provider, quote, endpoint, fee_limit_msat):
        from .discovery import read_json
        q = quote['body']
        request = self.identity.sign({'type':'fund-credit','quote':quote,'issued':int(time.time())})
        async with client.stream('POST','/v1/credit/fund',json=request) as response:
            voucher = await read_json(response)
        v = verify(voucher, provider)
        if v.get('type') == 'credit-ready':
            if (v.get('buyer') != self.identity.public or v.get('quote_hash') != digest(quote)
                    or v.get('amount_msat') != 0):
                raise ValueError('Invalid credit readiness')
            return 0
        d = v.get('deposit', {})
        if (v.get('type') != 'credit-invoice' or d.get('type') != 'credit-deposit'
                or d.get('provider') != provider or d.get('buyer') != self.identity.public
                or d.get('quote_hash') != digest(quote) or d.get('network') != q['payment_network']
                or type(d.get('amount_msat')) is not int or not 0 < d['amount_msat'] <= q.get('funding_max_msat', q['max_total_msat'])
                or (q.get('funding_floor_msat') and (d['amount_msat'] % 1000 or d['amount_msat'] < q['funding_floor_msat']))
                or digest(d) != v.get('commitment')):
            raise ValueError('Prepaid invoice violates contract')
        name = q['session'] + '.fund'
        self.save(name+'.batch.json', voucher)
        self.save(name+'.attempt.json', {'kind':'credit-deposit','endpoint':endpoint,
            'provider':provider,'session':q['session'],'payment_hash':v['payment_hash'],
            'amount_msat':d['amount_msat'],'fee_limit_msat':fee_limit_msat,
            'commitment':v['commitment'],'invoice':v['invoice'],'batch_hash':digest(voucher),
            'wallet_identity':getattr(self.wallet,'identity',None),'network':getattr(self.wallet,'network','regtest')})
        preimage = await self.wallet.pay(v['invoice'],v['payment_hash'],d['amount_msat'],v['commitment'],fee_limit_msat)
        self.save(name+'.payment.json', {'preimage':preimage.hex(),'amount_msat':d['amount_msat']})
        deadline = time.monotonic()+30
        while True:
            result = await self.credit_query(client, provider, voucher=voucher)
            if result.get('deposit_credited') is True:
                self.save(name+'.credited.json', {'provider':provider,'payment_hash':v['payment_hash']})
                break
            if time.monotonic() >= deadline:
                raise ValueError('Deposit pending credit; recover without paying again')
            await asyncio.sleep(1)
        return d['amount_msat']

    async def recover_credits(self, approved_origins=(), transport=None, tor_proxy=None):
        """Read-only supplier reconciliation while no purchases are running."""
        from .discovery import peer_url
        results = []
        for path in sorted(self.directory.glob('*.fund.attempt.json')):
            name = path.name.removesuffix('.attempt.json')
            if (not (self.directory/(name+'.payment.json')).exists()
                    or (self.directory/(name+'.credited.json')).exists()):
                continue
            attempt = json.loads(path.read_text())
            endpoint = peer_url(attempt['endpoint'], approved_origins)
            voucher = json.loads((self.directory/(name+'.batch.json')).read_text())
            async with httpx.AsyncClient(base_url=endpoint, transport=transport, headers={"Accept-Encoding": "identity"},
                    proxy=tor_proxy if '.onion' in endpoint else None,
                    timeout=10, trust_env=False, follow_redirects=False) as client:
                status = await self.credit_query(client, attempt['provider'], voucher=voucher)
                if status.get('deposit_credited') is True:
                    self.save(name+'.credited.json', {'provider':attempt['provider'],'payment_hash':attempt['payment_hash']})
                results.append({'provider':attempt['provider'], 'balance_msat':status['balance_msat']})
        for path in sorted(self.directory.glob('*.credit.json')):
            record = json.loads(path.read_text())
            if not record['pending']:
                continue
            endpoint = peer_url(record['endpoint'], approved_origins)
            async with httpx.AsyncClient(base_url=endpoint, transport=transport, headers={"Accept-Encoding": "identity"},
                    proxy=tor_proxy if '.onion' in endpoint else None,
                    timeout=10, trust_env=False, follow_redirects=False) as client:
                status = await self.credit_query(client, record['provider'], session=record['session'])
            hold = status.get('hold')
            if hold is None:
                # An earlier HTTP acceptance could still be in flight. Wait
                # beyond its quote lifetime before declaring it unaccepted.
                if time.time() <= record['expires']+60:
                    continue
                charged = 0
            else:
                if (type(hold.get('charged_msat')) is not int
                        or not 0 <= hold['charged_msat'] <= record['maximum_msat']
                        or hold.get('reserved_msat') != record['maximum_msat']):
                    raise ValueError('Invalid credit accounting')
                if hold.get('closed') != 1:
                    continue
                charged = hold['charged_msat']
            record.update(charged_msat=charged, pending=False)
            self.save(path.name, record)
        return results

    async def recover_prepaid_output(self, endpoint, provider, session, transport=None, tor_proxy=None):
        """Retrieve already purchased output. Never admit work or send payments."""
        from .discovery import read_json
        quote = json.loads((self.directory/(session+'.quote.json')).read_text())
        q = verify(quote, provider)
        if q.get('settlement_mode') != 'prepaid-v1' or q['buyer'] != self.identity.public:
            raise ValueError('Invalid prepaid recovery quote')
        async with httpx.AsyncClient(base_url=endpoint, transport=transport, headers={"Accept-Encoding": "identity"}, proxy=tor_proxy,
                timeout=40, trust_env=False, follow_redirects=False) as client:
            complete = False
            async def pages():
                nonlocal complete
                offset = 0
                while True:
                    request = self.identity.sign({'type':'recover','session':session,
                        'issued':int(time.time()),'offset':offset})
                    async with client.stream('POST','/v1/recover',json=request) as response:
                        records = verify(await read_json(response), provider)
                    if (records.get('type') != 'recovery' or records.get('session') != session
                            or records.get('quote') != quote or not isinstance(records.get('batches'),list)
                            or len(records['batches']) > min(64,q['max_output_tokens']-offset)):
                        raise ValueError('Invalid prepaid recovery evidence')
                    for batch in records['batches']:
                        yield batch
                    following = records.get('next_offset')
                    if following is None:
                        complete = records.get('state') == 'complete'
                        break
                    if (type(following) is not int or following != offset+len(records['batches'])
                            or not offset < following < q['max_output_tokens']):
                        raise ValueError('Invalid recovery pagination')
                    offset = following
            previous, total, parts, seq = digest(quote), 0, [], 0
            async for batch in pages():
                body = verify(batch, provider)
                h = body['sealed']['header']
                count = h.get('token_count')
                if (body.get('type') != 'batch' or body.get('settlement_mode') != 'prepaid-v1'
                        or h.get('session') != session or h.get('sequence') != seq or h.get('previous') != previous
                        or h.get('model_id') != q['model_id'] or h.get('request_hash') != q['request_hash']
                        or type(count) is not int or not 1 <= count <= q['batch_tokens']
                        or h.get('total_tokens') != total+count or total+count > q['max_output_tokens']
                        or h.get('amount_msat') != charge(q, total+count)-charge(q, total)
                        or body.get('invoice') is not None or body.get('free_key') is not None):
                    raise ValueError('Invalid recovered token chain')
                key = await self.fetch_key(client, provider, batch, None)
                groups = unseal(body['sealed'], key)['groups']
                if (not isinstance(groups,list) or sum(len(g['token_ids']) for g in groups) != count
                        or any(type(t) is not int or t < 0 for g in groups for t in g['token_ids'])
                        or any(not isinstance(g['text'],str) for g in groups)):
                    raise ValueError('Recovered token content differs from contract')
                name = f'{session}.{seq}'
                self.save(name+'.batch.json',batch)
                self.save(name+'.key.json',{'key':key.hex()})
                receipt = self.identity.sign({'type':'receipt','session':session,'sequence':seq,
                    'batch_hash':digest(batch),'payment_hash':body['sealed']['payment_hash'],'received_tokens':count})
                self.save(name+'.receipt.json',receipt)
                async with client.stream('POST','/v1/receipt',json=receipt) as response:
                    response.raise_for_status()
                parts.extend(g['text'] for g in groups)
                total += count
                previous = digest(batch)
                seq += 1
            return {'partial_output':''.join(parts), 'output_tokens':total,
                'spent_msat':charge(q, total), 'complete':complete}

    async def fetch_key(self, client, provider, batch, payment_preimage):
        from .discovery import read_json
        header = batch['body']['sealed']['header']
        prepaid = batch['body'].get('settlement_mode') == 'prepaid-v1'
        if not prepaid and hashlib.sha256(payment_preimage).hexdigest() != batch['body']['invoice_payment_hash']:
            raise ValueError('Payment proof differs from invoice')
        fields = {'type':'prepaid-key' if prepaid else 'release-key','session':header['session'],
            'sequence':header['sequence'],'batch_hash':digest(batch),'issued':int(time.time())}
        if not prepaid:
            fields['payment_preimage'] = payment_preimage.hex()
        request = self.identity.sign(fields)
        # Strike may report payment completion after the buyer receives its preimage.
        # A failed/unknown result preserves the payment record for explicit recovery.
        deadline = time.monotonic() + 30
        while True:
            async with client.stream('POST','/v1/release-key',json=request) as response:
                if response.status_code not in {400,429,503} or time.monotonic() >= deadline:
                    result = verify(await read_json(response), provider)
                    break
            await asyncio.sleep(2)
        if (result.get('type') != 'batch-key' or result.get('buyer') != self.identity.public
                or result.get('session') != header['session'] or result.get('sequence') != header['sequence']
                or result.get('batch_hash') != digest(batch)):
            raise ValueError('Invalid supplier key release')
        key = unb64(result['key'])
        unseal(batch['body']['sealed'], key)
        return key

    async def recover_hosted_keys(self, endpoint, provider, transport=None, tor_proxy=None):
        # The caller supplies the endpoint explicitly; never follow a recovery URL
        # from a remote invoice, webhook, or an unsigned payment result.
        results = []
        async with httpx.AsyncClient(base_url=endpoint, transport=transport, headers={"Accept-Encoding": "identity"}, proxy=tor_proxy,
                timeout=40, trust_env=False, follow_redirects=False) as client:
            for path in sorted(self.directory.glob('*.payment.json')):
                name = path.name.removesuffix('.payment.json')
                batch = json.loads((self.directory/(name+'.batch.json')).read_text())
                if batch['signer'] != provider:
                    continue
                body = verify(batch, provider)
                if body.get('settlement_mode') != 'provider-key-v1':
                    continue
                payment = json.loads(path.read_text())
                key = await self.fetch_key(client, provider, batch, bytes.fromhex(payment['preimage']))
                self.save(name+'.key.json', {'key':key.hex()})
                payload = unseal(body['sealed'],key)
                h = body['sealed']['header']
                groups = payload['groups']
                if (sum(len(g['token_ids']) for g in groups) != h['token_count']
                        or any(type(t) is not int or t < 0 for g in groups for t in g['token_ids'])
                        or any(not isinstance(g['text'],str) for g in groups)):
                    raise ValueError('Recovered token content differs from contract')
                receipt = self.identity.sign({'type':'receipt','session':h['session'],'sequence':h['sequence'],
                    'batch_hash':digest(batch),'payment_hash':body['invoice_payment_hash'], 'received_tokens':h['token_count']})
                self.save(name+'.receipt.json',receipt)
                try:
                    async with client.stream('POST','/v1/receipt',json=receipt): pass
                except httpx.HTTPError: pass
                results.append({'batch':name,'text':''.join(g['text'] for g in groups),'tokens':h['token_count']})
        return results

    async def run(self, *args, **kwargs):
        reservation = {}
        try:
            async with aclosing(self._run(*args, **kwargs, reservation=reservation)) as source:
                async for event in source:
                    yield event
        finally:
            if reservation:
                from .spending import settle
                settle(self.directory, reservation['session'])

    async def _run(self, endpoint, provider, model_id, prompt, max_tokens, max_msat,
                  allow_lab=False, fee_limit_msat=1000, transport=None, tor_proxy=None, messages=None, detailed=False, assurance=None, total_fee_limit_msat=1000, daily_limit_msat=0, allow_provider_key_release=False, allow_prepaid_compute=False, reservation=None, on_quote=None, funding_limit_msat=None):
        assurance = assurance or ("lab-unverified" if allow_lab else "required")
        if assurance not in {"lab-unverified", "seller-claim"}:
            raise ValueError("Execution proof unavailable; buyer refuses unverified inference")
        if getattr(self.wallet, "network", None) == "mainnet" and assurance != "seller-claim":
            raise ValueError("Mainnet requires explicit seller-claim assurance")
        if type(total_fee_limit_msat) is not int or total_fee_limit_msat < 0:
            raise ValueError("Invalid total routing fee limit")
        if type(fee_limit_msat) is not int or fee_limit_msat < 0:
            raise ValueError("Invalid per-batch routing fee limit")
        reserved_fees = 0
        network = "offence-v1" if getattr(self.wallet, "network", None) == "mainnet" else "offence-lab-v1"
        body = {"type": "request", "network": network,
            "provider": provider, "model_id": model_id, "nonce": secrets.token_hex(32),
            "issued": int(time.time()), "prompt": prompt, "max_output_tokens": max_tokens,
            "max_total_msat": max_msat, "proof_policy": assurance, "fractional_billing": True,
            "funding_limit_msat": max_msat if funding_limit_msat is None else funding_limit_msat}
        if allow_prepaid_compute:
            body['allow_prepaid_compute'] = True
        if allow_provider_key_release:
            body["allow_provider_key_release"] = True
        if messages is not None:
            body["messages"] = messages
        from .models import Request
        Request.model_validate(body)
        request = self.identity.sign(body)
        async with httpx.AsyncClient(base_url=endpoint, transport=transport, headers={"Accept-Encoding": "identity"}, proxy=tor_proxy,
                                     timeout=180, trust_env=False, follow_redirects=False) as client:
            from .discovery import read_json
            async with client.stream("POST", "/v1/quote", json=request) as response:
                quote = await read_json(response)
            q = verify(quote, provider)
            if (q.get("type") != "quote" or q.get("network") != network
                    or q.get("session") != digest(request) or q.get("request_hash") != digest(request)
                    or q.get("model_id") != model_id or q.get("buyer") != self.identity.public
                    or q.get("max_output_tokens") != max_tokens or q.get("max_total_msat", max_msat + 1) > max_msat
                    or q.get("expires", 0) <= time.time()
                    or type(q.get("output_msat_per_token")) is not int or q["output_msat_per_token"] < 0
                    or q["max_total_msat"] != charge(q, max_tokens)
                    or type(q.get("batch_tokens")) is not int or not 1 <= q["batch_tokens"] <= 128
                    or q.get("proof") != "unavailable"
                    or q.get("assurance", "lab-unverified") != assurance
                    or q.get("payment_network") != (getattr(self.wallet, "network", "regtest") if q["max_total_msat"] else "free-lab")):
                raise ValueError("Provider quote violates buyer request")
            if q["max_total_msat"] and not self.wallet:
                raise ValueError("Paid quote requires a configured buyer wallet before acceptance")
            mode = q.get('settlement_mode', 'preimage-v1')
            if (mode not in {'preimage-v1','provider-key-v1','prepaid-v1'}
                    or (mode == 'provider-key-v1' and not allow_provider_key_release)
                    or (mode == 'prepaid-v1' and not allow_prepaid_compute)):
                raise ValueError('Unaccepted settlement mode')
            if rate(q).denominator != 1 and mode != 'prepaid-v1':
                raise ValueError('Fractional rates require prepaid settlement')
            funding_max = q.get('funding_max_msat', q['max_total_msat'])
            if mode == 'prepaid-v1' and network == 'offence-v1' and 'funding_floor_msat' not in q:
                raise ValueError('Mainnet prepaid quote requires explicit funding terms')
            if ('funding_floor_msat' in q or 'funding_max_msat' in q or 'funding_usd_per_btc' in q):
                floor = cent_deposit(q.get('funding_usd_per_btc'))
                if (mode != 'prepaid-v1' or type(q.get('funding_floor_msat')) is not int
                        or q['funding_floor_msat'] != floor or type(funding_max) is not int
                        or funding_max != ((max(q['max_total_msat'], floor)+999)//1000)*1000
                        or funding_max > body['funding_limit_msat']):
                    raise ValueError('Funding quote exceeds buyer authorization')
            if on_quote is not None:
                on_quote(q)
            # A batch can contain fewer tokens than its maximum (byte limits or
            # backend groups). Reserve for one paid batch per output token rather
            # than assuming every batch will be full and failing after GPU work.
            if mode == 'prepaid-v1' and (type(q.get('minimum_compute_msat')) is not int
                    or q['minimum_compute_msat'] != min(q['max_total_msat'], charge(q, q['batch_tokens']))
                    or not 0 < q['minimum_compute_msat'] <= q['max_total_msat']):
                raise ValueError('Invalid prepaid minimum compute charge')
            if q["max_total_msat"] and (1 if mode == 'prepaid-v1' else max_tokens) * fee_limit_msat > total_fee_limit_msat:
                raise ValueError("Total routing fee budget cannot cover worst-case batch count")
            session = q["session"]
            if q["max_total_msat"] and (daily_limit_msat or getattr(self.wallet, "network", None) == "mainnet"):
                from .spending import reserve
                reserve(self.directory, session, max(q["max_total_msat"], funding_max), total_fee_limit_msat, daily_limit_msat)
                reservation["session"] = session
            self.save(session + ".quote.json", quote)
            if mode == 'prepaid-v1':
                deposited = await self.fund(client, provider, quote, endpoint, fee_limit_msat)
                self.save(session + '.credit.json', {'provider':provider, 'endpoint':endpoint,
                    'session':session, 'maximum_msat':q['max_total_msat'], 'deposited_msat':deposited,
                    'charged_msat':q['max_total_msat'], 'pending':True, 'expires':q['expires']})
            acceptance_body = {"type": "accept", "session": session, "quote_hash": digest(quote)}
            if q.get('acceptance') == 'quote-request-v1':
                acceptance_body.update(quote=quote, request=request)
            acceptance = self.identity.sign(acceptance_body)
            previous, total, spent, seq, ended = digest(quote), 0, 0, 0, False
            async with client.stream("POST", "/v1/stream", json=acceptance) as response:
                response.raise_for_status()
                buffer = b""
                async for chunk in identity_bytes(response):
                    buffer += chunk
                    if len(buffer) > 1024 * 1024:
                        raise ValueError("Provider stream buffer exceeded")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        message = json.loads(line)
                        body = verify(message, provider)
                        if ended:
                            raise ValueError("Data after stream end")
                        if body.get("type") == "error":
                            self.save(session + ".error.json", message)
                            raise ValueError("Provider stream failed; received tokens remain recorded")
                        if body.get("type") == "end":
                            if (body.get("session") != session or body.get("previous") != previous
                                    or body.get("total_tokens") != total or body.get("batches") != seq):
                                raise ValueError("Invalid completion record")
                            finish_reason = body.get("finish_reason", "length" if total == max_tokens else "stop")
                            if finish_reason not in {"stop", "length"}:
                                raise ValueError("Unsupported completion reason")
                            ended = True
                            self.save(session + ".end.json", message)
                            if mode == 'prepaid-v1':
                                charged = max(q['minimum_compute_msat'], charge(q, total))
                                if body.get('charged_msat') != charged:
                                    raise ValueError('Prepaid final charge differs from contract')
                                record = json.loads((self.directory/(session+'.credit.json')).read_text())
                                record.update(charged_msat=charged, pending=False)
                                self.save(session+'.credit.json', record)
                            continue
                        if body.get("type") != "batch" or body.get("proof") != "unavailable":
                            raise ValueError("Unexpected message or unsupported proof claim")
                        sealed = body["sealed"]
                        h = sealed["header"]
                        count = h.get("token_count")
                        amount = h.get("amount_msat")
                        if (h.get("session") != session or h.get("sequence") != seq or h.get("previous") != previous
                                or h.get("model_id") != model_id or h.get("request_hash") != digest(request)
                                or type(count) is not int or not 1 <= count <= q["batch_tokens"]
                                or h.get("total_tokens") != total + count or total + count > max_tokens
                                or type(amount) is not int or amount != charge(q, total+count)-charge(q, total)
                                or spent + amount > max_msat):
                            raise ValueError("Invalid batch ordering, token count, or billing")
                        if body.get('settlement_mode', 'preimage-v1') != mode:
                            raise ValueError('Batch settlement differs from quote')
                        payment_hash = body.get('invoice_payment_hash', sealed['payment_hash'])
                        if mode == 'provider-key-v1' and (not isinstance(payment_hash, str) or len(payment_hash) != 64
                                or len(bytes.fromhex(payment_hash)) != 32 or not amount):
                            raise ValueError('Invalid hosted payment hash')
                        name = f"{session}.{seq}"
                        self.save(name + ".batch.json", message)
                        if mode == 'prepaid-v1':
                            if body.get('invoice') is not None:
                                raise ValueError('Prepaid output cannot request another payment')
                            if body.get('free_key') is not None:
                                raise ValueError('Prepaid output key must stay private')
                            preimage = await self.fetch_key(client, provider, message, None)
                            self.save(name+'.key.json', {'key':preimage.hex()})
                        elif amount:
                            if not self.wallet or body.get("free_key") is not None:
                                raise ValueError("Paid batch requires a regtest wallet")
                            if reserved_fees + fee_limit_msat > total_fee_limit_msat:
                                raise ValueError("Total routing fee budget exhausted")
                            reserved_fees += fee_limit_msat
                            # Conservatively reserve the full fee cap, even on successful payments.
                            # Persist the intent before dispatch. Unknown outcomes are reconciled,
                            # never inferred to be failures and never automatically resubmitted.
                            self.save(name + ".attempt.json", {
                                "payment_hash": payment_hash, "amount_msat": amount,
                                "commitment": digest(sealed), "fee_limit_msat": fee_limit_msat,
                                "invoice": body["invoice"], "batch_hash": digest(message),
                                "provider": provider, "session": session, "sequence": seq,
                                "wallet_identity": getattr(self.wallet, 'identity', None),
                                "network": getattr(self.wallet, "network", "regtest")})
                            preimage = await self.wallet.pay(body["invoice"], payment_hash, amount,
                                                            digest(sealed), fee_limit_msat)
                            self.save(name + ".payment.json", {"preimage": preimage.hex(), "amount_msat": amount})
                            if mode == 'provider-key-v1':
                                preimage = await self.fetch_key(client, provider, message, preimage)
                                self.save(name + '.key.json', {'key':preimage.hex()})
                        else:
                            if body.get("invoice") is not None:
                                raise ValueError("Unexpected invoice for a free batch")
                            preimage = unb64(body["free_key"])
                        payload = unseal(sealed, preimage)
                        groups = payload["groups"]
                        if (not isinstance(groups, list) or sum(len(g["token_ids"]) for g in groups) != count
                                or any(type(t) is not int or t < 0 for g in groups for t in g["token_ids"])
                                or any(not isinstance(g["text"], str) for g in groups)):
                            raise ValueError("Decrypted token count or content differs from contract")
                        receipt = self.identity.sign({"type": "receipt", "session": session,
                            "sequence": seq, "batch_hash": digest(message),
                            "payment_hash": payment_hash, "received_tokens": count})
                        self.save(name + ".receipt.json", receipt)
                        # Receipt delivery failure must not discard already received output.
                        try:
                            async with client.stream("POST", "/v1/receipt", json=receipt):
                                pass
                        except httpx.HTTPError:
                            pass
                        total += count
                        spent += amount
                        seq += 1
                        previous = digest(message)
                        text = "".join(g["text"] for g in groups)
                        yield {"type": "delta", "text": text, "token_count": count, "session": session, "amount_msat": amount} if detailed else text
                if buffer.strip() or not ended:
                    raise ValueError("Stream interrupted without a signed completion record")

                if detailed:
                    yield {"type": "end", "finish_reason": finish_reason, "compute_minimum_msat": q.get("minimum_compute_msat", 0)}
