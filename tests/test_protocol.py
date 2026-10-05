import json
import sqlite3

import httpx
import pytest

from offence.app import create_app
from offence.client import Buyer
from offence.crypto import Identity, digest, unb64, unseal, verify
from offence.protocol import ProofUnavailable
from conftest import request


def setup(tmp_path, config):
    app = create_app(tmp_path / "provider", config, background=False)
    return app, app.state.provider, Identity()


def accept(provider, buyer, quote):
    return provider.accept(buyer.sign({"type": "accept", "session": quote["body"]["session"],
                                       "quote_hash": digest(quote)}))


def test_proof_is_required_and_cannot_be_overridden_by_offer(config, manifest, tmp_path):
    _, p, b = setup(tmp_path, config)
    with pytest.raises(ProofUnavailable):
        p.quote(request(b, p.identity.public, manifest, proof_policy="required"))
    config.allow_lab_unverified = False
    with pytest.raises(ProofUnavailable):
        p.quote(request(b, p.identity.public, manifest))
    assert p.store.evidence()["sessions"] == {}


def test_quotes_bind_price_model_buyer_and_prevent_replay(config, manifest, tmp_path):
    _, p, b = setup(tmp_path, config)
    req = request(b, p.identity.public, manifest)
    q = p.quote(req)
    assert p.quote(req)['body']['session'] == q['body']['session']
    impostor = Identity()
    with pytest.raises(ValueError):
        accept(p, impostor, q)
    assert accept(p, b, q)[0] == q
    with pytest.raises(ValueError):
        accept(p, b, q)


@pytest.mark.parametrize("changes", [{"max_total_msat": 1}, {"model_id": "0" * 64}, {"max_output_tokens": 10000}])
def test_quote_rejects_invalid_terms(config, manifest, tmp_path, changes):
    config.offer.output_msat_per_token = 10
    _, p, b = setup(tmp_path, config)
    with pytest.raises(ValueError):
        p.quote(request(b, p.identity.public, manifest, **changes))


async def test_free_stream_receipts_recovery_and_buyer_data(config, manifest, tmp_path):
    app, p, b = setup(tmp_path, config)
    buyer = Buyer(b, tmp_path / "buyer")
    output = [part async for part in buyer.run("http://provider", p.identity.public, manifest.model_id,
             "hello", 8, 0, allow_lab=True, transport=httpx.ASGITransport(app=app))]
    assert "".join(output) == "This is a lab fixture."
    assert len(output) == 3
    assert p.store.evidence()["buyer_receipts"] == 3
    assert p.store.evidence()["sessions"] == {"complete": 1}
    from offence.evidence import verify_delivery
    packages = p.store.evidence_packages()
    assert len(packages) == 3
    assert sum(verify_delivery(p)["acknowledged_tokens"] for p in packages) == 5
    assert verify_delivery(packages[0])["execution_verified"] is False
    quote_file = next((tmp_path / "buyer").glob("*.quote.json"))
    q = json.loads(quote_file.read_text())
    recovered = p.store.recover(q["body"]["session"], b.public)
    assert len(recovered["batches"]) == 3
    with pytest.raises(ValueError):
        p.store.recover(q["body"]["session"], Identity().public)


async def test_backend_failure_preserves_already_delivered_tokens(config, manifest, tmp_path):
    class Broken:
        async def stream(self, *args):
            yield {"token_ids": [1, 2], "text": "partial"}
            raise RuntimeError("failed")
    _, p, b = setup(tmp_path, config)
    p.backend = Broken()
    quote = p.quote(request(b, p.identity.public, manifest))
    q, req = accept(p, b, quote)
    stream = [x async for x in p.stream(q, req)]
    assert stream[0]["body"]["type"] == "batch"
    assert stream[1]["body"]["type"] == "error"
    batch = stream[0]["body"]
    assert unseal(batch["sealed"], unb64(batch["free_key"]))["groups"][0]["text"] == "partial"
    assert p.active == 0


async def test_budget_frozen_at_quote(config, manifest, tmp_path):
    _, p, b = setup(tmp_path, config)
    quote = p.quote(request(b, p.identity.public, manifest))
    config.offer.output_msat_per_token = 1000
    q, req = accept(p, b, quote)
    messages = [x async for x in p.stream(q, req)]
    assert all(x["body"]["sealed"]["header"]["amount_msat"] == 0 for x in messages[:-1])


async def test_receipt_cannot_be_forged_or_recounted(config, manifest, tmp_path):
    _, p, b = setup(tmp_path, config)
    quote = p.quote(request(b, p.identity.public, manifest))
    q, req = accept(p, b, quote)
    batch = [x async for x in p.stream(q, req)][0]
    body = {"type": "receipt", "session": q["body"]["session"], "sequence": 0,
            "batch_hash": digest(batch), "payment_hash": batch["body"]["sealed"]["payment_hash"], "received_tokens": 2}
    with pytest.raises(ValueError):
        p.store.receipt(Identity().sign(body))
    p.store.receipt(b.sign(body))
    p.store.receipt(b.sign(body))
    assert p.store.evidence()["buyer_receipts"] == 1
    body["received_tokens"] = 200
    with pytest.raises(ValueError):
        p.store.receipt(b.sign(body))


async def test_api_proof_gate_returns_clear_error(config, manifest, tmp_path):
    app, p, b = setup(tmp_path, config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://node") as client:
        r = await client.post("/v1/quote", json=request(b, p.identity.public, manifest, proof_policy="required"))
        assert r.status_code == 412
        assert (await client.get("/health")).json()["production_payments"] == "blocked"
        assert (await client.post("/v1/gossip", content=b"x" * 600000)).status_code == 413


async def test_buyer_defaults_to_proof_required(tmp_path):
    buyer = Buyer(Identity(), tmp_path)
    with pytest.raises(ValueError, match="Execution proof"):
        _ = [x async for x in buyer.run("http://unused", "0" * 64, "0" * 64, "hi", 1, 0)]
