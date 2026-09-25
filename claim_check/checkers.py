"""Week-one claim checkers: funding, apy, x402_seller.

Each checker returns:
  {verdict, claimed_bps, measured_bps, delta_bps, sources, gaps, notes,
   fetched_any}
verdict in {supported, inflated, unsupported, unverifiable}.
fetched_any=False means we retrieved nothing: the caller must NOT settle.
"""
import json
import re
import subprocess
import time
import urllib.request

TOL_BPS = 50.0


def _curl_json(url, data=None, timeout=25):
    cmd = ["curl", "-sS", "-m", str(timeout), url]
    if data is not None:
        cmd += ["-X", "POST", "-H", "Content-Type: application/json",
                "-d", json.dumps(data)]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       timeout=timeout + 10)
    if p.returncode != 0:
        raise RuntimeError("curl: %s" % p.stderr[:200])
    return json.loads(p.stdout)


def _get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "claim-check/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse_claim_bps(claim):
    """Return (bps, interpretation_note). Accepts '8.4%', '0.0001', 8.4."""
    s = str(claim).strip().replace(",", "")
    if s.endswith("%"):
        return float(s[:-1]) / 100.0 * 10000.0, "percent"
    v = float(s)
    if abs(v) > 0.05:
        # A bare number like 8.4 is far more likely to mean 8.4% than 84000bps
        # as a raw fraction; funding/APY claims are quoted in percent.
        return v / 100.0 * 10000.0, "bare number read as percent"
    return v * 10000.0, "fraction"


# ---------------- funding ----------------

def _hl_funding(coin):
    # curl: urllib intermittently truncates HL responses (known lesson).
    meta, ctxs = _curl_json("https://api.hyperliquid.xyz/info",
                            {"type": "metaAndAssetCtxs"})
    names = [u["name"] for u in meta["universe"]]
    i = names.index(coin)
    return float(ctxs[i]["funding"])  # per 8h


def _dydx_funding(ticker):
    # nextFundingRate is per hour on dYdX v4; normalize x8.
    j = _get_json("https://indexer.dydx.trade/v4/perpetualMarkets")
    m = j["markets"]["%s-USD" % ticker]
    return float(m["nextFundingRate"]) * 8.0


def _okx_funding(inst):
    j = _get_json(
        "https://www.okx.com/api/v5/public/funding-rate?instId=%s" % inst)
    return float(j["data"][0]["fundingRate"])  # per 8h


def _binance_funding(symbol):
    j = _get_json(
        "https://fapi.binance.com/fapi/v1/fundingRate?symbol=%s&limit=1"
        % symbol)
    return float(j[0]["fundingRate"])  # per 8h


def _bybit_funding(symbol):
    j = _get_json(
        "https://api.bybit.com/v5/market/funding/history?category=linear"
        "&symbol=%s&limit=1" % symbol)
    return float(j["result"]["list"][0]["fundingRate"])  # per 8h


VENUES = [
    ("hyperliquid", lambda a: _hl_funding(a), "8h"),
    ("dydx", lambda a: _dydx_funding(a), "1h->8h"),
    ("okx", lambda a: _okx_funding(a + "-USDT-SWAP"), "8h"),
    ("binance", lambda a: _binance_funding(a + "USDT"), "8h"),
    ("bybit", lambda a: _bybit_funding(a + "USDT"), "8h"),
]


