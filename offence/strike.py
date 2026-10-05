"""Receive-only Strike adapter. No spending, redirects, or arbitrary API origins."""
import os
import re
import time
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
from .wire import identity_bytes


def strike_address(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9_.-]{1,64}@strike\.me', value):
        raise ValueError('Use a Strike Lightning Address: username@strike.me')
    return value


def btc_msat(value):
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r'[0-9]+(?:\.[0-9]{1,11})?', value):
        raise ValueError('Invalid BTC amount')
    result = Decimal(value) * 100_000_000_000
    if not result.is_finite() or result != result.to_integral_value():
        raise ValueError('Invalid BTC precision')
    return int(result)


def amount_matches(value, amount):
    return (isinstance(value, dict) and value.get('currency') == 'BTC'
            and btc_msat(value.get('amount')) == amount)


class Strike:
    network = 'mainnet'
    settlement_mode = 'provider-key-v1'

    def __init__(self, address, api_key, transport=None):
        self.address = strike_address(address)
        if not api_key or any(c.isspace() for c in api_key):
            raise ValueError('Missing or invalid Strike API credential')
        self.headers = {'Authorization': 'Bearer ' + api_key, 'Accept-Encoding': 'identity'}
        self.transport = transport

    @classmethod
    def from_env(cls, address):
        return cls(address, os.environ.get('OFFENCE_STRIKE_API_KEY', ''))

    async def call(self, method, path, data=None):
        async with httpx.AsyncClient(base_url='https://api.strike.me', headers=self.headers,
                transport=self.transport, trust_env=False, follow_redirects=False, timeout=20) as client:
            async with client.stream(method, path, json=data) as response:
                if response.status_code not in {200, 201}:
                    # Do not expose remote error bodies, request headers, or credentials.
                    raise ValueError('Strike request failed')
                raw = bytearray()
                async for chunk in identity_bytes(response):
                    raw.extend(chunk)
                    if len(raw) > 256 * 1024:
                        raise ValueError('Strike response exceeds limit')
                import json
                return json.loads(raw)

    async def profile(self):
        result = await self.call('GET', '/v1/accounts/handle/' + self.address.split('@')[0] + '/profile')
        if (result.get('handle', '').lower() != self.address.split('@')[0]
                or result.get('canReceive') is not True
                or not any(c.get('currency') == 'BTC' and c.get('isInvoiceable') is True
                           and c.get('isAvailable') is True for c in result.get('currencies', []))):
            raise ValueError('Strike account cannot receive BTC invoices')
        return str(UUID(result['id']))

    async def check_network(self):
        # Fixed production origin only. This never creates or pays an invoice.
        await self.profile()

    async def create_batch_invoice(self, amount, commitment, expiry):
        if type(amount) is not int or amount <= 0:
            raise ValueError('Invalid invoice amount')
        receiver = await self.profile()
        correlation = str(uuid4())
        invoice = await self.call('POST', '/v1/invoices/handle/' + self.address.split('@')[0], {
            'correlationId': correlation, 'description': 'Offence encrypted token batch',
            'amount': {'currency': 'BTC', 'amount': format(Decimal(amount)/100_000_000_000, '.11f')}})
        invoice_id = str(UUID(invoice['invoiceId']))
        reference = {'invoice_id': invoice_id, 'receiver_id': receiver, 'correlation': correlation}
        self.validate_invoice(invoice, reference, amount)
        quote = await self.call('POST', '/v1/invoices/' + invoice_id + '/quote', {'descriptionHash': commitment})
        if not amount_matches(quote.get('sourceAmount'), amount) or not amount_matches(quote.get('targetAmount'), amount):
            raise ValueError('Strike quote changes BTC amount or receiving currency')
        # Parse invoice via the local BOLT11 validator. The buyer independently
        # validates it with its own wallet before spending.
        from .bolt11 import decode_invoice
        parsed = decode_invoice(quote['lnInvoice'])
        if (parsed['amount_msat'] != amount or parsed['description_hash'] != commitment
                or parsed['expires'] <= time.time() + 5):
            raise ValueError('Strike invoice differs from batch')
        return {'invoice': quote['lnInvoice'], 'payment_hash': parsed['payment_hash'], 'reference': reference}

    @staticmethod
    def validate_invoice(invoice, reference, amount):
        if (invoice.get('invoiceId') != reference['invoice_id']
                or invoice.get('receiverId') != reference['receiver_id']
                or invoice.get('correlationId') != reference['correlation']
                or not amount_matches(invoice.get('amount'), amount)):
            raise ValueError('Strike invoice identity or amount mismatch')

    async def settled_batch(self, reference, payment_hash, amount):
        invoice_id = str(UUID(reference['invoice_id']))
        invoice = await self.call('GET', '/v1/invoices/' + invoice_id + '?includeTransactions=true')
        self.validate_invoice(invoice, reference, amount)
        if invoice.get('state') != 'PAID':
            return False
        return any(t.get('state') == 'COMPLETED' and amount_matches(t.get('amountReceived'), amount)
                   for t in invoice.get('transactions', []))
