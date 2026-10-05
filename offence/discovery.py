import asyncio
import ipaddress
import json
import logging
import random
import re
import time
from urllib.parse import urlsplit

import httpx
from .crypto import canonical, verify
from .wire import identity_bytes

log = logging.getLogger(__name__)
MAX_WIRE = 512 * 1024


def peer_url(url: str, allowed_private=()):
    """Reject arbitrary LAN probes and DNS rebinding from untrusted gossip.

    Public DNS names are deliberately not auto-dialled yet. Literal public IPs,
    v3 onion names, and exact operator-approved origins are supported.
    """
    p = urlsplit(url)
    if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
            or p.path not in {"", "/"} or p.query or p.fragment or p.port == 0):
        raise ValueError("Expected an HTTP(S) peer origin")
    normalized = url.rstrip("/")
    if normalized in allowed_private:
        return normalized
    if re.fullmatch(r"[a-z2-7]{56}\.onion", p.hostname) and p.scheme == "http":
        return normalized
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError as exc:
        raise ValueError("Peer DNS names require explicit operator configuration") from exc
    if not address.is_global or p.scheme != "https":
        raise ValueError("Public peers require HTTPS and a globally routable IP")
    return normalized


async def read_json(response):
    response.raise_for_status()
    data = bytearray()
    async for block in identity_bytes(response):
        if len(data) + len(block) > MAX_WIRE:
            raise ValueError("Peer response too large")
        data.extend(block)
    return json.loads(data)


class Discovery:
    def __init__(self, config, identity, store):
        self.config, self.identity, self.store = config, identity, store
        self.backoff = {}
        self.last_error = None

    def advertisement(self):
        if not self.config.endpoint:
            return None
        endpoint = peer_url(self.config.endpoint, self.config.allowed_private_peers)
        now = int(time.time())
        offer = self.config.offer
        body = {"type": "advertisement", "network": self.config.network, "issued": now,
                "expires": now + 180, "sequence": self.store.sequence(), "endpoint": endpoint,
                "offer": {**offer.model_dump(), "text_chat": self.config.backend == "vllm"} if offer else None}
        envelope = self.identity.sign(body)
        self.store.ingest(envelope)
        return envelope

    def client(self, endpoint):
        proxy = self.config.tor_proxy if urlsplit(endpoint).hostname.endswith(".onion") else None
        if endpoint.split("://", 1)[1].split(":", 1)[0].endswith(".onion") and not proxy:
            raise ValueError("Tor peer requires a configured SOCKS proxy")
        return httpx.AsyncClient(proxy=proxy, timeout=10, follow_redirects=False, trust_env=False,
                                 headers={'Accept-Encoding': 'identity'})

    async def exchange(self, endpoint, expected=None):
        endpoint = peer_url(endpoint, self.config.allowed_private_peers)
        import secrets
        nonce = secrets.token_hex(16)
        payload = self.identity.sign({"type": "gossip", "nonce": nonce,
                                      "ads": self.store.peers(16)})
        async with self.client(endpoint) as client:
            async with client.stream("POST", endpoint + "/v1/gossip", content=canonical(payload),
                                     headers={"Content-Type": "application/json"}) as response:
                message = await read_json(response)
        body = verify(message, expected)
        if body.get("type") != "gossip-reply" or body.get("nonce") != nonce:
            raise ValueError("Gossip response challenge mismatch")
        ads = body.get("ads")
        if not isinstance(ads, list) or len(ads) > 32:
            raise ValueError("Invalid advertisement list")
        for ad in ads:
            try:
                self.store.ingest(ad)
            except (ValueError, TypeError, KeyError):
                continue

    async def tick(self):
        self.advertisement()
        self.store.cleanup()
        candidates = {url: None for url in self.config.seeds}
        for ad in self.store.peers():
            if ad["signer"] != self.identity.public:
                candidates[ad["body"]["endpoint"]] = ad["signer"]
        items = list(candidates.items())
        random.shuffle(items)
        # Always spend part of each tick on configured seeds. Gossip saturation
        # cannot starve the owner's independent bootstrap paths.
        seeds = [(url, None) for url in self.config.seeds]
        random.shuffle(seeds)
        selected = seeds[:2]
        selected += [item for item in items if item[0] not in {s[0] for s in selected}][:4-len(selected)]
        for endpoint, expected in selected:
            failures, retry = self.backoff.get(endpoint, (0, 0))
            if retry > time.monotonic():
                continue
            try:
                await self.exchange(endpoint, expected)
                self.backoff.pop(endpoint, None)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                failures = min(failures + 1, 8)
                if len(self.backoff) >= self.config.max_peers:
                    self.backoff.pop(next(iter(self.backoff)))
                self.backoff[endpoint] = (failures, time.monotonic() + min(300, 2**failures))
                self.last_error = type(exc).__name__
                log.info("Peer exchange failed: %s", type(exc).__name__)

    async def run(self):
        while True:
            try:
                await self.tick()
            except (ValueError, OSError) as exc:
                self.last_error = type(exc).__name__
                log.warning("Discovery configuration error: %s", type(exc).__name__)
            await asyncio.sleep(self.config.gossip_interval_s)
