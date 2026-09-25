"""Claim Check: pay-per-claim verifier on x402.

Muse flow: human "is this yield real?" -> Muse hits /v1/quote (free) ->
Muse asks human to approve the price -> human yes -> Muse pays the 402,
reads the verdict + signed receipt.
"""
import base64
import hashlib
import json
import secrets
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse, HTMLResponse

from . import artifacts, checkers, config, ledger, x402pay

app = FastAPI(title="Claim Check",
              description="Pay-per-claim verifier for agent commerce. "
                          "x402 USDC on Base. Quote is free; checks are "
                          "$0.15-$0.25; verification is free and local.",
              version="1.0.0")
_privkey = None


def privkey():
    global _privkey
    if _privkey is None:
        _privkey = config.load_privkey()
    return _privkey


def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def resource_url(request: Request, path: str) -> str:
    base = str(request.base_url).rstrip("/")
    return base + path


def signed_offer(request: Request, price_units: int, resource: str,
                 valid_until: int) -> dict:
    return artifacts.make_offer(
        resource_url(request, resource), price_units, config.USDC_BASE,
        config.PAY_TO, config.CAIP2, valid_until, privkey())


def payment_required(request: Request, price_units: int, resource: str,
                     description: str):
    offer = signed_offer(request, price_units, resource,
                         int(time.time()) + config.QUOTE_TTL_S)
    body = {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact",
            "network": config.X402_NETWORK_V1,
            "maxAmountRequired": str(price_units),
            "asset": config.USDC_BASE,
            "payTo": config.PAY_TO,
            "resource": resource_url(request, resource),
            "description": description,
            "mimeType": "application/json",
            "maxTimeoutSeconds": 300,
        }],
        "extensions": {"offer-receipt": {"info": {"offers": [offer]}}},
        "error": "payment required: sign an EIP-3009 USDC authorization "
                 "and resend in the X-PAYMENT header",
    }
    return JSONResponse(status_code=402, content=body)


# ---------------- quote ----------------

@app.post("/v1/quote")
def quote(body: dict):
    kind = body.get("kind")
    claim = body.get("claim")
    params = body.get("params") or {}
    if kind not in checkers.CHECKERS:
        ledger.save_rejected_quote(kind, claim, "unknown kind")
        return JSONResponse(status_code=400, content={
            "checkable": False,
            "reason": "unknown kind %r; week-one kinds: %s"
                      % (kind, sorted(checkers.CHECKERS)),
            "cannot_prove": ["kind not supported"],
        })
    if claim is None:
        ledger.save_rejected_quote(kind, claim, "claim is required")
        return JSONResponse(status_code=400, content={
            "checkable": False, "reason": "claim is required",
            "cannot_prove": ["no claim supplied"],
        })
    price = config.KIND_PRICES[kind]
    units = config.usd_to_units(price)
    qid = "q_" + secrets.token_hex(12)
    expires = int(time.time()) + config.QUOTE_TTL_S
    ledger.save_quote(qid, kind, claim, params, price, units, expires)
    planned = {
        "funding": ["hyperliquid", "dydx", "okx", "binance", "bybit"],
        "apy": ["issuer page fetch"],
        "x402_seller": ["Base USDC settlement logs"],
    }[kind]
    cannot = {
        "funding": ["intent or future funding; only current point-in-time "
                    "rates are checkable"],
        "apy": ["APY figures not published on the fetched page; private "
                "or login-walled sources"],
        "x402_seller": ["off-chain sales; chains other than Base"],
    }[kind]
    try:
        claimed_bps, interp = checkers.parse_claim_bps(claim)
    except Exception:
        claimed_bps, interp = None, "unparseable"
    return {
        "checkable": True, "quote_id": qid,
        "price_usdc": price, "price_units": units,
        "expires_at": expires, "eta_seconds": 20,
        "kind": kind, "claim": claim, "claim_bps": claimed_bps,
        "claim_interpretation": interp,
        "sources_planned": planned, "cannot_prove": cannot,
        "ask_the_human": True,
        "pay_instructions": "POST /v1/check with {quote_id}; on 402, sign "
                            "the EIP-3009 authorization and resend with "
                            "X-PAYMENT header",
    }


# ---------------- check ----------------

