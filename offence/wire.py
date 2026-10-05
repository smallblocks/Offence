"""Reject HTTP compression before transport readers allocate decoded content."""


async def identity_bytes(response):
    # Peers can ignore Accept-Encoding. Check before HTTPX decompresses a body.
    encoding = response.headers.get('content-encoding', '').strip().lower()
    if encoding not in {'', 'identity'}:
        raise ValueError('Compressed HTTP responses are not supported')
    async for block in response.aiter_bytes():
        yield block
