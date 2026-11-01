"""Read-only chain access over PINNED testnet RPCs: ERC-20 balance / allowance, receipts and Transfer events.

The RPC URL is chosen from config.TESTNET_RPC by chain id; a URL from the gateway is never used (SSRF). Nothing is
ever signed or broadcast from here.
"""
import json
import re
import urllib.request
from urllib.parse import urlsplit

import config

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}\Z")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]{0,64}\Z")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}\Z")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class ChainError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _post(url, method, params, timeout):
    if urlsplit(url).scheme != "https":
        raise ChainError("pinned RPC must be https")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": config.USER_AGENT})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            raw = r.read(config.MAX_RPC_BYTES + 1)
    except Exception as e:
        raise ChainError(f"RPC call failed: {type(e).__name__}") from None
    if len(raw) > config.MAX_RPC_BYTES:
        raise ChainError("RPC answer too large")
    try:
        return json.loads(raw).get("result")
    except (ValueError, AttributeError):
        raise ChainError("RPC returned invalid JSON") from None


def rpc(chain_id, method, params, timeout=15):
    url = config.TESTNET_RPC.get(int(chain_id))
    if not url:
        raise ChainError(f"no pinned RPC for chain {chain_id} (testnets only)")
    return _post(url, method, params, timeout)


def _quantity(v):
    if not isinstance(v, str) or not HEX_RE.match(v) or v == "0x":
        raise ChainError("RPC returned no result (is the token deployed on this chain?)")
    return int(v, 16)


def _eth_call(chain_id, to, data, timeout=15):
    return _quantity(rpc(chain_id, "eth_call", [{"to": to, "data": data}, "latest"], timeout))


def head(chain_id):
    """Latest block number. Also proves the pinned RPC serves the chain we think it does."""
    if _quantity(rpc(chain_id, "eth_chainId", [])) != int(chain_id):
        raise ChainError(f"pinned RPC for chain {chain_id} reports another chain")
    return _quantity(rpc(chain_id, "eth_blockNumber", []))


def receipt(chain_id, tx_hash):
    """The transaction receipt, or None if the node does not know it (yet)."""
    if not isinstance(tx_hash, str) or not TX_RE.match(tx_hash):
        raise ChainError("bad transaction hash")
    r = rpc(chain_id, "eth_getTransactionReceipt", [tx_hash])
    if r is None:
        return None
    if not isinstance(r, dict) or not isinstance(r.get("logs"), list):
        raise ChainError("RPC returned a malformed receipt")
    return r


def transfers(rcpt):
    """ERC-20 Transfer events of a receipt, as {token, from, to, value}. Anything that is not one is skipped."""
    out = []
    for log in rcpt.get("logs", [])[:200]:
        t = log.get("topics") if isinstance(log, dict) else None
        if not isinstance(t, list) or len(t) != 3 or t[0] != TRANSFER_TOPIC:
            continue
        data, token = log.get("data"), log.get("address")
        if not isinstance(data, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", data):
            continue
        if not isinstance(token, str) or not ADDR_RE.match(token):
            continue
        if not all(isinstance(x, str) and re.fullmatch(r"0x0{24}[0-9a-fA-F]{40}", x) for x in t[1:]):
            continue
        out.append({"token": token.lower(), "from": "0x" + t[1][-40:].lower(), "to": "0x" + t[2][-40:].lower(),
                    "value": int(data, 16)})
    return out


def _word(addr):
    if not ADDR_RE.match(addr or ""):
        raise ChainError("bad address")
    return addr[2:].lower().rjust(64, "0")


def balance_of(chain_id, token, owner):
    return _eth_call(chain_id, token, "0x70a08231" + _word(owner))


def allowance(chain_id, token, owner, spender):
    return _eth_call(chain_id, token, "0xdd62ed3e" + _word(owner) + _word(spender))