def check_funding(claim, params):
    asset = str(params.get("asset", "ETH")).upper()
    claimed_bps, interp = parse_claim_bps(claim)
    sources, gaps = [], []
    for name, fn, basis in VENUES:
        try:
            rate_8h = fn(asset)
            sources.append({
                "venue": name, "rate_8h": rate_8h,
                "rate_bps": rate_8h * 10000.0, "basis": basis,
                "as_of": int(time.time()),
            })
        except Exception as e:
            gaps.append({"venue": name, "reason": str(e)[:160]})
    if not sources:
        return {"verdict": "unverifiable", "claimed_bps": claimed_bps,
                "measured_bps": None, "delta_bps": None, "sources": sources,
                "gaps": gaps, "fetched_any": False,
                "notes": "no venue reachable; nothing fetched, do not charge"}
    rates = sorted(s["rate_bps"] for s in sources)
    measured = rates[len(rates) // 2]
    delta = claimed_bps - measured
    if abs(delta) <= TOL_BPS:
        verdict = "supported"
    elif delta > 0:
        verdict = "inflated"
    else:
        verdict = "unsupported"
    return {
        "verdict": verdict, "claimed_bps": claimed_bps,
        "measured_bps": measured, "delta_bps": delta,
        "sources": sources, "gaps": gaps, "fetched_any": True,
        "notes": "claim read as %s; 8h-normalized median of %d venues; "
                 "tolerance 50bps%s"
                 % (interp, len(sources),
                    "; direction understated" if verdict == "unsupported"
                    else ""),
    }


# ---------------- apy ----------------

APY_RES = [
    re.compile(r"(\d+(?:\.\d+)?)\s*%\s*AP[YR]", re.I),
    re.compile(r"AP[YR]\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*%", re.I),
]


def check_apy(claim, params):
    url = params.get("url")
    claimed_bps, interp = parse_claim_bps(claim)
    gaps = []
    if not url:
        return {"verdict": "unverifiable", "claimed_bps": claimed_bps,
                "measured_bps": None, "delta_bps": None, "sources": [],
                "gaps": [{"venue": "params", "reason": "no url supplied"}],
                "fetched_any": False,
                "notes": "need params.url (issuer page) to fetch"}
    try:
        req = urllib.request.Request(url,
                                     headers={"User-Agent": "claim-check/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            html = r.read().decode("utf-8", "replace")
        fetched = True
    except Exception as e:
        return {"verdict": "unverifiable", "claimed_bps": claimed_bps,
                "measured_bps": None, "delta_bps": None, "sources": [],
                "gaps": [{"venue": "fetch", "reason": str(e)[:160]}],
                "fetched_any": False,
                "notes": "issuer page unreachable; nothing fetched"}
    found = []
    for rx in APY_RES:
        found += [float(m.group(1)) for m in rx.finditer(html)]
    if not found:
        return {"verdict": "unverifiable", "claimed_bps": claimed_bps,
                "measured_bps": None, "delta_bps": None,
                "sources": [{"venue": "page", "url": url,
                             "as_of": int(time.time())}],
                "gaps": [{"venue": "parse",
                          "reason": "no APY/APR figure found on page"}],
                "fetched_any": True,
                "notes": "page fetched but no APY/APR number present"}
    measured_bps = max(found) / 100.0 * 10000.0
    delta = claimed_bps - measured_bps
    if abs(delta) <= TOL_BPS:
        verdict = "supported"
    elif delta > 0:
        verdict = "inflated"
    else:
        verdict = "unsupported"
    return {
        "verdict": verdict, "claimed_bps": claimed_bps,
        "measured_bps": measured_bps, "delta_bps": delta,
        "sources": [{"venue": "page", "url": url, "apy_pct": found,
                     "as_of": int(time.time())}],
        "gaps": gaps, "fetched_any": True,
        "notes": "claim read as %s; compared against highest APY/APR figure "
                 "on the fetched page" % interp,
    }


# ---------------- x402_seller ----------------

def check_x402_seller(claim, params):
    seller = params.get("seller_address", "")
    claimed_bps, interp = parse_claim_bps(claim)
    metric = params.get("metric", "volume_usdc_7d")
    gaps = []
    if not seller:
        return {"verdict": "unverifiable", "claimed_bps": claimed_bps,
                "measured_bps": None, "delta_bps": None, "sources": [],
                "gaps": [{"venue": "params",
                          "reason": "no seller_address supplied"}],
                "fetched_any": False, "notes": "need params.seller_address"}
    try:
        from web3 import Web3
        from . import config
        w3 = Web3(Web3.HTTPProvider(config.RPC_URL,
                                    request_kwargs={"timeout": 25}))
        if not w3.is_connected():
            raise RuntimeError("no Base RPC connection")
        latest = w3.eth.block_number
        # ~7d of Base blocks (~2s).
        start = max(0, latest - 302400)
        transfer_sig = w3.keccak(
            text="Transfer(address,address,uint256)").hex()
        logs = w3.eth.get_logs({
            "fromBlock": start, "toBlock": latest,
            "address": w3.to_checksum_address(config.USDC_BASE),
            "topics": [transfer_sig, None,
                       "0x000000000000000000000000" + seller[2:].lower()],
        })
        total = sum(int(l["data"], 16) for l in logs) / 1e6
        buyers = len({l["topics"][1].hex() for l in logs})
        fetched = True
    except Exception as e:
        return {"verdict": "unverifiable", "claimed_bps": claimed_bps,
                "measured_bps": None, "delta_bps": None, "sources": [],
                "gaps": [{"venue": "base_rpc", "reason": str(e)[:160]}],
                "fetched_any": False,
                "notes": "on-chain scan failed; nothing fetched"}
    measured = total if metric == "volume_usdc_7d" else float(buyers)
    claimed = claimed_bps if metric != "volume_usdc_7d" else None
    if metric == "volume_usdc_7d":
        # claim is a volume figure in USDC; compare directly, not in bps.
        try:
            claimed_vol = float(str(claim).replace(",", "").rstrip("%"))
        except Exception:
            claimed_vol = None
        if claimed_vol is None:
            verdict, delta = "unverifiable", None
        else:
            delta = claimed_vol - measured
            tol = max(1.0, measured * 0.05)
            verdict = ("supported" if abs(delta) <= tol
                       else "inflated" if delta > 0 else "unsupported")
    else:
        delta = claimed_bps - measured
        verdict = ("supported" if abs(delta) <= max(1.0, TOL_BPS)
                   else "inflated" if delta > 0 else "unsupported")
    return {
        "verdict": verdict, "claimed_bps": claimed_bps,
        "measured_bps": measured, "delta_bps": delta,
        "sources": [{"venue": "base_usdc_logs", "seller": seller,
                     "volume_usdc_7d": total, "buyers_7d": buyers,
                     "as_of": int(time.time())}],
        "gaps": gaps, "fetched_any": True,
        "notes": "measured from Base USDC Transfer events to seller, "
                 "last ~7d",
    }


CHECKERS = {
    "funding": check_funding,
    "apy": check_apy,
    "x402_seller": check_x402_seller,
}
