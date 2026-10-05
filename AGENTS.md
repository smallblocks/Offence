# Offence

Permissionless inference service, initially packaged for StartOS and Linux Docker.
The project name is Offence. Product requirements are in SPEC.md and the
source-backed network comparison is in docs/RESEARCH.md.

## Rules

- No central registry, admission authority, mandatory intermediary, or native asset.
- Each provider runs complete inference. Buyers select providers. Paid mainnet
  requires prepaid compute and explicit buyer consent to a one-batch minimum
  reservation charge. Unused allowance stays as supplier-local credit.
- Fractional rates use exact decimal strings and cumulative msat rounding, never per-token rounding.
- Paid mainnet funding requires an operator USD/BTC snapshot, whole-sat one-cent minimum, and buyer funding caps.
- Model manifests identify exact files independently of a hosting website.
- Model accuracy is a seller claim judged through buyer-local reputation and checks.
- Execution proof is optional, not a payment prerequisite. Signatures/hashes are not execution proof.
- Honor explicit proof-required buyer policies without silently downgrading them.
- Keep lab payment simulations and regtest separate from production.
- Keep task planning and result combination in the harness. Offence routes independent waves.
- Routing never relaxes model/trust policy; retry allowances cannot consume initial-task reservations.
- Supplier protection takes priority: bound admitted work, runtime privileges and backend access.
- The node-hosted gateway is text-only and free-lab-only. The standalone buyer app
  supports budgeted LND and NWC payments; neither passes through unsupported parameters.
- NWC inference limits exclude routing fees; the owner must accept wallet-managed fees and set a wallet allowance.
- NWC connection secrets are owner-only local files; preserve uncertain-payment recovery and never resend on timeout.
- Buyer owner and agent keys are separate. Agent requests cannot alter owner policy.
- Buyer launch binds to loopback and locks its data directory. Do not expose it publicly.
- StartOS initializes /data/runtime before running as UID 10001; preserve identity on upgrades.
- StartOS reports the requested readonly config mount as rw; root-owned permissions protect it.
- Live StartOS has NoNewPrivs=0; do not claim Docker hardening flags apply to LXC.
- No em dashes. Secrets belong in ignored runtime files, never source or logs.
- The operator explicitly approves GitHub commit, push and releases for Offence
  under smallblocks, as a project exception to the workspace Gitea-only rule.
- Docker Actions artifacts are downloadable image archives retained 30 days, not GHCR images or s9pk releases.
- Keep operational records in ignored .private/ and .startos/, never in public source.
- Do not deploy without a concrete review.
- Bump the package revision before every s9pk pack; use release preflight.
- start-cli 1.1.0 can pack without a Git commit (manifest gitHash is null). Record
  scripts/snapshot.py hashes for uncommitted builds; commit/push still require approval.
- StartOS exposes the interface over HTTPS on its assigned SSL port.
  Plain HTTP is available internally, not on the LAN by default.
- Run `.venv/bin/python -m pytest` and `npm run check` for relevant changes.
- Strike provider-key-v1 requires explicit buyer opt-in and supplier availability for key recovery; never claim offline decryption for hosted settlement.
- Prepaid output keys are private buyer-authenticated records, never public evidence.
- Recover uncertain wallet outcomes without resending; never release ambiguous payment exposure by age.
- Hosted keys and sessions survive routine cleanup; preserve the database with the node identity.
- End sessions using the handoff procedure and replace Current state, max 15 lines.
- Supplier security clearance requires effective GPU-runtime and network checks;
  application tests and the Offence container's privileges do not establish GPU isolation.

- GPU runtimes using numeric UIDs may also require named passwd/group entries.
- JIT runtimes need an executable bounded build directory; Docker tmpfs defaults may block it.
- Inspect container AutoRemove before relying on stop/rename for rollback preservation.

## Current state

- Published supplier remains 0.1.0:10; published buyer remains buyer-v0.1.0-alpha.2.
- Source implements prepaid admission, exact fractional pricing and four audit application fixes.
- Verification: 244 application tests and TypeScript pass; application changes are undeployed.
- Supplier security clearance remains incomplete; passing application tests is insufficient.
- Remaining gates include runtime lifecycle, alternate runtime, dependencies and client authentication.
- Cold-start isolation and coordinated application deployment remain outstanding.
- Operational evidence and deployment records remain in ignored .private/ and .startos/.
- Next: close runtime lifecycle gaps, complete containment checks and review application deployment.