@app.post("/v1/check")
def check(body: dict, request: Request,
          x_payment: str = Header(default=None)):
    qid = body.get("quote_id")
    policy_hash = body.get("policy_hash") or ""
    voucher_id = body.get("voucher_id")
    q = ledger.get_quote(qid) if qid else None
    if not q:
        return JSONResponse(status_code=404,
                            content={"error": "unknown quote_id"})
    if q["used"]:
        return JSONResponse(status_code=409,
                            content={"error": "quote already used"})
    if q["expires"] < int(time.time()):
        return JSONResponse(status_code=410,
                            content={"error": "quote expired; get a new one"})
    kind, price_units = q["kind"], q["price_units"]
    resource = "/v1/check"

    auth = None
    payer = None
    via_voucher = False
    if voucher_id:
        if not ledger.spend_voucher(voucher_id):
            return JSONResponse(status_code=402, content={
                "error": "voucher invalid or out of credits"})
        via_voucher = True
        payer = "voucher:" + voucher_id
    elif x_payment:
        try:
            payment = x402pay.parse_payment_header(x_payment)
            auth = x402pay.verify_payment(payment, price_units)
            payer = auth["from"]
        except x402pay.PaymentError as e:
            return JSONResponse(status_code=402,
                                content={"error": "bad payment: %s" % e})
    else:
        return payment_required(
            request, price_units, resource,
            "Claim Check: verify one %s claim" % kind)

    # Honesty rule: fetch first. If we retrieved nothing, the authorization
    # is never submitted and the buyer is never charged.
    params = json.loads(q["params"])
    result = checkers.CHECKERS[kind](q["claim"], params)
    if not result.get("fetched_any"):
        return JSONResponse(status_code=503, content={
            "verdict": "unverifiable",
            "error": "could not fetch any source; you were NOT charged",
            "gaps": result.get("gaps", []),
        })

    # Settle (or consume voucher credit, already decremented).
    sig = None
    if via_voucher:
        settlement = {"tx_hash": "", "settlement": "voucher"}
    else:
        payload = x402pay.parse_payment_header(x_payment)["payload"]
        sig = payload["signature"]
        try:
            settlement = x402pay.settle(auth, sig)
        except x402pay.PaymentError as e:
            return JSONResponse(status_code=402,
                                content={"error": "settlement failed: %s"
                                                 % e})

    check_id = "c_" + secrets.token_hex(12)
    request_hash = sha256_hex(canon({"quote_id": qid, "kind": kind,
                                     "claim": q["claim"], "params": params}))
    body_core = {
        "check_id": check_id, "kind": kind, "claim": q["claim"],
        "verdict": result["verdict"], "claimed_bps": result["claimed_bps"],
        "measured_bps": result["measured_bps"],
        "delta_bps": result["delta_bps"], "sources": result["sources"],
        "gaps": result["gaps"], "notes": result.get("notes", ""),
        "settlement": settlement["settlement"],
        "tx_hash": settlement["tx_hash"], "payer": payer,
    }
    response_hash = sha256_hex(canon(body_core))
    sources_hash = sha256_hex(canon(result["sources"]))

    receipt = artifacts.make_receipt(
        config.CAIP2, resource_url(request, resource), payer,
        settlement["tx_hash"], privkey())
    fulfillment = artifacts.make_fulfillment(
        qid, kind, q["claim"], result["verdict"], result["claimed_bps"],
        result["measured_bps"], result["delta_bps"], request_hash,
        response_hash, sources_hash, policy_hash, privkey())

    receipt_hash = sha256_hex(receipt["signature"])
    ledger.save_check(check_id, qid, kind, payer, settlement["tx_hash"],
                      settlement["settlement"], result["verdict"],
                      result["claimed_bps"], result["measured_bps"],
                      result["delta_bps"], q["price_usd"])
    ledger.save_receipt(receipt_hash, check_id, json.dumps(receipt),
                        json.dumps(fulfillment))
    ledger.mark_quote_used(qid)

    out = dict(body_core)
    out["receipt_hash"] = receipt_hash
    out["verify"] = ("free and local: recover the signer of "
                     "extensions['offer-receipt'].info.receipt and compare "
                     "to %s; or POST the artifact to /v1/verify"
                     % config.PAY_TO)
    out["extensions"] = {
        "offer-receipt": {"info": {"receipt": receipt}},
        "claim-check": fulfillment,
    }
    headers = {}
    if settlement["settlement"] == "onchain":
        presp = {"success": True, "transaction": settlement["tx_hash"],
                 "network": config.X402_NETWORK_V1, "payer": payer}
        headers["X-PAYMENT-RESPONSE"] = base64.b64encode(
            json.dumps(presp).encode()).decode()
    return JSONResponse(status_code=200, content=out, headers=headers)


# ---------------- verify (free) ----------------

