"""Shared config for Claim Check."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "claimcheck.db"

KEY_PATH = Path.home() / ".evm-agent" / "id.json"

# Base mainnet USDC and the seller wallet (agent EVM wallet).
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAY_TO = "0x9f7e7200Adcf5c073981B902b8E8B471A458E528"
CHAIN_ID = 8453
CAIP2 = "eip155:8453"
X402_NETWORK_V1 = "base"

# Pricing (USDC). Grok's rule: never $0.05; $0.15 default, $0.25 multi-source.
PRICE_DEFAULT_USD = 0.15
PRICE_MULTI_USD = 0.25
USDC_DECIMALS = 6

QUOTE_TTL_S = 900  # quotes live 15 minutes

# DRY_RUN=1: verify signatures honestly but simulate settlement (no on-chain tx).
DRY_RUN = os.environ.get("CLAIMCHECK_DRY_RUN", "0") == "1"
RPC_URL = os.environ.get("CLAIMCHECK_RPC", "https://mainnet.base.org")

KIND_PRICES = {
    "funding": PRICE_MULTI_USD,      # 3+ venues
    "apy": PRICE_DEFAULT_USD,        # one page fetch
    "x402_seller": PRICE_MULTI_USD,  # on-chain scan
}


def usd_to_units(usd: float) -> int:
    return int(round(usd * 10 ** USDC_DECIMALS))


def load_privkey() -> str:
    env_key = os.environ.get("CLAIMCHECK_PRIVKEY", "").strip()
    if env_key:
        return env_key if env_key.startswith("0x") else "0x" + env_key
    raw = KEY_PATH.read_text().strip()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            for k in ("privateKey", "private_key", "privkey", "secret", "key"):
                if k in parsed:
                    return parsed[k]
        if isinstance(parsed, list):
            return "0x" + bytes(parsed).hex()
    except Exception:
        pass
    h = raw[2:] if raw.startswith("0x") else raw
    if len(h) == 64 and all(c in "0123456789abcdefABCDEF" for c in h):
        return "0x" + h
    raise RuntimeError("cannot parse EVM key at %s" % KEY_PATH)
