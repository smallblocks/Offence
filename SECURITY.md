# Security and disclosure

Offence handles untrusted prompts, peer records and payment messages. Its model
identity claims are not execution proofs. Model output must remain untrusted data;
an agent harness must decide separately whether it may run tools or access files.
The inference service does not grant callers shell access or execute model tools.

Do not put credentials, payment preimages, private keys, prompts or server details
in a public issue. Use the repository's private vulnerability reporting facility
when available. If it is not enabled, request a private reporting channel without
including exploit details or secrets.

## Release review

The initial source and package review is complete for the experimental revision 10
preview. No production-safety claim is made.
Operator deployment history, local identities and infrastructure configuration are
excluded from the public source candidate. Publication checks cover the source candidate and package application payload.
Both architecture payloads match reviewed application files byte for byte.

Review findings and remaining gates:

- Two high-severity npm advisories remain in lint dependencies bundled inside the
  pinned StartOS SDK: brace-expansion and js-yaml. The SDK is a development-only
  dependency. Neither affected module is present in the inspected ncc bundle.
  npm runtime and locked Python dependency audits have no known findings in this
  review. The bundled development tooling still needs an upstream fix.
- Discovery advertisements and responses have byte limits, and remote cache
  saturation cannot suppress the local advertisement. Tests cover these bounds.
  Unpinned entries rotate at capacity and owner-pinned identities are protected.
  Configured seeds receive a guaranteed share of discovery attempts. Independent
  source quotas and general Sybil/eclipse resistance remain incomplete; buyers
  require approved keys by default, including old unrestricted settings unless
  the owner explicitly opts into unknown suppliers.
- The paid CLI rejects insufficient worst-case routing-fee budgets before supplier
  acceptance. Stopped purchases release unused quote capacity. Unknown dispatched payments
  and unresolved credit usage remain reserved until read-only recovery confirms
  their disposition. Paid failures trigger local retry cooldowns. See the historical docs/CORRECTIONS.md.
- Fractional token rates require exact cumulative rounding and prepaid settlement
  with explicit buyer capability. Regression checks cover the accounting; live
  settlement and deployment acceptance remain separate gates.
- Supplier isolation depends on its deployment environment as well as application
  checks. Docker hardening is not evidence of equivalent StartOS enforcement.
  This review does not establish OS, GPU driver or model-runtime containment.
- Live hosted-wallet invoice creation, settlement, backup/restore and provider
  disappearance recovery remain separate acceptance tests. An account-profile
  check does not establish these guarantees.
- Package metadata passes the private-identifier check. The restricted Docker
  image smoke test passes. The package is not yet validated on live StartOS.

The OpenAI-compatible gateway currently permits explicitly enabled free inference
only. Paid direct CLI use requires a buyer wallet; no public website checkout or
production paid OpenAI-compatible gateway is provided.

## Operator safeguards

Keep wallets and GPU management APIs private. Use receiving-only supplier wallet
permissions, separate buyer spending credentials, explicit spending limits, and
backups of identity plus settlement state. Never enable a real-money offer while
its backend or recovery path is unverified. Update dependency advisory checks for
every release; a clean scan cannot rule out unknown vulnerabilities.

## Standalone buyer app

The separate downloadable buyer app uses a loopback-only API with owner/agent
key separation, Host/Origin checks, explicit supplier/model policy and durable
spending limits. It supports LND regtest and opt-in mainnet payment code; mainnet
purchases have not been validated with live funds. The node-hosted gateway and
split-job API remain free-only. No endpoint grants model output tool execution.
An agent with filesystem or shell access under the owner's account is outside
this API boundary. Do not describe the app as an operating-system sandbox.

A fresh buyer discovers the public seed without sending inference prompts.
Availability still depends on suppliers advertising usable model offers. The
download has been installed on macOS and exercised on Linux; Windows launchers
are included but have not been validated. Runtime dependency audit reports no
known findings. Source and archive privacy checks must be repeated on releases.

## Prepaid supplier credit

Paid mainnet admission requires confirmed supplier-held credit and explicit buyer
consent. Funding vouchers bind buyer identity, provider, amount, invoice hash and
quote. Redemption checks the receiving wallet, not just a presented preimage, and
payment hashes can be credited only once. Funding, acceptance and credit recovery
never grant the supplier spending authority over the buyer wallet.

The acceptance transaction reserves credit and work together and consumes the
disclosed minimum compute charge. Rejecting admission cannot debit credit. Crash
recovery returns unused reservations while preserving recorded compute charges.
A disconnect before stream delivery still consumes the minimum, closing the
accept-and-abandon quota attack. Quotes and unpaid invoices reserve no GPU slots.

Unused funds stay as supplier credit; automated outbound refunds are not part of
this receiving-only wallet integration. This introduces supplier custody risk.
Signed balances are claims, not a cryptographic guarantee of solvency or refunds.
Paid output keys remain behind buyer-authenticated retrieval and are not published
in receipt evidence. Buyer and supplier identities and ledgers require backups.
These controls do not establish live wallet settlement, fair access under funded
attacks, or production capacity. No real funds are used by the regression suite.

## Request and response resource bounds

The outer ASGI guard admits requests before reading signed bodies or verifying
signatures. Bounded per-source budgets apply even to malformed requests, with
separate admission and delivery budgets. Reverse proxies share source budgets;
these application controls do not replace network traffic limits.

Funding records an attempt before contacting the receiving wallet. Retrying the
same current quote returns its saved voucher or an unresolved-outcome error,
including after restart. A persistent supplier-wide hourly invoice budget limits
new identities. This does not reconcile an invoice whose creation response was
lost, and it does not release buyer payment exposure.

Recovery is paged. Legacy unpaged requests that exceed one bounded page fail
explicitly instead of loading the full session. Remote HTTP readers request
identity encoding and reject compressed responses before decoding. Compatibility
with configured wallet and model services must be checked during deployment.
