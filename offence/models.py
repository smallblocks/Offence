from pathlib import Path
from typing import Literal
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .crypto import digest


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Artifact(Strict):
    path: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=2**53 - 1)
    role: Literal["weights", "tokenizer", "config", "adapter", "template", "runtime"]

    @model_validator(mode="after")
    def safe_path(self):
        p = Path(self.path)
        if p.is_absolute() or ".." in p.parts or "\\" in self.path:
            raise ValueError("Artifact path must be relative and contained")
        return self


class Manifest(Strict):
    format: Literal["offence/model/v1"] = "offence/model/v1"
    name: str = Field(min_length=1, max_length=128)
    architecture: str = Field(min_length=1, max_length=128)
    quantization: str = Field(min_length=1, max_length=128)
    context_tokens: int = Field(ge=1, le=10_000_000)
    artifacts: list[Artifact] = Field(min_length=1, max_length=256)
    # Mirrors are locators, excluded from identity. They grant no authority.
    sources: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unique_files(self):
        if len({a.path for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate artifact path")
        if "weights" not in {a.role for a in self.artifacts}:
            raise ValueError("Weights are required")
        return self

    @property
    def model_id(self):
        fields = self.model_dump(exclude={"sources"})
        fields["artifacts"] = sorted(fields["artifacts"], key=lambda a: a["path"])
        return digest(fields)

    def verify_files(self, root: Path):
        for artifact in self.artifacts:
            path = (root / artifact.path).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("Artifact escapes model directory")
            if path.stat().st_size != artifact.size:
                raise ValueError(f"Wrong size: {artifact.path}")
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != artifact.sha256:
                raise ValueError(f"Wrong hash: {artifact.path}")


class Offer(Strict):
    manifest: Manifest
    output_msat_per_token: int = Field(ge=0, le=1_000_000_000)
    output_msat_per_token_exact: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def valid_rate(self):
        from .pricing import rate
        rate(self)
        return self

    batch_tokens: int = Field(default=8, ge=1, le=128)
    max_output_tokens: int = Field(default=512, ge=1, le=32768)
    generation_deadline_s: int = Field(default=120, ge=1, le=3600)
    payment_timeout_s: int = Field(default=60, ge=5, le=600)
    proof: Literal["unavailable"] = "unavailable"
    available: bool = True
    text_chat: bool = False
    hardware: str | None = Field(default=None, max_length=256)


class Advertisement(Strict):
    type: Literal["advertisement"] = "advertisement"
    network: Literal["offence-lab-v1", "offence-v1"] = "offence-lab-v1"
    issued: int
    expires: int
    sequence: int = Field(ge=0)
    endpoint: str = Field(max_length=512)
    offer: Offer | None = None


class ChatMessage(Strict):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=16384)


class Request(Strict):
    type: Literal["request"] = "request"
    network: Literal["offence-lab-v1", "offence-v1"] = "offence-lab-v1"
    provider: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued: int
    prompt: str = Field(default="", max_length=16384)
    messages: list[ChatMessage] | None = Field(default=None, min_length=1, max_length=64)
    max_output_tokens: int = Field(ge=1, le=32768)
    max_total_msat: int = Field(ge=0, le=10**12)
    proof_policy: Literal["required", "lab-unverified", "seller-claim"] = "required"
    allow_provider_key_release: bool = False
    allow_prepaid_compute: bool = False
    fractional_billing: bool = False
    funding_limit_msat: int = Field(default=0, ge=0, le=10**12)

    @model_validator(mode="after")
    def input_shape(self):
        if bool(self.prompt) == bool(self.messages):
            raise ValueError("Supply either prompt or text messages")
        if self.messages and sum(len(m.content.encode()) for m in self.messages) > 16384:
            raise ValueError("Chat context exceeds byte limit")
        return self


class GatewayRoute(Strict):
    alias: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    endpoint: str = Field(max_length=512)
    provider: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_output_tokens: int = Field(default=512, ge=1, le=32768)


class RoutingPolicy(Strict):
    alias: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")
    # Ordered by the operator's preference, not a network-wide quality claim.
    model_ids: list[str] = Field(min_length=1, max_length=32)
    providers: list[str] = Field(default_factory=list, max_length=128)
    trusted_providers: list[str] = Field(default_factory=list, max_length=128)
    strategy: Literal["cheapest", "fastest", "preferred-model", "balanced"] = "balanced"
    privacy: Literal["any", "trusted-only"] = "any"
    max_output_tokens: int = Field(default=512, ge=1, le=32768)
    min_context_tokens: int = Field(default=1, ge=1, le=10000000)

    @model_validator(mode="after")
    def valid_identities(self):
        import re
        for values in (self.model_ids, self.providers, self.trusted_providers):
            if len(values) != len(set(values)) or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in values):
                raise ValueError("Policy identities must be unique SHA-256/public-key hex values")
        if self.privacy == "trusted-only" and not self.trusted_providers:
            raise ValueError("Private policy requires trusted providers")
        return self


