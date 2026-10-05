import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from .crypto import Identity
from .models import Manifest


def main():
    parser = argparse.ArgumentParser(description="Offence lab node and buyer")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--data", type=Path, default=Path("data"))
    serve.add_argument("--config", type=Path, help="Read-only operator configuration outside runtime data")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    check = sub.add_parser("verify-model")
    check.add_argument("manifest", type=Path)
    check.add_argument("directory", type=Path)
    manifest = sub.add_parser("manifest", help="Hash a GGUF containing weights and tokenizer")
    manifest.add_argument("file", type=Path)
    manifest.add_argument("--name", required=True)
    manifest.add_argument("--architecture", required=True)
    manifest.add_argument("--quantization", required=True)
    manifest.add_argument("--context", type=int, required=True)
    buy = sub.add_parser("buy")
    buy.add_argument("endpoint")
    buy.add_argument("--provider", required=True)
    buy.add_argument("--model-id", required=True)
    buy.add_argument("--prompt", required=True)
    buy.add_argument("--max-tokens", type=int, default=64)
    buy.add_argument("--max-msat", type=int, default=0)
    buy.add_argument("--funding-limit-msat", type=int, help="Maximum prepaid funding, defaults to --max-msat; must cover the rounded one-cent deposit")
    buy.add_argument("--fee-limit-msat", type=int, default=1000, help="Maximum routing fee per batch")
    buy.add_argument("--data", type=Path, default=Path("data/buyer"))
    buy.add_argument("--allow-lab-unverified", action="store_true")
    wallet_mode = buy.add_mutually_exclusive_group()
    wallet_mode.add_argument("--lnd-regtest", action="store_true")
    wallet_mode.add_argument("--lnd-mainnet", action="store_true")
    buy.add_argument("--assurance", choices=["required", "seller-claim", "lab-unverified"])
    buy.add_argument("--total-fee-limit-msat", type=int, default=1000)
    buy.add_argument("--daily-limit-msat", type=int, default=0)
    buy.add_argument("--tor-proxy")
    buy.add_argument('--allow-prepaid-compute', action='store_true', help='Accept supplier-held credit and the minimum compute reservation charge')
    buy.add_argument('--allow-provider-key-release', action='store_true', help='Accept hosted settlement: recovering paid output requires the supplier online')
    keys = sub.add_parser('recover-hosted-keys', help='Recover paid hosted output from an explicitly chosen supplier; never sends funds')
    keys.add_argument('endpoint')
    keys.add_argument('--provider', required=True)
    keys.add_argument('--data', type=Path, default=Path('data/buyer'))
    keys.add_argument('--tor-proxy')
    recover = sub.add_parser("recover-payments", help="Reconcile regtest payments without sending funds")
    recover.add_argument("--lnd-mainnet", action="store_true")
    recover.add_argument("--data", type=Path, default=Path("data/buyer"))
    price = sub.add_parser("price", help="Convert pricing inputs to an exact rate and compatibility ceiling")
    modes = price.add_mutually_exclusive_group(required=True)
    modes.add_argument("--sats-per-token")
    modes.add_argument("--cents-per-kwh")
    price.add_argument("--joules-per-token")
    price.add_argument("--usd-per-btc")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        from .app import create_app
        from .models import Config
        uvicorn.run(create_app(args.data, Config.load(args.config) if args.config else None), host=args.host, port=args.port,
                    limit_concurrency=64, timeout_keep_alive=10, proxy_headers=False)
    elif args.command == "verify-model":
        model = Manifest.model_validate(json.loads(args.manifest.read_text()))
        model.verify_files(args.directory)
        print(json.dumps({"model_id": model.model_id, "files_verified": True, "execution_verified": False}))
    elif args.command == "manifest":
        with args.file.open("rb") as handle:
            sha = hashlib.file_digest(handle, "sha256").hexdigest()
        model = Manifest(name=args.name, architecture=args.architecture, quantization=args.quantization,
                         context_tokens=args.context, artifacts=[{"path": args.file.name, "sha256": sha,
                         "size": args.file.stat().st_size, "role": "weights"}])
        print(model.model_dump_json(indent=2))
    elif args.command == "price":
        from .pricing import energy_price, sats_per_token, sats_rate
        if args.sats_per_token is not None:
            print(json.dumps({"output_msat_per_token": sats_per_token(args.sats_per_token), "output_msat_per_token_exact": sats_rate(args.sats_per_token)}))
        else:
            print(json.dumps(energy_price(args.cents_per_kwh, args.joules_per_token, args.usd_per_btc)))
    elif args.command == 'recover-hosted-keys':
        from .client import Buyer
        buyer = Buyer(Identity.load(args.data / 'identity.key'), args.data)
        print(json.dumps(asyncio.run(buyer.recover_hosted_keys(args.endpoint, args.provider, tor_proxy=args.tor_proxy))))
    elif args.command == "recover-payments":
        from .client import Buyer
        from .lightning import LndRegtest, LndMainnet
        buyer = Buyer(Identity.load(args.data / "identity.key"), args.data, (LndMainnet if args.lnd_mainnet else LndRegtest).from_env())
        print(json.dumps(asyncio.run(buyer.reconcile_payments())))
    else:
        from .client import Buyer
        from .lightning import LndRegtest, LndMainnet
        buyer = Buyer(Identity.load(args.data / "identity.key"), args.data,
                      LndMainnet.from_env() if args.lnd_mainnet else LndRegtest.from_env() if args.lnd_regtest else None)

        async def run():
            if buyer.wallet:
                await buyer.reconcile_payments()
            async for text in buyer.run(args.endpoint, args.provider, args.model_id, args.prompt,
                                        args.max_tokens, args.max_msat, args.allow_lab_unverified,
                                        args.fee_limit_msat, tor_proxy=args.tor_proxy, assurance=args.assurance,
                                        total_fee_limit_msat=args.total_fee_limit_msat, daily_limit_msat=args.daily_limit_msat, allow_provider_key_release=args.allow_provider_key_release, allow_prepaid_compute=args.allow_prepaid_compute, funding_limit_msat=args.funding_limit_msat):
                print(text, end="", flush=True)
            print()
        try:
            asyncio.run(run())
        except Exception as exc:
            print(f"\nInference stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
