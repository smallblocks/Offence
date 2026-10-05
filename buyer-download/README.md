# Offence Buyer

A local buyer app for the Offence provider graph. No GPU or supplier node required.
This is an experimental text-chat client, not a general tool-calling coding harness.

## Start

1. Install Python 3.12 or newer from https://www.python.org/downloads/.
2. Extract this entire download into a folder you own.
3. On macOS run `Start-Offence.command`; on Windows run `Start-Offence.cmd`;
   on Linux run `sh start-offence.sh`. A terminal alternative on any platform is
   `python3 launch.py` (Windows: `py -3 launch.py`).
4. The first launch installs pinned dependencies from PyPI into this folder.
   Subsequent launches reuse that environment. The local browser screen opens.
5. Refresh the graph, select model hashes and approve supplier signing keys verified
   through an independent channel. The default refuses unknown suppliers. A signed
   advertisement or low price alone is not evidence of trust.
   Explicitly choose an assurance mode. Requiring proof blocks requests because
   execution proofs are unavailable. Free lab and mainnet use separate networks.
6. Save the policy. Give your agent the displayed base URL, agent key and model
   `auto`. Never give it the private owner link or owner key.

Use `--no-browser` for headless operation or `--port 8788` for a different local port.
Close with Ctrl+C in the terminal. Keep the terminal running while your agent uses
inference. This is a Python-based download, not a signed native installer.

## Payment wallet

All spending defaults to zero. To connect a mainnet wallet:

1. In an NWC-compatible wallet, create a dedicated Offence connection with
   `get_info`, `pay_invoice` and `lookup_invoice` permissions. Set a wallet-side
   allowance that includes routing fees. Offence cannot read or verify that allowance.
2. Paste its private `nostr+walletconnect://` connection into the local app and
   choose **Connect wallet**. This checks access without sending a payment.
3. Choose the connected wallet, your inference limits in sats, and explicitly
   accept seller claims, wallet-managed fees and prepaid compute terms. Save to enable purchases.
4. Give your agent only its local API key. The browser can close after setup;
   the buyer process must remain running.

NWC does not offer a standard per-payment fee cap. Offence limits inference
charges, while the wallet controls routing fees and total wallet spending.
Fees are additional to the displayed NWC inference allowance. The connection
is stored privately in `~/.offence-buyer/wallet.nwc`, never returned by the API.
Disconnect stops purchases and removes the local connection unless it is needed
for uncertain-payment recovery. Revoke it in the wallet to invalidate all copies.
Recovery uses lookup only and never retries a payment automatically.
A receiving Lightning address does not grant spending authority.

### Advanced: direct LND

Free lab inference and LND regtest remain available. For LND mainnet, use a node
with usable outbound channel balance.

Set these environment variables locally before launching:

- `OFFENCE_LND_URL`: your wallet's HTTPS REST origin.
- `OFFENCE_LND_MACAROON_FILE`: path to a least-privilege payment-capable macaroon.
- `OFFENCE_LND_TLS_CERT_FILE`: path to the wallet's TLS certificate.

Then select the matching wallet network in the app and save to verify it. The
supplier never receives these credentials. Wallet errors leave spending blocked.
Buyer recovery reconciles existing payment intents at startup without resending.
Hosted supplier-key settlement requires explicit owner opt-in. Its key recovery
still requires that supplier; use `python -m offence.cli recover-hosted-keys` with
this app's purchases directory and the chosen endpoint/provider for recovery.

The UI shows sats with up to three decimal places; API amounts are integer
millisatoshis, where 1,000 msat is one sat. Paid mainnet suppliers require prepaid
compute. Mainnet funding starts at one US cent, rounded up to whole sats using
the supplier's disclosed USD/BTC snapshot. The deposit is credit, not a flat fee.
If existing credit is insufficient, funding covers at least that minimum or the
missing request allowance, whichever is larger. The per-request cap also caps
funding, and the daily cap must cover the payment before it is sent. The exchange
snapshot is not an independently verified live market feed. Larger requests can
require more than one cent; funds remain specific to the selected supplier.

Exact fractional token prices accumulate across chunks. Only the cumulative
request cost rounds up to a millisatoshi, so chunk size cannot multiply rounding.
Displayed token rates retain finer precision than settlement amounts. Confirmed
credit is bound to your buyer signing identity and reserved before GPU admission.
Acceptance consumes a minimum reservation charge equal to one output batch,
capped by the request maximum. It applies even if you disconnect or the backend
fails before producing output. The minimum counts toward subsequent output
charges, rather than being added to them. This is a compute purchase, not a
promise to refund every failed generation.

Funding is not a capacity reservation: if a quote expires or admission is busy,
the deposit remains supplier credit for a later request.

Unused allowance is returned to your balance with that supplier. There is no
automatic Lightning refund and credit cannot move between suppliers. The local
UI shows accounted credit and unresolved reservations. Back up your identity and
purchase records: a lost identity, supplier disappearance or supplier data loss
can make credit unrecoverable. Only prepay suppliers you choose to trust.

The daily allowance conservatively counts transferred funds and reuse of existing
credit, with LND fee caps when applicable. Deposits are not reported as output
charges. Unused quote reservations are released after a stopped request; only
actual payment exposure and unresolved credit usage remain reserved. Use
**Recover payment status** to query the original wallet and supplier without
sending funds. Unknown wallet outcomes never expire merely because time passed.
No automatic retry or fallback purchase occurs after a failed paid stream.
Failures trigger a local retry cooldown, not a public accusation against a supplier.

For interrupted non-streaming calls, HTTP 502 includes `partial_output`, a request
`id` and billing metadata. Retrieve saved text with authenticated
`GET /v1/purchases/{id}`, including after restarting the buyer.
`GET /v1/purchases` lists recent local request IDs if a connection failed before
you received the ID. Owner recovery can retrieve missing prepaid batches and keys
from the supplier in bounded pages, without paying again. Partial delivery
is never presented as a complete answer. Streaming errors omit the success marker.

The displayed token total counts verified output received by the local app, not
proof that a downstream agent consumed it. Output cost excludes routing fees and
payments whose output has not been recovered. Local signed purchase records are
authoritative evidence of partial and interrupted sessions.

## Security and privacy

The server binds only to 127.0.0.1, rejects foreign Host/Origin headers, and keeps
owner and agent keys separate. Agent requests cannot set endpoints, increase
budgets, run tools, or read files. Text returned by a supplier remains untrusted;
your agent harness must enforce its own tool permissions. This app is not an OS
sandbox against other software already running under your account.

State is stored under `~/.offence-buyer` by default. Back up the whole directory
privately, especially identity and purchase records. Never put it in a web folder
or source repository. Deleting it resets identity, evidence and spending history.
Do not run copies of this directory concurrently on different machines.

The default seed is https://offence.ai. Add other seeds and approve their exact
DNS/private origins locally. Public IP HTTPS and configured Tor origins follow
peer validation. A seed is not a mandatory intermediary or a trust authority. Use independent
introductions when available. Enabling unknown suppliers explicitly permits
prompt delivery to identities that a malicious seed can fabricate. Suppliers receive the
context you send; splitting a task does not make that context private.

API: authenticated `GET /v1/models`, `GET /v1/providers`, and
`POST /v1/chat/completions`. Text system/user/assistant messages, `max_tokens`,
`stream`, `temperature: 0`, and `n: 1` only. Input is bounded to 16 KiB. Tools,
images, arbitrary parameters and paid split-job execution are not supported.
