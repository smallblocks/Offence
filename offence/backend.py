"""Token-ID preserving backends. An SSE message is not necessarily one token."""
import asyncio
import json
import os

import httpx
from .wire import identity_bytes


class Fixture:
    """A protocol fixture, never advertised as a real language model."""
    async def stream(self, prompt, max_tokens):
        for index, word in enumerate(["This ", "is ", "a ", "lab ", "fixture."][:max_tokens]):
            await asyncio.sleep(0)
            yield {"token_ids": [index], "text": word}


class LlamaCpp:
    def __init__(self, url, context_tokens):
        self.url, self.context_tokens = url.rstrip("/"), context_tokens

    async def stream(self, prompt, max_tokens):
        headers = {"Accept-Encoding": "identity"}
        if os.getenv("OFFENCE_BACKEND_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["OFFENCE_BACKEND_API_KEY"]
        async with httpx.AsyncClient(timeout=60, trust_env=False, headers=headers,
                                     follow_redirects=False) as client:
            tokenized = await bounded_json(client, self.url + "/tokenize", {"content": prompt, "add_special": True})
            if len(tokenized["tokens"]) + max_tokens > self.context_tokens:
                raise ValueError("Prompt and output exceed model context")
            async with client.stream("POST", self.url + "/completion", json={
                "prompt": prompt, "n_predict": max_tokens, "stream": True, "return_tokens": True,
                "temperature": 0, "cache_prompt": False}) as response:
                response.raise_for_status()
                buffer = b""
                total = 0
                stopped = False
                async for chunk in identity_bytes(response):
                    buffer += chunk
                    if len(buffer) > 256 * 1024:
                        raise ValueError("Backend SSE frame too large")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.startswith(b"data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == b"[DONE]":
                            continue
                        data = json.loads(raw)
                        if data.get("error"):
                            raise ValueError("Backend inference failed")
                        token_ids = data.get("tokens", [])
                        text = data.get("content", "")
                        if (not isinstance(token_ids, list) or not isinstance(text, str)
                                or any(type(t) is not int or t < 0 for t in token_ids)
                                or len(text.encode()) > 65536):
                            raise ValueError("Backend returned invalid token data")
                        if text and not token_ids:
                            raise ValueError("Backend omitted exact token IDs; refusing estimated billing")
                        total += len(token_ids)
                        if total > max_tokens:
                            raise ValueError("Backend exceeded output budget")
                        if token_ids:
                            yield {"token_ids": token_ids, "text": text}
                        if data.get("stop") is True:
                            stopped = True
                if not stopped:
                    raise ValueError("Backend disconnected without a completion marker")


async def batches(source, limit):
    """Never split text from its token group or call characters tokens."""
    from .crypto import canonical
    groups, count, encoded_size = [], 0, 0
    async for group in source:
        size = len(group["token_ids"])
        if not size:
            continue
        if size > limit:
            raise ValueError("Backend token group exceeds negotiated batch size")
        wire_size = len(canonical(group))
        if wire_size > 128 * 1024:
            raise ValueError("Backend token group exceeds byte budget")
        if count + size > limit or encoded_size + wire_size > 128 * 1024:
            yield groups
            groups, count, encoded_size = [], 0, 0
        groups.append(group)
        count += size
        encoded_size += wire_size
        if count == limit:
            yield groups
            groups, count, encoded_size = [], 0, 0
    if groups:
        yield groups


async def bounded_json(client, path, payload):
    """Backend responses are untrusted, even on an operator-configured LAN."""
    async with client.stream("POST", path, json=payload) as response:
        response.raise_for_status()
        raw = bytearray()
        async for block in identity_bytes(response):
            raw.extend(block)
            if len(raw) > 512 * 1024:
                raise ValueError("Backend response too large")
        return json.loads(raw)


class Vllm:
    """Text-only adapter. No passthrough parameters, tools, URLs or model loading."""
    def __init__(self, url, model, context_tokens):
        self.url, self.model, self.context_tokens = url.rstrip("/"), model, context_tokens

    def stream(self, prompt, max_tokens):
        return self._stream({"prompt": prompt}, max_tokens, False)

    def stream_chat(self, messages, max_tokens):
        return self._stream({"messages": messages}, max_tokens, True)

    async def _stream(self, content, max_tokens, chat):
        headers = {"Accept-Encoding": "identity"}
        if os.getenv("OFFENCE_BACKEND_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["OFFENCE_BACKEND_API_KEY"]
        async with httpx.AsyncClient(base_url=self.url, headers=headers, timeout=60,
                                     trust_env=False, follow_redirects=False) as client:
            tokenized = await bounded_json(client, "/tokenize", {
                "model": self.model, **content, **({"add_generation_prompt": True} if chat else {})})
            count = tokenized.get("count")
            if type(count) is not int or count < 0 or count + max_tokens > self.context_tokens:
                raise ValueError("Prompt and output exceed model context or invalid token count")
            payload = {"model": self.model, **content, "max_tokens": max_tokens,
                       "stream": True, "temperature": 0, "n": 1, "return_token_ids": True}
            path = "/v1/chat/completions" if chat else "/v1/completions"
            async with client.stream("POST", path, json=payload) as response:
                response.raise_for_status()
                buffer, total, finished, done = b"", 0, False, False
                async for block in identity_bytes(response):
                    buffer += block
                    if len(buffer) > 256 * 1024:
                        raise ValueError("Backend SSE frame too large")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.startswith(b"data:"):
                            continue
                        raw = line[5:].strip()
                        if done:
                            raise ValueError("Backend data after completion")
                        if raw == b"[DONE]":
                            done = True
                            continue
                        data = json.loads(raw)
                        if data.get("error") or data.get("model") != self.model:
                            raise ValueError("Backend error or model name mismatch")
                        choices = data.get("choices")
                        if not isinstance(choices, list) or len(choices) != 1:
                            raise ValueError("Expected exactly one completion choice")
                        choice = choices[0]
                        if choice.get("index") != 0 or finished:
                            raise ValueError("Unexpected completion choice")
                        delta = choice.get("delta", {}) if chat else {}
                        if any(delta.get(k) for k in ("tool_calls", "function_call", "reasoning", "reasoning_content", "refusal")):
                            raise ValueError("Backend emitted unsupported non-text output")
                        text = (delta.get("content") if chat else choice.get("text")) or ""
                        ids = choice.get("token_ids") or []
                        if (not isinstance(text, str) or not isinstance(ids, list)
                                or any(type(t) is not int or t < 0 for t in ids)
                                or len(text.encode()) > 65536 or (text and not ids)):
                            raise ValueError("Invalid backend text or missing exact token IDs")
                        total += len(ids)
                        if total > max_tokens:
                            raise ValueError("Backend exceeded output budget")
                        if ids:
                            yield {"token_ids": ids, "text": text}
                        reason = choice.get("finish_reason")
                        if reason is not None:
                            if reason not in {"stop", "length"}:
                                raise ValueError("Unsupported completion reason")
                            finished = True
                            yield {"token_ids": [], "text": "", "finish_reason": reason}
                if buffer.strip() or not finished or not done:
                    raise ValueError("Backend disconnected without completion")
