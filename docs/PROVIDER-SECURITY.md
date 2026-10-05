# Supplier protection boundary

The inference supplier is the primary protected party. A buyer purchases bounded
inference and never receives host, container, filesystem, GPU management, model
loading, shell or wallet authority. Prompt text remains data, including when it
contains commands, URLs or instructions to ignore previous messages.

## Enforced application controls

- Exact signed request, provider identity, model manifest and quote binding.
- Only plain text inputs; no arbitrary backend parameters, media fetches, tools,
  executable extensions, remote model paths or provider administration proxy.
- Fixed operator-configured backend origin and served model. No redirects or
  ambient HTTP proxy environment settings.
- 512 KiB public request limit and 10-second body deadline; bounded HTTP concurrency
  and per-source budgets enforced before body reads and signature verification,
  including invalid requests; 10-second blocked-send deadline and outer request lifetime.
- Supplier-selected session, output, generation and payment deadlines. Backend
  streams are explicitly closed on disconnect or failure, releasing local slots.
- Persistent, global rolling-hour admission quotas across buyer identities and
  restarts. Only accepted work reserves the full offered context capacity
  as its work allowance. Quotes reserve no GPU slots, durable session rows or work
  allowance. New buyers return signed quote/request pairs, so quote cache churn
  does not invalidate their acceptance.
- Persistent supplier-wide hourly funding-attempt budget and one invoice attempt
  per signed quote. Retries return a saved voucher or fail on an unresolved outcome.
- Bounded recovery pages; oversized legacy unpaged requests fail explicitly.
- HTTP compression is rejected before response decoding, including for peers,
  model backends and receiving wallets.
- Bounded backend responses, frames, token groups and encrypted batches. Refuse
  missing token IDs, substituted model names, unsupported output and invalid endings.
- SQLite page cap and storage admission watermark; buyer evidence has a separate
  64 MiB cap. These are logical application bounds, not an OS volume quota.
- No wallet in the agent gateway and zero allowed gateway spending. Direct paid
  CLI purchases require explicit wallet and assurance choices. Proof-required
  requests fail closed.

Defaults: 2 sessions, 120 admitted requests per rolling hour, 1,000,000 reserved work
tokens per rolling hour, and 128 MiB database cap. Operators should set these for
the capacity they are willing to donate to free lab testing. A large model context
can consume the conservative work quota quickly, even for short prompts.

## Deployment isolation

The Docker image runs as UID/GID 10001. `compose.yaml` adds a read-only root
filesystem, removes all capabilities, sets no-new-privileges, limits memory/CPU/
processes, and exposes HTTP on loopback. Runtime data is the writable named volume;
operator configuration is mounted read-only. Create `data/config.json` before
starting Compose. Put secrets in the shell environment or ignored `.env`.

StartOS uses a short-lived initialization process to move legacy identity,
database and buyer records into `runtime/` on the service volume. It refuses
symlinks, hard-linked files and conflicting identities, then starts the daemon as
`offence`. The daemon receives a requested read-only operator configuration mount and a
separate writable state mount. Verify the effective mount flags on the target:
StartOS may expose that configuration mount as rw, so root ownership and directory
permissions must independently prevent service-user writes. The initializer runs before the daemon, not through a public API.
Backup/restore includes the entire service volume. StartOS's LXC containment is a
separate deployment boundary; Docker Compose resource/security options are not
claimed to apply automatically to StartOS.

Keep the GPU server on a restricted network. Firewall it so only the provider
service and authorized administration systems can reach it. Offence's fixed-route
application policy is not an OS egress firewall. A compromised application process
still has the network privileges its host grants. Do not expose vLLM directly:
its API key does not authenticate every endpoint in current upstream versions.
Configure model/runtime isolation separately, including read-only model mounts,
restricted egress, no host management sockets, and no Lightning secrets.

## Limits and unresolved gates

Permissionless identities are cheap. Paid mainnet admission requires confirmed
prepaid credit. Acceptance atomically reserves the request ceiling and consumes
a minimum compute reservation charge of one batch, capped by that ceiling.
Unused allowance returns to the buyer's supplier-local balance, including after
restart. Quote-only and unfunded acceptance floods cannot reserve GPU resources.
This does not guarantee fair access against funded attacks or volumetric traffic.
The minimum is not a measured guarantee of covering long-context prefill cost;
operators must calibrate prices, work limits and deadlines for their hardware.
Free lab capacity remains a deliberate donation protected only by resource limits.
Closing an HTTP stream requests cancellation; only integration with the specific
runtime can establish how promptly GPU work stops. A runtime may compute beyond the output the buyer ultimately receives. Do not claim provider loss is bounded to one token
batch. Free lab inference deliberately offers no payment guarantee.

Ciphertext/payment binding, recovery and supplier payment assurance require
production validation. Cryptographic execution proof is optional; requests that
require it must refuse while it is unavailable. File hashes and provider
signatures are not execution proofs. Containers are not an absolute defense against
kernel/GPU-driver vulnerabilities. The project does not claim a completed security
audit or enforce a sandbox on an arbitrary buyer's local agent tools.

References: [vLLM API authentication](https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/),
[Docker security](https://docs.docker.com/engine/security/), and
[OWASP agent security](https://cheatsheetseries.owasp.org/cheatsheets/AI_Agent_Security_Cheat_Sheet.html).

## Split-job boundary

The buyer may submit independent subtask waves. Jobs share the ordinary chat quota
and concurrency limits and cannot override supplier admission. Retries are bounded
by a durable job allowance and cannot repeat partially delivered work. Request
cancellation does not count as a supplier reliability failure. No execution of
provider output, automatic cross-provider context sharing or dependency planning
is added to the provider process. See [Jobs](JOBS.md) for limits and privacy scope.