class GatewayConfig(Strict):
    routes: list[GatewayRoute] = Field(default_factory=list, max_length=64)
    policies: list[RoutingPolicy] = Field(default_factory=list, max_length=32)
    max_active_jobs: int = Field(default=2, ge=1, le=8)
    max_job_tokens: int = Field(default=16384, ge=1, le=100000)
    max_job_retries: int = Field(default=1, ge=0, le=2)
    allow_free_lab: bool = False
    max_concurrent: int = Field(default=2, ge=1, le=16)
    daily_output_tokens: int = Field(default=10000, ge=1, le=10000000)
    request_deadline_s: int = Field(default=120, ge=1, le=3600)

    @model_validator(mode="after")
    def unique_routes(self):
        aliases = [r.alias for r in self.routes] + [p.alias for p in self.policies]
        if len(set(aliases)) != len(aliases):
            raise ValueError("Duplicate gateway alias")
        return self


class Pricing(Strict):
    mode: Literal["sats-per-token", "cents-per-kwh"]
    sats_per_token: str | None = None
    cents_per_kwh: str | None = None
    joules_per_token: str | None = None
    usd_per_btc: str | None = None

    def token_price(self):
        from .pricing import energy_price, sats_per_token
        if self.mode == "sats-per-token":
            return sats_per_token(self.sats_per_token)
        return energy_price(self.cents_per_kwh, self.joules_per_token,
                            self.usd_per_btc)["output_msat_per_token"]

    def exact_token_price(self):
        from .pricing import energy_price, sats_rate
        if self.mode == "sats-per-token":
            return sats_rate(self.sats_per_token)
        return energy_price(self.cents_per_kwh, self.joules_per_token, self.usd_per_btc)["output_msat_per_token_exact"]

    @model_validator(mode="after")
    def valid_price(self):
        self.token_price()
        self.exact_token_price()
        return self


class Config(Strict):
    endpoint: str = ""
    seeds: list[str] = Field(default_factory=list, max_length=32)
    allowed_private_peers: list[str] = Field(default_factory=list, max_length=32)
    tor_proxy: str | None = None
    backend: Literal["none", "fixture", "llamacpp", "vllm"] = "none"
    backend_url: str = ""
    backend_model: str = Field(default="", max_length=256)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    prepaid_compute: bool = False  # Regtest opt-in; paid mainnet always requires prepayment.
    max_requests_per_hour: int = Field(default=120, ge=1, le=100000)
    max_work_tokens_per_hour: int = Field(default=1000000, ge=1, le=100000000)
    max_storage_mb: int = Field(default=128, ge=16, le=4096)
    offer: Offer | None = None
    pricing: Pricing | None = None
    allow_lab_unverified: bool = False
    allow_seller_claim: bool = False
    share_receipts: bool = False
    lightning: Literal["disabled", "lnd-regtest", "lnd-mainnet", "strike"] = "disabled"
    strike_address: str = ""
    gossip_interval_s: int = Field(default=30, ge=1, le=3600)
    max_peers: int = Field(default=512, ge=1, le=4096)
    max_sessions: int = Field(default=2, ge=1, le=64)

    @model_validator(mode="after")
    def coherent(self):
        if self.lightning == "strike":
            from .strike import strike_address
            self.strike_address = strike_address(self.strike_address)
        if self.pricing and self.offer:
            self.offer.output_msat_per_token = self.pricing.token_price()
            self.offer.output_msat_per_token_exact = self.pricing.exact_token_price()
        if self.backend in {"llamacpp", "vllm"}:
            from urllib.parse import urlsplit
            p = urlsplit(self.backend_url)
            if (p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password
                    or p.path not in {"", "/"} or p.query or p.fragment or p.port == 0):
                raise ValueError("Backend requires a fixed HTTP(S) origin without credentials or paths")
        if self.backend == "vllm" and not self.backend_model:
            raise ValueError("vLLM requires an operator-selected served model")
        from .discovery import peer_url
        for route in self.gateway.routes:
            peer_url(route.endpoint, self.allowed_private_peers)
        if self.lightning == "lnd-regtest" and not self.allow_lab_unverified:
            raise ValueError("Regtest requires explicit lab opt-in")
        if self.lightning in {"lnd-mainnet", "strike"} and not self.allow_seller_claim:
            raise ValueError("Mainnet requires explicit seller-claim assurance")
        if self.lightning in {"lnd-mainnet", "strike"} and self.backend == "fixture":
            raise ValueError("Fixture backend cannot receive mainnet payments")
        if self.offer and self.lightning in {"lnd-mainnet", "strike"}:
            from .pricing import cent_deposit
            cent_deposit(self.pricing.usd_per_btc if self.pricing else None)
        return self

    @property
    def network(self):
        return "offence-v1" if self.lightning in {"lnd-mainnet", "strike"} else "offence-lab-v1"

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate(json.loads(path.read_text())) if path.exists() else cls()
