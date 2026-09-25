"""Signed artifacts.

Two layers, both EIP-712:

1. The official x402 offer-receipt extension (normative schemas from
   specs/extensions/extension-offer-and-receipt.md). We never add fields to
   these; interop beats novelty.
     - Offer  -> extensions["offer-receipt"].info.offers[] on the 402
     - Receipt -> extensions["offer-receipt"].info.receipt on success

2. Our Claim Check fulfillment: the causal chain the v1 receipt does not
   bind (claim, verdict, request/response hashes, sources, policy hash),
   signed with our own EIP-712 type, at extensions["claim-check"].

Verification is free and local: anyone with the payload + signature recovers
the signer and compares it to payTo. No server round-trip needed.
"""
import time

from eth_account import Account
from eth_account.messages import encode_typed_data

DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
]

OFFER_DOMAIN = {"name": "x402 offer", "version": "1", "chainId": 1}
OFFER_TYPES = {
    "EIP712Domain": DOMAIN_FIELDS,
    "Offer": [
        {"name": "version", "type": "uint256"},
        {"name": "resourceUrl", "type": "string"},
        {"name": "scheme", "type": "string"},
        {"name": "network", "type": "string"},
        {"name": "asset", "type": "string"},
        {"name": "payTo", "type": "string"},
        {"name": "amount", "type": "string"},
        {"name": "validUntil", "type": "uint256"},
    ],
}

RECEIPT_DOMAIN = {"name": "x402 receipt", "version": "1", "chainId": 1}
RECEIPT_TYPES = {
    "EIP712Domain": DOMAIN_FIELDS,
    "Receipt": [
        {"name": "version", "type": "uint256"},
        {"name": "network", "type": "string"},
        {"name": "resourceUrl", "type": "string"},
        {"name": "payer", "type": "string"},
        {"name": "issuedAt", "type": "uint256"},
        {"name": "transaction", "type": "string"},
    ],
}

FULFILL_DOMAIN = {"name": "Claim Check fulfillment", "version": "1", "chainId": 1}
FULFILL_TYPES = {
    "EIP712Domain": DOMAIN_FIELDS,
    "Fulfillment": [
        {"name": "version", "type": "uint256"},
        {"name": "quoteId", "type": "string"},
        {"name": "kind", "type": "string"},
        {"name": "claim", "type": "string"},
        {"name": "verdict", "type": "string"},
        {"name": "claimedBps", "type": "string"},
        {"name": "measuredBps", "type": "string"},
        {"name": "deltaBps", "type": "string"},
        {"name": "requestHash", "type": "string"},
        {"name": "responseHash", "type": "string"},
        {"name": "sourcesHash", "type": "string"},
        {"name": "policyHash", "type": "string"},
        {"name": "issuedAt", "type": "uint256"},
    ],
}


def _sign(domain, types, primary, message, privkey) -> str:
    enc = encode_typed_data(full_message={
        "types": types, "primaryType": primary, "domain": domain,
        "message": message})
    return "0x" + Account.sign_message(enc, private_key=privkey).signature.hex()


def _recover(domain, types, primary, payload, signature) -> str:
    enc = encode_typed_data(full_message={
        "types": types, "primaryType": primary, "domain": domain,
        "message": payload})
    sig = signature[2:] if signature.startswith("0x") else signature
    return Account.recover_message(enc, signature=bytes.fromhex(sig))


def make_offer(resource_url, amount_units, asset, pay_to, network_caip2,
               valid_until, privkey, accept_index=0) -> dict:
    payload = {
        "version": 1, "resourceUrl": resource_url, "scheme": "exact",
        "network": network_caip2, "asset": asset, "payTo": pay_to,
        "amount": str(amount_units), "validUntil": int(valid_until),
    }
    return {
        "format": "eip712", "acceptIndex": accept_index, "payload": payload,
        "signature": _sign(OFFER_DOMAIN, OFFER_TYPES, "Offer", payload, privkey),
    }


def make_receipt(network_caip2, resource_url, payer, tx_hash, privkey) -> dict:
    payload = {
        "version": 1, "network": network_caip2, "resourceUrl": resource_url,
        "payer": payer, "issuedAt": int(time.time()),
        "transaction": tx_hash or "",
    }
    return {
        "format": "eip712", "payload": payload,
        "signature": _sign(RECEIPT_DOMAIN, RECEIPT_TYPES, "Receipt", payload,
                           privkey),
    }


def make_fulfillment(quote_id, kind, claim, verdict, claimed_bps, measured_bps,
                     delta_bps, request_hash, response_hash, sources_hash,
                     policy_hash, privkey) -> dict:
    payload = {
        "version": 1, "quoteId": quote_id, "kind": kind, "claim": str(claim),
        "verdict": verdict, "claimedBps": str(claimed_bps),
        "measuredBps": str(measured_bps), "deltaBps": str(delta_bps),
        "requestHash": request_hash, "responseHash": response_hash,
        "sourcesHash": sources_hash, "policyHash": policy_hash or "",
        "issuedAt": int(time.time()),
    }
    return {
        "format": "eip712", "payload": payload,
        "signature": _sign(FULFILL_DOMAIN, FULFILL_TYPES, "Fulfillment",
                           payload, privkey),
    }


def verify_offer(artifact) -> str:
    return _recover(OFFER_DOMAIN, OFFER_TYPES, "Offer", artifact["payload"],
                    artifact["signature"])


def verify_receipt(artifact) -> str:
    return _recover(RECEIPT_DOMAIN, RECEIPT_TYPES, "Receipt",
                    artifact["payload"], artifact["signature"])


def verify_fulfillment(artifact) -> str:
    return _recover(FULFILL_DOMAIN, FULFILL_TYPES, "Fulfillment",
                    artifact["payload"], artifact["signature"])
