# Testing a real inference backend

This opt-in runner starts a temporary loopback-only Offence provider, completes a
free request, disconnects a buyer after output arrives, and checks that another
request succeeds. It terminates the provider directly and fails if shutdown needs
forced termination. Provider and buyer files are deleted after the run.

First run the CPU fixture to check the runner itself:

```sh
.venv/bin/python scripts/backend_integration.py --fixture
```

For real inference, reserve the hardware through your existing allocation process
and start a compatible backend separately. Wait for model loading and kernel
initialization to finish. The backend must return exact token IDs. Use an isolated
environment with restricted networking, no wallet credentials, no management
sockets and no writable model mounts. A Nix shell alone provides no isolation.

```sh
.venv/bin/python scripts/backend_integration.py \
  --backend vllm \
  --backend-url http://127.0.0.1:8000 \
  --backend-model YOUR_SERVED_MODEL_NAME \
  --manifest manifest.json \
  --model-directory /read-only-model
```

For llama.cpp, select `--backend llamacpp` and omit `--backend-model`.
`--model-directory` verifies artifact sizes and hashes before requests. When the
backend runs elsewhere, verify its files there before running the test. Neither
hashes nor the served model name prove which computation the backend executes.
An optional `OFFENCE_BACKEND_API_KEY` is passed only to the test provider. It is
never included in the report. Do not send a key to an untrusted backend origin.

The runner uses no discovery seeds or wallet, charges zero, allows one session,
limits output to 64 tokens and generation to 30 seconds, and bounds admitted
work. The supplied manifest controls the context limit. It does not start or
stop the inference backend, enforce its resource limits, or configure its network.
Use backend-side limits appropriate for the hardware you reserved.

The JSON report contains token counts, observed provider slot release time and
shutdown status. It excludes prompts, output text, identities, backend addresses
and local paths. Failures return only an exception type; provider logs and
transient records are discarded. It does not measure GPU cancellation latency:
slot release is an application observation. A fast backend may finish before the
buyer disconnects. Backend metrics are needed to establish actual cancellation.

Exit status is zero only when all checks and graceful provider shutdown pass.
The CPU fixture verifies protocol plumbing; real backend compatibility requires
running the operator-selected backend. Live payments, sustained load, separate
buyer hosts and public discovery require additional tests.

## Backend counters and two-provider routing

The second runner starts two providers with separate identities and state, plus a
local gateway. Both providers use the same backend. It rejects a model excluded
by policy, interrupts one provider after partial output, checks that this work is
not replayed even with a retry allowance, and sends a new job to the surviving
provider. A policy trusting only the disappeared provider must reject the new job.
It kills only a provider process that it created. It never stops the backend.

First verify the runner with paced synthetic HTTP output:

```sh
.venv/bin/python scripts/resilience_integration.py --fixture --measure-cancellation
```

This fixture exercises process failure and counter parsing without a model or GPU.
For a real vLLM backend with exact token IDs and text chat support:

```sh
.venv/bin/python scripts/resilience_integration.py \
  --backend-url http://127.0.0.1:8000 \
  --backend-model YOUR_SERVED_MODEL_NAME \
  --manifest manifest.json \
  --model-directory /read-only-model \
  --measure-cancellation
```

Reserve an idle, exclusive backend before using `--measure-cancellation`. The
runner reads its `/metrics` on the configured origin without redirects or proxy
settings, selecting `model_name` exactly. If configured, `OFFENCE_BACKEND_API_KEY`
is also used for these requests to the same backend origin. Missing or invalid
counters fail the check. It requires observed running work before closing the buyer, then samples
running/waiting gauges and generation counters until three successive idle
samples have a stable token count. It reports the first stable idle sample,
confirmation time, total generated tokens, and tokens generated since the sample
just before closing. Sampling and metric update intervals limit these values;
none measures the instant GPU kernels stop. Concurrent work invalidates attribution.
Omit this option if you only want routing checks or the backend lacks metrics.

These checks allow up to 512 output tokens per request, batches of eight, a
60-second generation deadline, and a 180-second overall exercise limit. Backend
resource limits remain the operator's responsibility. A backend finishing before
interruption fails the check because cancellation was not observed. Each provider
admits at most 24 requests per hour and bounds work using the manifest context.
Payments and discovery are disabled. Logs, identities and transient job records
are deleted; JSON output contains only counts, timing and check outcomes.

Two provider processes sharing one backend test routing and provider failure.
They do not test independent GPU hosts, backend loss, cross-host networking,
public discovery, paid settlement or sustained load. Run those separately with
hardware and network allocations approved by their operators.
