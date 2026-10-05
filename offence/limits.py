"""ASGI limits cover the entire response, including slow streaming consumers."""
import asyncio
from collections import OrderedDict
import time
from starlette.responses import JSONResponse


class ResourceLimits:
    def __init__(self, app, max_active=32, send_timeout=10, lifetime=3660):
        self.app, self.max_active = app, max_active
        self.send_timeout, self.lifetime, self.active = send_timeout, lifetime, 0
        self.buckets = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def bounded_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            async with asyncio.timeout(self.send_timeout):
                await send(message)
        # Bound work before signature verification, including malformed bodies.
        # Separate new admissions from delivery for clients behind one proxy.
        admission = scope.get('path') in {'/v1/quote', '/v1/gossip', '/v1/credit/fund'}
        host = (scope.get('client') or ('unknown', 0))[0]
        key = (host, admission)
        now = time.monotonic()
        capacity, refill = (64, 16) if admission else (512, 256)
        credits, last = self.buckets.pop(key, (capacity, now))
        credits = min(capacity, credits + (now-last)*refill)
        self.buckets[key] = (max(0, credits-1), now)
        if len(self.buckets) > 4096:
            self.buckets.popitem(last=False)
        if credits < 1 or self.active >= self.max_active:
            status = 429 if credits < 1 else 503
            try:
                await JSONResponse({'detail': 'Request capacity exceeded'}, status)(scope, receive, bounded_send)
            except TimeoutError:
                pass
            return
        self.active += 1
        try:
            async with asyncio.timeout(self.lifetime):
                await self.app(scope, receive, bounded_send)
        except TimeoutError:
            if not started:
                try:
                    await JSONResponse({"detail": "Request deadline exceeded"}, 408)(scope, receive, bounded_send)
                except TimeoutError:
                    pass
            # After response headers, close the stream without a success marker.
        finally:
            self.active -= 1
