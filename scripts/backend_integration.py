"""Run bounded, free integration checks against an operator-selected backend."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from offence.client import Buyer
from offence.crypto import Identity
from offence.models import Config, Manifest, Offer


def configuration(args, endpoint):
    if args.fixture:
        if args.manifest or args.backend_url or args.backend_model or args.model_directory:
            raise ValueError("Fixture mode cannot select a real backend or model")
        data = b"protocol fixture, not language model weights\n"
        manifest = Manifest(name="Protocol fixture", architecture="protocol-fixture",
            quantization="none", context_tokens=4096, artifacts=[{
                "path": "fixture.txt", "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "role": "weights"}])
    else:
        if not args.manifest or not args.backend_url:
            raise ValueError("A real backend requires a manifest and backend URL")
        manifest = Manifest.model_validate(json.loads(args.manifest.read_text()))
        if args.model_directory:
            manifest.verify_files(args.model_directory)
    return Config(endpoint=endpoint, seeds=[], allowed_private_peers=[endpoint],
        backend="fixture" if args.fixture else args.backend,
        backend_url=args.backend_url or "", backend_model=args.backend_model or "",
        lightning="disabled", allow_lab_unverified=True, share_receipts=False,
        max_sessions=1, max_requests_per_hour=12,
        max_work_tokens_per_hour=manifest.context_tokens * 12,
        offer=Offer(manifest=manifest, output_msat_per_token=0, batch_tokens=32,
                    max_output_tokens=64, generation_deadline_s=30))


async def exercise(config, directory, process):
    endpoint = config.endpoint
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise RuntimeError("Test provider exited before readiness")
            try:
                response = await client.get(endpoint + "/v1/status")
                response.raise_for_status()
                status = response.json()
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Test provider did not become ready")
                await asyncio.sleep(.1)
        if status["wallet"] != "disabled" or status["active_sessions"] != 0:
            raise RuntimeError("Unexpected provider state")
        buyer = Buyer(Identity(), directory / "buyer")

        def stream(prompt, tokens):
            return buyer.run(endpoint, status["identity"], config.offer.manifest.model_id,
                prompt, tokens, 0, allow_lab=True, detailed=True)

        async def completed(prompt):
            events = [event async for event in stream(prompt, 16)]
            deltas = [event for event in events if event["type"] == "delta"]
            count = sum(event["token_count"] for event in deltas)
            if (not events or events[-1]["type"] != "end" or not 0 < count <= 16
                    or any(event["amount_msat"] != 0 for event in deltas)):
                raise RuntimeError("Completion or exact token accounting check failed")
            return count

        first_tokens = await completed("The capital of France is")
        interrupted = stream("Count the positive integers in order, separated by commas:", 64)
        try:
            first = await anext(interrupted)
            if first["type"] != "delta" or first["token_count"] <= 0:
                raise RuntimeError("No output received before disconnect")
        finally:
            started = time.monotonic()
            await interrupted.aclose()
        deadline = time.monotonic() + 10
        while True:
            response = await client.get(endpoint + "/v1/status")
            response.raise_for_status()
            current = response.json()
            if current["active_sessions"] == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Provider session did not recover after disconnect")
            await asyncio.sleep(.1)
        released = round(time.monotonic() - started, 3)
        recovery_tokens = await completed("Two plus two equals")
        final = (await client.get(endpoint + "/v1/status")).json()
        if final["active_sessions"] != 0:
            raise RuntimeError("Provider retained an active session")
    return {"backend": config.backend, "completion_tokens": first_tokens,
            "recovery_tokens": recovery_tokens, "slot_release_seconds": released,
            "payments": "disabled", "gpu_cancellation_latency": "not_measured"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="store_true", help="Use CPU protocol data, not a model")
    parser.add_argument("--backend", choices=["vllm", "llamacpp"], default="vllm")
    parser.add_argument("--backend-url")
    parser.add_argument("--backend-model", help="Exact served model name for vLLM")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--model-directory", type=Path, help="Verify manifest files before requests")
    args = parser.parse_args()
    process = None
    result = None
    failure = None
    forced = False
    with tempfile.TemporaryDirectory(prefix="offence-backend-check-") as temporary:
        directory = Path(temporary)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        try:
            config = configuration(args, f"http://127.0.0.1:{port}")
            config_path = directory / "config.json"
            config_path.write_text(config.model_dump_json())
            # Do not inherit wallet settings, proxy settings or unrelated secrets.
            env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "SYSTEMROOT") if key in os.environ}
            env.update(HOME=str(directory), PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
            if not args.fixture and os.environ.get("OFFENCE_BACKEND_API_KEY"):
                env["OFFENCE_BACKEND_API_KEY"] = os.environ["OFFENCE_BACKEND_API_KEY"]
            with (directory / "provider.log").open("wb") as log:
                # Start Python directly so termination reaches the server.
                process = subprocess.Popen([sys.executable, "-m", "offence.cli", "serve",
                    "--config", str(config_path), "--data", str(directory / "provider"),
                    "--host", "127.0.0.1", "--port", str(port)],
                    cwd=ROOT, env=env, stdout=log, stderr=log)
                try:
                    result = asyncio.run(asyncio.wait_for(exercise(config, directory, process), timeout=120))
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        forced = True
                        process.kill()
                        process.wait(timeout=5)
        except (Exception, KeyboardInterrupt) as exc:
            # Exception text can contain backend addresses, prompts or private paths.
            failure = type(exc).__name__
    if failure:
        print(json.dumps({"status": "failed", "error_type": failure,
                          "forced_shutdown": forced}))
        return 1
    result.update(status="passed" if not forced else "failed", graceful_shutdown=not forced)
    print(json.dumps(result))
    return 1 if forced else 0


if __name__ == "__main__":
    raise SystemExit(main())
