from .crypto import digest, verify
from .pricing import charge


def verify_delivery(package):
    """Verify bilateral acknowledgment, never model execution or unique humans."""
    q = verify(package["quote"])
    b = verify(package["batch"], package["quote"]["signer"])
    r = verify(package["receipt"], q["buyer"])
    header = b["sealed"]["header"]
    if (q.get("type") != "quote" or b.get("type") != "batch" or r.get("type") != "receipt"
            or q["session"] != header["session"] or q["session"] != r["session"]
            or q["model_id"] != header["model_id"] or q["request_hash"] != header["request_hash"]
            or r["batch_hash"] != digest(package["batch"]) or r["payment_hash"] != b.get("invoice_payment_hash", b["sealed"]["payment_hash"])
            or r["sequence"] != header["sequence"] or r["received_tokens"] != header["token_count"]
            or header["amount_msat"] != charge(q, header["total_tokens"]) - charge(q, header["total_tokens"]-header["token_count"])):
        raise ValueError("Bilateral evidence does not agree")
    return {"provider": package["quote"]["signer"], "buyer": q["buyer"], "model_id": q["model_id"],
            "acknowledged_tokens": header["token_count"], "execution_verified": False,
            "payment_settlement_verified": False, "sybil_resistant": False}
