# Claim Check

Pay-per-claim verifier on x402. A stranger's number goes in, a signed
recompute comes out. USDC on Base, `exact` scheme, x402 v1.

## Run locally

```
cd ~/workspace/claim-check
CLAIMCHECK_DRY_RUN=1 .venv/bin/python -m uvicorn claim_check.app:app --port 8099
```

`CLAIMCHECK_DRY_RUN=1` verifies signatures honestly but simulates settlement
(no on-chain tx). Production (Fly EU) runs without it and settles for real.

## End-to-end test

```
PYTHONPATH=. CLAIMCHECK_DRY_RUN=1 .venv/bin/python tests/e2e_local.py
```

Covers: free quote, 402 + signed offer, paid check with a real EIP-3009
signature (supported / inflated / unsupported verdicts), unverifiable ->
503 with no charge, voucher buy/redeem/check, bad-payment rejection,
free /v1/verify, discovery docs.

## Money flow

Buyer signs EIP-3009 USDC authorization -> server verifies -> server fetches
data -> server submits `transferWithAuthorization` (pays Base gas from the
agent wallet) -> $0.15/$0.25 USDC lands directly in
`0x9f7e7200Adcf5c073981B902b8E8B471A458E528`. No intermediary, no payouts.

Gas top-up rule (standing): when wallet ETH < $0.50, swap $2 of received
USDC for ETH, log it.

## Receipts

Official x402 offer-receipt extension (EIP-712, normative schemas), plus a
signed Claim Check fulfillment binding quote -> payment -> request hash ->
result hash -> sources -> policy_hash. Verification is free and local.

## Layout

- `claim_check/app.py` - FastAPI routes
- `claim_check/checkers.py` - funding / apy / x402_seller
- `claim_check/x402pay.py` - payment verify + settle
- `claim_check/artifacts.py` - EIP-712 offer/receipt/fulfillment
- `claim_check/ledger.py` - SQLite ledger
- `claim_check/config.py` - constants, key loading
- `tests/e2e_local.py` - full local verification
- `hidden_state/` - cron watermarks (spec watch)
