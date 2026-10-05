"""Direct LND integration with explicit network selection and no network fallback."""
import asyncio
import base64
import hashlib
import json
import os
import ssl
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from .wire import identity_bytes
from .crypto import b64, unb64


class LndRegtest:
    network = "regtest"
    invoice_prefix = "lnbcrt"
    def __init__(self, url, macaroon_path, ca_path):
        if not url.startswith("https://"):
            raise ValueError("LND requires HTTPS")
        self.url = url.rstrip("/")
        self.headers = {"Grpc-Metadata-macaroon": Path(macaroon_path).read_bytes().hex()}
        self.tls = ssl.create_default_context(cafile=ca_path)

    @classmethod
    def from_env(cls):
        if os.environ.get("OFFENCE_LND_MACAROON_HEX"):
            instance = object.__new__(cls)
            url = os.environ["OFFENCE_LND_URL"]
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
                raise ValueError("LND requires a fixed HTTPS origin")
            macaroon = bytes.fromhex(os.environ["OFFENCE_LND_MACAROON_HEX"])
            if not macaroon:
                raise ValueError("Missing wallet credential")
            instance.url = url.rstrip("/")
            instance.headers = {"Grpc-Metadata-macaroon": macaroon.hex()}
            instance.tls = ssl.create_default_context(cadata=os.environ["OFFENCE_LND_TLS_CERT_PEM"])
            return instance
        return cls(os.environ["OFFENCE_LND_URL"], os.environ["OFFENCE_LND_MACAROON_FILE"],
                   os.environ["OFFENCE_LND_TLS_CERT_FILE"])

    async def call(self, method, path, data=None):
        async with httpx.AsyncClient(base_url=self.url, headers={**self.headers, "Accept-Encoding": "identity"}, verify=self.tls,
                                     trust_env=False, timeout=30, follow_redirects=False) as client:
            async with client.stream(method, path, json=data) as response:
                response.raise_for_status()
                raw = bytearray()
                async for block in identity_bytes(response):
                    raw.extend(block)
                    if len(raw) > 1024 * 1024:
                        raise ValueError("Wallet response exceeds limit")
                return json.loads(raw)

    async def check_network(self):
        info = await self.call("GET", "/v1/getinfo")
        chains = info.get("chains", [])
        if not chains or any(c.get("chain") != "bitcoin" or c.get("network") != self.network for c in chains):
            raise ValueError(f"LND must be on bitcoin {self.network}")

    async def invoice(self, preimage, amount_msat, commitment, expiry):
        await self.check_network()
        if amount_msat <= 0:
            raise ValueError("Invoice amount must be positive")
        response = await self.call("POST", "/v1/invoices", {
            "r_preimage": b64(preimage), "value_msat": str(amount_msat),
            "description_hash": b64(bytes.fromhex(commitment)), "expiry": str(expiry)})
        if unb64(response["r_hash"]).hex() != hashlib.sha256(preimage).hexdigest():
            raise ValueError("LND returned the wrong invoice hash")
        return response["payment_request"]

    async def settled(self, payment_hash, amount_msat):
        result = await self.call("GET", "/v1/invoice/" + payment_hash)
        return result.get("state") == "SETTLED" and int(result.get("amt_paid_msat", 0)) >= amount_msat

    async def wait(self, payment_hash, amount_msat, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await self.settled(payment_hash, amount_msat):
                return True
            await asyncio.sleep(0.25)
        return False

    async def pay(self, invoice, payment_hash, amount_msat, commitment, fee_limit_msat):
        await self.check_network()
        if not invoice.startswith(self.invoice_prefix) or (self.network == "mainnet" and invoice.startswith("lnbcrt")):
            raise ValueError(f"Only {self.network} invoices are permitted")
        decoded = await self.call("GET", "/v1/payreq/" + invoice)
        if (decoded.get("payment_hash") != payment_hash or int(decoded.get("num_msat", -1)) != amount_msat
                or decoded.get("description_hash") != commitment
                or int(decoded.get("timestamp", 0)) + int(decoded.get("expiry", 0)) <= time.time()):
            raise ValueError("Invoice differs from the signed batch or is expired")
        result = await self.call("POST", "/v1/channels/transactions", {
            "payment_request": invoice, "fee_limit": {"fixed_msat": str(fee_limit_msat)}})
        if result.get("payment_error"):
            raise ValueError("Lightning payment failed")
        preimage = unb64(result["payment_preimage"])
        if hashlib.sha256(preimage).hexdigest() != payment_hash:
            raise ValueError("Payment returned an invalid preimage")
        return preimage

    async def track(self, payment_hash, amount_msat, fee_limit_msat):
        """Bounded read-only reconciliation. Missing or interrupted results stay unknown."""
        await self.check_network()
        if len(payment_hash) != 64 or len(bytes.fromhex(payment_hash)) != 32:
            raise ValueError("Invalid payment hash")
        try:
            async with asyncio.timeout(15):
                async with httpx.AsyncClient(base_url=self.url, headers={**self.headers, "Accept-Encoding": "identity"}, verify=self.tls,
                        trust_env=False, timeout=10, follow_redirects=False) as client:
                    async with client.stream("GET", "/v2/router/track/" + quote(base64.urlsafe_b64encode(bytes.fromhex(payment_hash)).decode(), safe=""),
                                             params={"no_inflight_updates": "false"}) as response:
                        response.raise_for_status()
                        buffer = b""
                        async for chunk in identity_bytes(response):
                            buffer += chunk
                            if len(buffer) > 1024 * 1024:
                                raise ValueError("Payment tracking response exceeded limit")
                            if b"\n" not in buffer:
                                continue
                            line, _ = buffer.split(b"\n", 1)
                            update = json.loads(line)
                            if "error" in update:
                                return {"status": "UNKNOWN"}
                            return self.validate_tracking(update["result"], payment_hash,
                                                          amount_msat, fee_limit_msat)
                        if buffer.strip():
                            update = json.loads(buffer)
                            if "error" not in update:
                                return self.validate_tracking(update["result"], payment_hash,
                                                              amount_msat, fee_limit_msat)
        except (httpx.HTTPError, TimeoutError):
            return {"status": "UNKNOWN"}
        return {"status": "UNKNOWN"}

    @staticmethod
    def validate_tracking(result, payment_hash, amount_msat, fee_limit_msat):
        if result.get("payment_hash") != payment_hash or int(result.get("value_msat", -1)) != amount_msat:
            raise ValueError("Payment tracking contract mismatch")
        status = result.get("status")
        if status not in {"SUCCEEDED", "FAILED", "IN_FLIGHT", "INITIATED"}:
            return {"status": "UNKNOWN"}
        if status != "SUCCEEDED":
            return {"status": status}
        preimage = bytes.fromhex(result["payment_preimage"])
        fee = int(result.get("fee_msat", -1))
        if len(preimage) != 32 or hashlib.sha256(preimage).hexdigest() != payment_hash:
            raise ValueError("Payment tracking preimage mismatch")
        if not 0 <= fee <= fee_limit_msat:
            raise ValueError("Payment tracking fee exceeds authorization")
        return {"status": status, "preimage": preimage.hex(), "fee_msat": fee}


class LndMainnet(LndRegtest):
    network = "mainnet"
    invoice_prefix = "lnbc"
