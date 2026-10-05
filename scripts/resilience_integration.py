"""Check disconnect counters and routing with two temporary, free providers."""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, StreamingResponse

from backend_integration import ROOT, configuration
from offence.app import create_app
from offence.client import Buyer
from offence.crypto import Identity
from offence.models import Advertisement, Config, GatewayConfig, RoutingPolicy


def require(condition):
    if not condition:
        raise RuntimeError("Integration expectation failed")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def parse_metrics(body, model):
    """Select only the served model; missing counters must not look like idle."""
    result = {}
    for name in ("generation_tokens_total", "num_requests_running", "num_requests_waiting"):
        values = []
        for line in body.splitlines():
            match = re.fullmatch(r"vllm:" + name + r'\{(.*)\}\s+(\S+)', line)
            if not match:
                continue
            label = re.search(r'(?:^|,)\s*model_name=("(?:[^"\\]|\\.)*")(?=,|$)', match[1])
            if label and json.loads(label[1]) == model:
                value = float(match[2])
                require(math.isfinite(value) and value >= 0)
                values.append(value)
        require(bool(values))
        result[name] = sum(values)
    return result


async def cancellation(config, identity, directory, client, use_api_key=True):
    headers = {}
    if use_api_key and os.environ.get("OFFENCE_BACKEND_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["OFFENCE_BACKEND_API_KEY"]

    async def metrics():
        body = bytearray()
        async with client.stream("GET", config.backend_url.rstrip("/") + "/metrics", headers=headers) as response:
            response.raise_for_status()
            async for block in response.aiter_bytes():
                require(len(body) + len(block) <= 2 * 1024 * 1024)
                body.extend(block)
        return parse_metrics(body.decode("utf-8"), config.backend_model)

    before = await metrics()
    require(before["num_requests_running"] == before["num_requests_waiting"] == 0)
    buyer = Buyer(Identity(), directory / "buyer")
    stream = buyer.run(config.endpoint, identity.public, config.offer.manifest.model_id,
        "Count positive integers in order, separated by commas, and continue:",
        512, 0, allow_lab=True, detailed=True)
    try:
        first = await anext(stream)
        require(first["type"] == "delta" and first["token_count"] > 0)
        pre_close = await metrics()
        require(pre_close["num_requests_running"] > 0)
    finally:
        started = time.monotonic()
        await stream.aclose()
    samples = []
    for _ in range(100):
        current = await metrics()
        samples.append({"seconds": round(time.monotonic() - started, 3), **current})
        recent = samples[-3:]
        if (len(recent) == 3 and all(s["num_requests_running"] == s["num_requests_waiting"] == 0
                for s in recent) and len({s["generation_tokens_total"] for s in recent}) == 1):
            break
        await asyncio.sleep(.05)
    else:
        raise RuntimeError("Backend did not settle after disconnect")
    generated = current["generation_tokens_total"] - before["generation_tokens_total"]
    after_close = current["generation_tokens_total"] - pre_close["generation_tokens_total"]
    require(0 < generated < 512 and after_close >= 0)
    return {"delivered_first_batch_tokens": first["token_count"],
        "generated_tokens": generated, "generated_since_pre_close_sample": after_close,
        "first_stable_idle_sample_seconds": samples[-3]["seconds"],
        "stable_idle_confirmation_seconds": samples[-1]["seconds"],
        "gpu_kernel_stop_time": "not_measured"}


def fixture_app():
    """A paced HTTP protocol fixture, with no model or GPU computation."""
    app = FastAPI()
    counters = {"generation_tokens_total": 0, "num_requests_running": 0, "num_requests_waiting": 0}

    @app.post("/tokenize")
    async def tokenize():
        return {"count": 1}

    @app.get("/metrics", response_class=PlainTextResponse)
    async def metrics():
        return "\n".join(f'vllm:{key}{{model_name="protocol-fixture"}} {value}'
                         for key, value in counters.items()) + "\n"

    @app.post("/v1/completions")
    @app.post("/v1/chat/completions")
    async def completion(payload: dict):
        async def events():
            counters["num_requests_running"] += 1
            try:
                for index in range(payload["max_tokens"]):
                    await asyncio.sleep(.02)
                    counters["generation_tokens_total"] += 1
                    choice = {"index": 0, "token_ids": [index], "delta": {"content": "x"},
                              "text": "x", "finish_reason": None}
                    yield "data: " + json.dumps({"model": "protocol-fixture", "choices": [choice]}) + "\n\n"
                choice = {"index": 0, "finish_reason": "length"}
                yield "data: " + json.dumps({"model": "protocol-fixture", "choices": [choice]}) + "\n\n"
                yield "data: [DONE]\n\n"
            finally:
                counters["num_requests_running"] -= 1
        return StreamingResponse(events(), media_type="text/event-stream")
    return app


async def start_server(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    task = asyncio.create_task(server.serve())
    for _ in range(150):
        if task.done():
            await task
            raise RuntimeError("Local server exited")
        if server.started:
            return server, task
        await asyncio.sleep(.1)
    server.should_exit = True
    await task
    raise RuntimeError("Local server did not start")


async def exercise(args, directory, processes, logs, servers):
    ports = [free_port() for _ in range(3)]
    urls = [f"http://127.0.0.1:{port}" for port in ports]
    base = configuration(args, urls[0])
    if args.fixture:
        port = free_port()
        servers.append(await start_server(fixture_app(), port))
        base.backend = "vllm"
        base.backend_url = f"http://127.0.0.1:{port}"
        base.backend_model = "protocol-fixture"
    require(bool(base.backend_model))
    base.offer.batch_tokens = 8
    base.offer.max_output_tokens = 512
    base.offer.generation_deadline_s = 60
    base.offer.text_chat = True
    base.max_requests_per_hour = 24
    base.max_work_tokens_per_hour = base.offer.manifest.context_tokens * 24
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(directory), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    if not args.fixture and os.environ.get("OFFENCE_BACKEND_API_KEY"):
        env["OFFENCE_BACKEND_API_KEY"] = os.environ["OFFENCE_BACKEND_API_KEY"]
    identities, configs = [], []
    for index in range(2):
        data = directory / f"provider-{index}"
        identity = Identity.load(data / "identity.key")
        identities.append(identity)
        config = base.model_copy(deep=True)
        config.endpoint, config.allowed_private_peers = urls[index], urls[:2]
        configs.append(config)
        path = data / "config.json"
        path.write_text(config.model_dump_json())
        log = (directory / f"provider-{index}.log").open("wb")
        logs.append(log)
        processes.append(subprocess.Popen([sys.executable, "-m", "offence.cli", "serve",
            "--config", str(path), "--data", str(data), "--host", "127.0.0.1", "--port", str(ports[index])],
            cwd=ROOT, env=env, stdout=log, stderr=log))
    report = {"mode": "protocol-fixture" if args.fixture else "real-backend", "providers": 2,
              "shared_backend": True, "payments": "disabled"}
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
        for index, url in enumerate(urls[:2]):
            for _ in range(150):
                require(processes[index].poll() is None)
                try:
                    response = await client.get(url + "/v1/status")
                    response.raise_for_status()
                    require(response.json()["wallet"] == "disabled")
                    break
                except httpx.HTTPError:
                    await asyncio.sleep(.1)
            else:
                raise RuntimeError("Provider did not start")
        if args.measure_cancellation:
            report["cancellation"] = await cancellation(configs[0], identities[0], directory, client, use_api_key=not args.fixture)
    key = secrets.token_hex(32)
    os.environ["OFFENCE_GATEWAY_API_KEY"] = key
    mid = base.offer.manifest.model_id
    cfg = Config(allowed_private_peers=urls[:2], gateway=GatewayConfig(allow_free_lab=True, policies=[
        RoutingPolicy(alias="balanced", model_ids=[mid], max_output_tokens=512),
        RoutingPolicy(alias="wrong-model", model_ids=["0" * 64]),
        RoutingPolicy(alias="trusted-only", model_ids=[mid], trusted_providers=[identities[0].public], privacy="trusted-only")]))
    app = create_app(directory / "gateway", cfg, background=False)
    for index in range(2):
        ad = Advertisement(issued=int(time.time()), expires=int(time.time()) + 180,
            sequence=1, endpoint=urls[index], offer=configs[index].offer)
        app.state.store.ingest(identities[index].sign(ad.model_dump()))
    servers.append(await start_server(app, ports[2]))
    try:
        async with httpx.AsyncClient(base_url=urls[2], headers={"Authorization": "Bearer " + key},
                                    trust_env=False, timeout=10) as client:
            async def submit(alias, tokens, retries=0):
                payload = {"idempotency_key": secrets.token_hex(16), "tasks": [{"id": "task", "model": alias,
                    "messages": [{"role": "user", "content": "Count positive integers, separated by commas, and continue:"}],
                    "max_tokens": tokens}], "max_total_output_tokens": tokens * (retries + 1),
                    "max_retries": retries, "proof_policy": "lab-unverified"}
                for _ in range(30):
                    response = await client.post("/v1/jobs", json=payload)
                    if response.status_code != 429:
                        return response
                    await asyncio.sleep(.5)
                raise RuntimeError("Job submission remained rate limited")

            async def poll(jid):
                for _ in range(30):
                    response = await client.get("/v1/jobs/" + jid)
                    if response.status_code != 429:
                        response.raise_for_status()
                        return response.json()
                    await asyncio.sleep(.5)
                raise RuntimeError("Job status remained rate limited")

            async def finished(jid):
                for _ in range(300):
                    result = await poll(jid)
                    if result["state"] not in {"queued", "running"}:
                        return result
                    await asyncio.sleep(.1)
                raise RuntimeError("Job did not finish")

            require((await submit("wrong-model", 16)).status_code == 409)
            response = await submit("balanced", 512, 1)
            require(response.status_code == 202)
            jid = response.json()["id"]
            for _ in range(600):
                partial = await poll(jid)
                if partial["received_output_tokens"] > 0 and partial["state"] == "running":
                    break
                require(partial["state"] in {"queued", "running"})
                await asyncio.sleep(.1)
            else:
                raise RuntimeError("No partial output observed")
            provider = partial["tasks"][0]["attempts"][0]["provider"]
            index = next(i for i, identity in enumerate(identities) if identity.public == provider)
            processes[index].kill()  # Failure injection only into a provider created by this runner.
            processes[index].wait(timeout=5)
            result = await finished(jid)
            require(result["state"] == "partial" and result["received_output_tokens"] > 0)
            require(len(result["tasks"][0]["attempts"]) == 1)
            require(result["tasks"][0]["attempts"][0]["model_id"] == mid and result["spent_msat"] == 0)
            response = await submit("balanced", 16, 1)
            require(response.status_code == 202)
            recovered = await finished(response.json()["id"])
            require(recovered["state"] == "complete" and recovered["spent_msat"] == 0)
            require(recovered["tasks"][0]["attempts"][-1]["provider"] == identities[1-index].public)
            require(all(a["model_id"] == mid for a in recovered["tasks"][0]["attempts"]))
            cfg.gateway.policies[2].trusted_providers = [identities[index].public]
            require((await submit("trusted-only", 16)).status_code == 409)
            report["routing"] = {"partial_delivered_tokens": result["received_output_tokens"],
                "partial_attempts": 1, "partial_work_replayed": False, "new_job_used_survivor": True,
                "model_restriction_preserved": True, "trust_restriction_preserved": True, "spent_msat": 0}
    finally:
        await app.state.jobs.shutdown()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--backend", choices=["vllm"], default="vllm")
    parser.add_argument("--backend-url")
    parser.add_argument("--backend-model")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model-directory", type=Path)
    parser.add_argument("--measure-cancellation", action="store_true", help="Require idle, exclusive backend and model metrics")
    args = parser.parse_args()
    processes, logs, servers = [], [], []
    original_key = os.environ.get("OFFENCE_GATEWAY_API_KEY")
    failure, forced, report = None, False, {}
    with tempfile.TemporaryDirectory(prefix="offence-resilience-check-") as temporary:
        async def run():
            try:
                return await asyncio.wait_for(exercise(args, Path(temporary), processes, logs, servers), 180)
            finally:
                for server, task in reversed(servers):
                    server.should_exit = True
                    try:
                        await asyncio.wait_for(task, 10)
                    except asyncio.TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        raise RuntimeError("Local server shutdown timed out")
        try:
            report = asyncio.run(run())
        except (Exception, KeyboardInterrupt) as exc:
            failure = type(exc).__name__  # Never publish exception text or logs.
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        forced = True
                        process.kill()
                        process.wait(timeout=5)
            for log in logs:
                log.close()
            if original_key is None:
                os.environ.pop("OFFENCE_GATEWAY_API_KEY", None)
            else:
                os.environ["OFFENCE_GATEWAY_API_KEY"] = original_key
    if failure or forced:
        print(json.dumps({"status": "failed", "error_type": failure, "forced_cleanup": forced}))
        return 1
    report.update(status="passed", graceful_survivor_shutdown=True)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
