"""x402 v1 'exact' scheme: verify EIP-3009 TransferWithAuthorization, then settle.

Order of operations per Grok's honesty rule: verify signature -> fetch the
underlying data -> ONLY THEN submit the settlement. If we fetched nothing,
the authorization is never submitted and the buyer is never charged.
"""
import base64
import json
import time

from eth_account import Account
from eth_account.messages import encode_typed_data

from . import config, ledger

USDC_DOMAIN = {
    "name": "USD Coin",
    "version": "2",
    "chainId": config.CHAIN_ID,
    "verifyingContract": config.USDC_BASE,
}
AUTH_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}

USDC_ABI = [
    {
        "name": "transferWithAuthorization",
        "type": "function",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "validAfter", "type": "uint256"},
            {"name": "validBefore", "type": "uint256"},
            {"name": "nonce", "type": "bytes32"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "authorizationState",
        "type": "function",
        "inputs": [
            {"name": "authorizer", "type": "address"},
            {"name": "nonce", "type": "bytes32"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
    },
]


class PaymentError(Exception):
    pass


def parse_payment_header(header: str) -> dict:
    try:
        return json.loads(base64.b64decode(header).decode())
    except Exception as e:
        raise PaymentError("unparseable X-PAYMENT header: %s" % e)


def _auth_message(auth: dict) -> dict:
    from web3 import Web3

    return {
        "types": AUTH_TYPES,
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": "USD Coin",
            "version": "2",
            "chainId": config.CHAIN_ID,
            "verifyingContract": Web3.to_checksum_address(config.USDC_BASE),
        },
        "message": {
            "from": Web3.to_checksum_address(auth["from"]),
            "to": Web3.to_checksum_address(auth["to"]),
            "value": int(auth["value"]),
            "validAfter": int(auth["validAfter"]),
            "validBefore": int(auth["validBefore"]),
            "nonce": auth["nonce"],
        },
    }


def verify_payment(payment: dict, expected_units: int) -> dict:
    """Returns the authorization dict or raises PaymentError. Never settles."""
    if payment.get("x402Version") != 1:
        raise PaymentError("only x402Version 1 supported")
    if payment.get("scheme") != "exact":
        raise PaymentError("only 'exact' scheme supported")
    if payment.get("network") != config.X402_NETWORK_V1:
        raise PaymentError("only '%s' network supported" % config.X402_NETWORK_V1)
    payload = payment.get("payload") or {}
    auth = payload.get("authorization") or {}
    sig = payload.get("signature") or ""
    for f in ("from", "to", "value", "validAfter", "validBefore", "nonce"):
        if f not in auth:
            raise PaymentError("authorization missing field %s" % f)
    try:
        enc = encode_typed_data(full_message=_auth_message(auth))
        sig_b = bytes.fromhex(sig[2:] if sig.startswith("0x") else sig)
        recovered = Account.recover_message(enc, signature=sig_b)
    except Exception as e:
        raise PaymentError("bad authorization signature: %s" % e)
    if recovered.lower() != auth["from"].lower():
        raise PaymentError("signature does not match authorization.from")
    if auth["to"].lower() != config.PAY_TO.lower():
        raise PaymentError("authorization.to is not the seller wallet")
    if int(auth["value"]) != int(expected_units):
        raise PaymentError(
            "authorization value %s != quoted price %s units"
            % (auth["value"], expected_units)
        )
    t = int(time.time())
    if int(auth["validAfter"]) > t:
        raise PaymentError("authorization not yet valid")
    if int(auth["validBefore"]) <= t:
        raise PaymentError("authorization expired")
    if ledger.nonce_used(auth["nonce"]):
        raise PaymentError("authorization nonce already used")
    return auth


def _w3():
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(config.RPC_URL, request_kwargs={"timeout": 25}))
    if not w3.is_connected():
        raise PaymentError("no Base RPC connection")
    return w3


def settle(auth: dict, signature: str) -> dict:
    """Submit transferWithAuthorization. Returns {tx_hash, settlement}."""
    ledger.mark_nonce(auth["nonce"])
    if config.DRY_RUN:
        return {"tx_hash": "", "settlement": "simulated"}
    w3 = _w3()
    usdc = w3.eth.contract(
        address=w3.to_checksum_address(config.USDC_BASE), abi=USDC_ABI
    )
    try:
        used = usdc.functions.authorizationState(
            w3.to_checksum_address(auth["from"]), auth["nonce"]
        ).call()
    except Exception:
        used = False
    if used:
        raise PaymentError("authorization already spent on-chain")
    sig_b = bytes.fromhex(
        signature[2:] if signature.startswith("0x") else signature
    )
    v, r, s = sig_b[64], sig_b[:32], sig_b[32:64]
    fn = usdc.functions.transferWithAuthorization(
        w3.to_checksum_address(auth["from"]),
        w3.to_checksum_address(auth["to"]),
        int(auth["value"]),
        int(auth["validAfter"]),
        int(auth["validBefore"]),
        auth["nonce"],
        v, r, s,
    )
    acct = Account.from_key(config.load_privkey())
    tx = fn.build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 120000,
            "maxFeePerGas": w3.eth.gas_price,
            "maxPriorityFeePerGas": w3.to_wei("0.01", "gwei"),
            "chainId": config.CHAIN_ID,
        }
    )
    signed = acct.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise PaymentError("settlement transaction reverted: %s" % tx_hash)
    return {"tx_hash": tx_hash, "settlement": "onchain"}