@app.post("/v1/verify")
def verify(body: dict):
    artifact = body.get("artifact") or body
    kind = body.get("kind", "auto")
    try:
        fmt = artifact.get("format")
        if fmt != "eip712":
            return {"valid": False,
                    "error": "only eip712 artifacts supported here"}
        payload = artifact["payload"]
        name = None
        if kind == "auto":
            # detect by payload shape
            if "payer" in payload and "issuedAt" in payload \
                    and "transaction" in payload:
                kind = "receipt"
            elif "resourceUrl" in payload and "amount" in payload:
                kind = "offer"
            elif "quoteId" in payload and "verdict" in payload:
                kind = "fulfillment"
        if kind == "receipt":
            signer = artifacts.verify_receipt(artifact)
            authorized = signer.lower() == config.PAY_TO.lower()
            fresh = abs(int(time.time()) - int(payload["issuedAt"])) < 90 * 86400
            checks = {"signer_is_seller": authorized, "fresh": fresh}
            name = "x402 receipt"
        elif kind == "offer":
            signer = artifacts.verify_offer(artifact)
            authorized = signer.lower() == config.PAY_TO.lower()
            checks = {"signer_is_seller": authorized}
            name = "x402 offer"
        elif kind == "fulfillment":
            signer = artifacts.verify_fulfillment(artifact)
            authorized = signer.lower() == config.PAY_TO.lower()
            checks = {"signer_is_seller": authorized}
            name = "claim-check fulfillment"
        else:
            return {"valid": False, "error": "unknown artifact kind"}
        return {"valid": all(checks.values()), "artifact": name,
                "signer": signer, "expected_signer": config.PAY_TO,
                "checks": checks}
    except Exception as e:
        return {"valid": False, "error": "verification failed: %s" % e}


@app.get("/v1/verify")
def verify_by_hash(hash: str):
    r = ledger.get_receipt(hash)
    if not r:
        return JSONResponse(status_code=404,
                            content={"error": "receipt not found"})
    return {"hash": hash, "check_id": r["check_id"],
            "receipt": json.loads(r["receipt_json"]),
            "fulfillment": json.loads(r["fulfillment_json"])}


# ---------------- vouchers ----------------

@app.post("/v1/voucher/buy")
def voucher_buy(body: dict, request: Request,
                x_payment: str = Header(default=None)):
    try:
        n = int(body.get("checks", 1))
    except Exception:
        return JSONResponse(status_code=400,
                            content={"error": "checks must be an integer"})
    if not 1 <= n <= 100:
        return JSONResponse(status_code=400,
                            content={"error": "1-100 checks per voucher"})
    units = config.usd_to_units(config.PRICE_DEFAULT_USD * n)
    if not x_payment:
        return payment_required(
            request, units, "/v1/voucher/buy",
            "Claim Check voucher: %d prepaid checks" % n)
    try:
        payment = x402pay.parse_payment_header(x_payment)
        auth = x402pay.verify_payment(payment, units)
        settlement = x402pay.settle(auth, payment["payload"]["signature"])
    except x402pay.PaymentError as e:
        return JSONResponse(status_code=402,
                            content={"error": "payment failed: %s" % e})
    vid = "v_" + secrets.token_urlsafe(16)
    ledger.create_voucher(vid, n)
    return {"voucher_id": vid, "credits": n,
            "settlement": settlement["settlement"],
            "tx_hash": settlement["tx_hash"],
            "use": "POST /v1/check with {quote_id, voucher_id}"}


@app.post("/v1/voucher/redeem")
def voucher_redeem(body: dict):
    """Human path: send USDC on Base to PAY_TO from any wallet, paste tx hash.

    Server confirms the transfer on-chain, then issues voucher credits.
    """
    tx_hash = body.get("tx_hash", "")
    if not (tx_hash.startswith("0x") and len(tx_hash) == 66):
        return JSONResponse(status_code=400,
                            content={"error": "invalid tx_hash"})
    if ledger.nonce_used("redeem:" + tx_hash.lower()):
        return JSONResponse(status_code=409,
                            content={"error": "tx already redeemed"})
    if config.DRY_RUN:
        value_units, ok = config.usd_to_units(0.15), True
    else:
        try:
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider(
                config.RPC_URL, request_kwargs={"timeout": 25}))
            rcpt = w3.eth.get_transaction_receipt(tx_hash)
            if not rcpt or rcpt.status != 1:
                raise RuntimeError("tx not confirmed")
            # find USDC Transfer to PAY_TO in logs
            sig = w3.keccak(text="Transfer(address,address,uint256)").hex()
            value_units, ok = 0, False
            for l in rcpt.logs:
                if (l.address.lower() == config.USDC_BASE.lower()
                        and l.topics[0].hex() == sig
                        and ("000000000000000000000000"
                             + config.PAY_TO[2:].lower())
                        in l.topics[2].hex().lower()):
                    value_units = int(l.data, 16)
                    ok = True
        except Exception as e:
            return JSONResponse(status_code=400, content={
                "error": "could not confirm USDC payment on-chain: %s" % e})
    if not ok:
        return JSONResponse(status_code=400, content={
            "error": "no USDC transfer to the seller wallet in that tx"})
    credits = max(1, round(value_units / 1e6 / config.PRICE_DEFAULT_USD))
    vid = "v_" + secrets.token_urlsafe(16)
    ledger.create_voucher(vid, credits)
    ledger.mark_nonce("redeem:" + tx_hash.lower())
    return {"voucher_id": vid, "credits": credits,
            "paid_usdc": value_units / 1e6,
            "use": "POST /v1/check with {quote_id, voucher_id}"}


