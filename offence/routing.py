"""Buyer-local policy routing. Signed claims are filtered before ranking."""
import time
from .crypto import verify
from .pricing import rate
from .discovery import peer_url
from .models import Advertisement, GatewayRoute


class Router:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.routes = {r.alias: r for r in config.gateway.routes}
        self.policies = {p.alias: p for p in config.gateway.policies}
        self.loads = {}

    def select(self, alias, max_tokens, excluded=(), trusted_only=False, max_price_msat=0, network=None, max_latency_ms=None):
        if alias in self.routes:
            r = self.routes[alias]
            if trusted_only:
                raise ValueError("Trusted-only routing requires a policy with explicit trust")
            if r.provider in excluded or max_tokens > r.max_output_tokens:
                raise ValueError("Pinned route unavailable or exceeds output limit")
            return r
        p = self.policies.get(alias)
        if not p or max_tokens > p.max_output_tokens:
            raise ValueError("Unknown policy or output limit exceeded")
        candidates = []
        for envelope in self.store.peers(self.config.max_peers):
            try:
                ad = Advertisement.model_validate(verify(envelope))
                provider, offer = envelope['signer'], ad.offer
                if (ad.expires <= time.time() or ad.issued > time.time() + 30 or not offer
                    or not offer.available or not offer.text_chat or rate(offer) > max_price_msat
                    or (network is not None and ad.network != network)
                    or provider in excluded or (p.providers and provider not in p.providers)
                    or ((trusted_only or p.privacy == 'trusted-only') and provider not in p.trusted_providers)
                    or max_tokens > offer.max_output_tokens or offer.manifest.context_tokens < p.min_context_tokens
                    or offer.manifest.model_id not in p.model_ids):
                    continue
                endpoint = peer_url(ad.endpoint, self.config.allowed_private_peers)
                if endpoint.split('://')[1].split(':')[0].endswith('.onion') and not self.config.tor_proxy:
                    continue
                stats = self.store.route_stats(provider, offer.manifest.model_id)
                # Failures cause a short local cooldown; providers cannot advertise their way out.
                if stats and stats['cooldown_until'] > time.time():
                    continue
                latency = stats['latency_ms'] if stats and stats['latency_ms'] is not None else 3600000
                if max_latency_ms is not None and (not stats or stats['latency_ms'] is None or latency > max_latency_ms):
                    continue
                failures = stats['failures'] if stats else 0
                preference = p.model_ids.index(offer.manifest.model_id)
                load = self.loads.get(provider, 0)
                if p.strategy == 'fastest':
                    rank = (load, latency, preference, provider)
                elif p.strategy == 'preferred-model':
                    rank = (preference, load, failures, latency, provider)
                elif p.strategy == 'cheapest':
                    rank = (rate(offer), load, failures, preference, latency, provider)
                else:
                    rank = (load, failures, preference, latency, provider)
                candidates.append((rank, GatewayRoute(alias=alias, endpoint=endpoint, provider=provider,
                    model_id=offer.manifest.model_id, max_output_tokens=min(p.max_output_tokens, offer.max_output_tokens))))
            except (ValueError, KeyError, TypeError):
                continue
        if not candidates:
            raise ValueError("No eligible provider; policy is never relaxed automatically")
        return min(candidates, key=lambda c:c[0])[1]

    def start(self, route):
        self.loads[route.provider] = self.loads.get(route.provider, 0) + 1

    def release(self, route):
        remaining = self.loads.get(route.provider, 1) - 1
        if remaining:
            self.loads[route.provider] = remaining
        else:
            self.loads.pop(route.provider, None)

    def finish(self, route, latency_ms, success):
        self.release(route)
        self.store.record_route(route.provider, route.model_id, latency_ms, success)