@app.post("/v1/voucher/balance")
def voucher_balance(body: dict):
    v = ledger.get_voucher(body.get("voucher_id", ""))
    if not v:
        return JSONResponse(status_code=404,
                            content={"error": "unknown voucher"})
    return {"voucher_id": v["id"], "credits": v["credits"]}


# ---------------- discovery ----------------

@app.get("/.well-known/x402")
def well_known():
    return {
        "x402Version": 1,
        "service": "Claim Check",
        "description": "Pay-per-claim verifier: a stranger's number goes "
                       "in, a signed recompute comes out. USDC on Base.",
        "endpoints": {
            "quote": {"path": "/v1/quote", "price": "free"},
            "check": {"path": "/v1/check",
                      "price": "$0.15-$0.25 USDC, x402 exact/BASE"},
            "verify": {"path": "/v1/verify", "price": "free"},
        },
        "receipts": "x402 offer-receipt extension (EIP-712) + signed "
                    "claim-check fulfillment; verification is free and local",
        "docs": "/llms.txt",
    }


LLMS_TXT = """# Claim Check

A pay-per-claim verifier for agent commerce. A stranger's number goes in,
a signed recompute comes out. You sell the delta between a claim and public
evidence, plus a signed receipt.

## Endpoints

- POST /v1/quote (FREE): send {kind, claim, params}. Returns checkable?,
  sources planned, price, ETA, and what cannot be proved. Always call this
  first. kind is one of: funding, apy, x402_seller.
- POST /v1/check ($0.15 default, $0.25 for multi-source): send {quote_id}.
  Without payment you get a 402 with signed offer. Pay via x402 v1 exact:
  sign the EIP-3009 USDC authorization on Base and resend with X-PAYMENT.
  Returns verdict, delta_bps, sources with as_of, gaps, and signed receipts.
- POST /v1/verify (FREE): send {artifact}. Recovers the signer and checks it
  locally. Anyone can verify offline with the payload + signature.

## Rules for agents

- NEVER store an API key. There is none.
- ALWAYS run /v1/quote first and ask the human before /v1/check.
- Verdicts: supported | inflated | unsupported | unverifiable.
- If the check returns unverifiable with "you were NOT charged", the
  settlement was never submitted. Never invent a number to look complete.

## Receipts

Every settled check emits the official x402 offer-receipt extension
(EIP-712, domain "x402 receipt") plus a signed claim-check fulfillment
binding quote -> payment -> request hash -> result hash -> sources ->
policy_hash. Verification is free and local.

## Vouchers

If you cannot sign x402: POST /v1/voucher/buy (x402) or send USDC on Base
to 0x9f7e7200Adcf5c073981B902b8E8B471A458E528 and POST /v1/voucher/redeem
with the tx hash. Then call /v1/check with {quote_id, voucher_id}.
"""


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms():
    return LLMS_TXT


@app.get("/openapi.json")
def openapi():
    return app.openapi()


INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Claim Check</title></head><body style="font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px">
<h1>Claim Check</h1>
<p>A stranger's number goes in, a signed recompute comes out. For agents.</p>
<ul>
<li><b>POST /v1/quote</b> - free. Is this claim checkable, and for how much?</li>
<li><b>POST /v1/check</b> - $0.15-$0.25 USDC on Base via x402. The verdict.</li>
<li><b>POST /v1/verify</b> - free. Verify any receipt, no server needed.</li>
</ul>
<h2>No x402 wallet? Use a voucher</h2>
<p>Send USDC on Base to<br><code>0x9f7e7200Adcf5c073981B902b8E8B471A458E528</code></p>
<p>Then paste the transaction hash:</p>
<input id="tx" style="width:100%" placeholder="0x...">
<button onclick="redeem()">Redeem</button>
<pre id="out"></pre>
<script>
async function redeem(){
  const r = await fetch('/v1/voucher/redeem',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({tx_hash: document.getElementById('tx').value.trim()})});
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
</script>
<p><a href="/llms.txt">llms.txt</a> for agents.</p>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
