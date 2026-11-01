#!/usr/bin/env python3
"""Receiving Agent: sells a HashKey Chain address profile, paid through the official HSP gateway.

The Track 2 flow, seen from this side:
  1. A request without payment gets 402 and our payment conditions, signed by the key of the address that gets paid.
  2. The Payment Agent (payment_agent.py) builds the order from them, signs, and HSP's Gateway settles on-chain.
  3. The same request with X-HSP-Payment-Request-Id and X-HSP-Proof is answered once, and only after this agent has
     checked the payment itself, in two independent places:
       - HSP: query_payment for OUR payment_request_id says payment-safe or -finalized, attribution_verified is true,
         and merchant, recipient, token, chain, USD amount and payer are the ones we quoted;
       - the chain: the transaction HSP names has status 1 on a pinned RPC, carries exactly one matching USDC Transfer
         (payer -> pay_to, inside the price band) and has enough confirmations.
     The caller never supplies a transaction hash. One quote serves once; one transaction can pay for one quote.

  receiving_agent.py [--host 127.0.0.1] [--port 8411]
"""
import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import chain as chain_rpc  # noqa: E402
import config  # noqa: E402
import flow  # noqa: E402
import wallets  # noqa: E402
from mcp_client import McpClient, McpError  # noqa: E402

CARD_PATH = "/.well-known/agent-card.json"
ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}\Z")
ID_RE = re.compile(r"^HSKM-PR-[0-9]{14}-[0-9a-f]{16}\Z")
TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}\Z")
SIG_RE = re.compile(r"^0x[0-9a-fA-F]{130}\Z")
BLOCK_RE = re.compile(r"^0x[0-9a-fA-F]{1,16}\Z")
MAX_PATH = 300


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def claim_message(pr_id, resource):
    return f"hsk-meter-claim:{pr_id}:{resource}"


def sign_text(account, text):
    from eth_account import Account
    from eth_account.messages import encode_defunct
    return "0x" + bytes(Account.sign_message(encode_defunct(text=text), account.key).signature).hex()


def recover_text(text, signature):
    from eth_account import Account
    from eth_account.messages import encode_defunct
    return Account.recover_message(encode_defunct(text=text), signature=signature)


def address_profile(address):
    """The paid data: public facts about an address on HashKey Chain mainnet, read-only."""
    def rpc(method, params):
        return chain_rpc._post(config.HSK_MAINNET_RPC, method, params, 10)

    def number(v):
        if not isinstance(v, str) or not re.fullmatch(r"0x[0-9a-fA-F]{1,64}", v):
            raise chain_rpc.ChainError("bad RPC value")
        return int(v, 16)
    code = rpc("eth_getCode", [address, "latest"])
    if not isinstance(code, str) or not re.fullmatch(r"0x[0-9a-fA-F]*", code):
        raise chain_rpc.ChainError("bad RPC value")
    usdce = rpc("eth_call", [{"to": config.HSK_USDCE, "data": "0x70a08231" + chain_rpc._word(address)}, "latest"])
    return {"chain": "HashKey Chain mainnet (177)", "address": address, "block": number(rpc("eth_blockNumber", [])),
            "isContract": len(code) > 2,
            "hskWei": str(number(rpc("eth_getBalance", [address, "latest"]))),
            "txCount": number(rpc("eth_getTransactionCount", [address, "latest"])),
            "usdceBaseUnits": str(number(usdce))}


class Refusal(Exception):
    def __init__(self, http, rule, detail="", retry_after=None):
        super().__init__(f"{rule}: {detail}")
        self.http, self.rule, self.detail, self.retry_after = http, rule, detail, retry_after


class Limiter:
    def __init__(self, per_minute):
        self.limit, self.hits, self.lock = per_minute, {}, threading.Lock()

    def allow(self, key, now):
        with self.lock:
            if len(self.hits) > 5000:
                self.hits.clear()
            recent = [t for t in self.hits.get(key, ()) if now - t < 60]
            ok = len(recent) < self.limit
            if ok:
                recent.append(now)
            self.hits[key] = recent
            return ok


@dataclass
class Quote:
    id: str
    resource: str
    payer: str
    usd: str
    network: str
    expires: int
    status: str


class Store:
    """Quotes survive a restart, so a replay is refused after one too. tx is UNIQUE: one payment pays for one quote."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        self.lock = threading.Lock()
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS quotes(
            id TEXT PRIMARY KEY, resource TEXT NOT NULL, payer TEXT NOT NULL, usd TEXT NOT NULL, network TEXT NOT NULL,
            expires INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'open', tx TEXT UNIQUE, claim_ts INTEGER)""")

    def add(self, q, now, max_open):
        with self.lock:
            self.db.execute("DELETE FROM quotes WHERE status='open' AND expires < ?", (now - config.A2A_CLAIM_GRACE_S,))
            live = self.db.execute("SELECT COUNT(*) FROM quotes WHERE status='open' AND expires >= ?", (now,)).fetchone()[0]
            if live >= max_open:
                raise Refusal(503, "busy", "too many open quotes", retry_after=60)
            self.db.execute("INSERT INTO quotes(id,resource,payer,usd,network,expires) VALUES(?,?,?,?,?,?)",
                            (q.id, q.resource, q.payer, q.usd, q.network, q.expires))

    def get(self, pr_id):
        with self.lock:
            r = self.db.execute("SELECT id,resource,payer,usd,network,expires,status FROM quotes WHERE id=?",
                                (pr_id,)).fetchone()
        return Quote(*r) if r else None

    def begin_serving(self, pr_id, tx, now):
        """open -> serving, atomically. now is taken right here, after verification; it also names this claim, so a
        claim that lost its slot cannot finish or release the one that took over. A claim left 'serving' by a crash can
        be taken again after two minutes; the data read is far shorter than that."""
        with self.lock:
            try:
                cur = self.db.execute("UPDATE quotes SET status='serving', tx=?, claim_ts=? WHERE id=? AND "
                                      "(status='open' OR (status='serving' AND claim_ts < ?))",
                                      (tx, now, pr_id, now - 120))
            except sqlite3.IntegrityError:
                raise Refusal(409, "tx_already_used", "this transaction already paid for another quote") from None
            if cur.rowcount != 1:
                raise Refusal(409, "already_served", "this quote was served; a new request needs a new payment")

    def release(self, pr_id, ts):
        with self.lock:
            self.db.execute("UPDATE quotes SET status='open', tx=NULL, claim_ts=NULL WHERE id=? AND claim_ts=? AND "
                            "status IN ('serving', 'served')", (pr_id, ts))

    def finish(self, pr_id, ts):
        with self.lock:
            return self.db.execute("UPDATE quotes SET status='served' WHERE id=? AND status='serving' AND claim_ts=?",
                                   (pr_id, ts)).rowcount == 1


class Seller:
    def __init__(self, account, hsp_factory, *, db_path, chain=chain_rpc, reader=address_profile, clock=time.time,
                 log=print, confirmations=config.A2A_MIN_CONFIRMATIONS):
        self.account, self.hsp_factory, self.chain, self.reader = account, hsp_factory, chain, reader
        self.clock, self.log, self.confirmations = clock, log, confirmations
        self.pay_to = account.address
        self.store = Store(db_path)
        self.quote_limit = Limiter(config.A2A_RATE_QUOTES_PER_MIN)
        self.claim_limit = Limiter(config.A2A_RATE_CLAIMS_PER_MIN)
        self.unpaid_until, self.unpaid_lock = {}, threading.Lock()   # quote id -> don't ask the gateway before this time

    def card(self, base_url):
        return {"name": "HSK Meter Receiving Agent", "version": "1.0.0", "url": base_url,
                "description": "Sells public HashKey Chain address profiles per request, paid through the HSP Gateway.",
                "capabilities": {"streaming": False}, "defaultInputModes": ["application/json"],
                "defaultOutputModes": ["application/json"],
                "skills": [{"id": "address-profile", "name": "Address profile", "tags": ["hashkey", "data"],
                            "description": "HSK balance, USDC.e balance, nonce and contract flag of one address.",
                            "examples": ["GET /v1/address-profile?address=0x..."]}],
                "payment": {"protocol": "hsp-agentic-payment", "merchant_name": config.MERCHANT_NAME,
                            "pay_to": self.pay_to, "networks": sorted(config.A2A_TOKENS),
                            "prices_usd": {k: str(v) for k, v in config.A2A_PRICES_USD.items()}}}

    def quote(self, path, resource, payer, network):
        now = int(self.clock())
        cid, token, coin, decimals, protocol = config.A2A_TOKENS[network]
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d%H%M%S")
        pr_id = f"HSKM-PR-{stamp}-{secrets.token_hex(8)}"
        usd = config.A2A_PRICES_USD[path]
        expires = now + config.A2A_QUOTE_TTL_S
        self.store.add(Quote(pr_id, resource, payer.lower(), str(usd), network, expires, "open"), now,
                       config.A2A_MAX_OPEN_QUOTES)
        cond = {"type": "hsp-payment-conditions/v1", "merchant_name": config.MERCHANT_NAME, "pay_to": self.pay_to,
                "amount": {"currency": "USD", "value": str(usd)}, "network": network, "chain_id": cid, "token": token,
                "coin": coin, "decimals": decimals, "protocol": protocol, "resource": resource, "payer": payer.lower(),
                "payment_request_id": pr_id, "expires_at": flow.utc_iso(expires), "gateway": config.MCP_URL}
        self.log(f"quote {pr_id} {usd} USD on {network} for {resource}")
        return cond, sign_text(self.account, canonical(cond))

    def _band(self, usd, decimals):
        unit, slack = Decimal(10) ** decimals, Decimal(1) + config.PRICE_SLACK
        return (int((usd * unit / slack).to_integral_value(ROUND_FLOOR)),
                int((usd * unit * slack).to_integral_value(ROUND_CEILING)))

    def verify_paid(self, q):
        """Both checks, or a Refusal naming the first one that failed. Returns the evidence."""
        cid, token, _, decimals, _ = config.A2A_TOKENS[q.network]
        usd = Decimal(q.usd)
        hsp = None
        try:
            hsp = self.hsp_factory()
            reply = hsp.call("query_payment", need=("expected_merchant_name",), payment_request_id=q.id,
                             expected_merchant_name=config.MERCHANT_NAME)
        except flow.HspError as e:
            if e.code == "10003":         # HSP's "payment not found": no order or payment exists for this id yet
                raise Refusal(402, "not_paid_yet", "HSP knows no payment for this quote", retry_after=15) from None
            self.log(f"gateway error: {flow._snip(str(e), 160)}")
            raise Refusal(503, "gateway_unavailable", "HSP could not be asked: retry", retry_after=15) from None
        except (flow.SchemaMismatch, McpError) as e:
            self.log(f"gateway error: {flow._snip(str(e), 160)}")
            raise Refusal(503, "gateway_unavailable", "HSP could not be asked: retry", retry_after=15) from None
        finally:
            if hsp is not None:
                hsp.mcp.close()
        status, pay = flow._status_of(reply)
        if status in config.POLLING_STATUSES or status == "payment-required":
            raise Refusal(402, "not_paid_yet", f"HSP status {flow._snip(status, 40)}", retry_after=15)
        if status not in config.PAID_STATUSES or pay.get("status") not in (None, status):
            raise Refusal(402, "payment_not_safe", f"HSP status {flow._snip(status, 40)}")

        def same(field, ours):
            v = pay.get(field)
            if not isinstance(v, str) or v.lower() != ours.lower():
                raise Refusal(402, f"mismatch_{field}", f"HSP says {flow._snip(v, 60)}, the quote says {ours}")
        if reply.get("attribution_verified") is not True or reply.get("merchant_name") != config.MERCHANT_NAME:
            raise Refusal(402, "attribution", "HSP does not attribute this payment to our merchant_name")
        same("payment_request_id", q.id)
        same("to_pay_address", self.pay_to)
        same("token_address", token)
        same("chain", f"eip155:{cid}")
        same("payer_address", q.payer)
        try:
            if Decimal(str(pay.get("usd_amount"))) != usd:
                raise ArithmeticError
            amount = Decimal(str(pay.get("pay_amount")))
            if not amount.is_finite() or abs(amount.adjusted()) > 30:     # a huge exponent would stall the server
                raise ArithmeticError
            gateway_raw = int(amount * Decimal(10) ** decimals)
        except ArithmeticError:
            raise Refusal(402, "mismatch_amount", "HSP's amount is not the quoted price") from None
        tx = pay.get("tx_signature")
        if not isinstance(tx, str) or not TX_RE.match(tx):
            raise Refusal(402, "not_paid_yet", "HSP names no transaction yet", retry_after=15)
        tx = tx.lower()                   # the UNIQUE column must see one spelling of a hash
        self.log(f"HSP says {status}, attribution_verified true; merchant, recipient, token, chain, amount and payer match")

        try:
            head = self.chain.head(cid)
            rcpt = self.chain.receipt(cid, tx)
        except chain_rpc.ChainError as e:
            self.log(f"chain error: {flow._snip(str(e), 160)}")
            raise Refusal(503, "chain_unavailable", "the chain could not be read: retry", retry_after=15) from None
        if rcpt is None:
            raise Refusal(402, "not_on_chain_yet", "the node does not know this transaction yet", retry_after=15)
        if str(rcpt.get("transactionHash")).lower() != tx.lower() or rcpt.get("status") != "0x1":
            raise Refusal(402, "tx_failed", "the transaction did not succeed on-chain")
        hits = [t for t in self.chain.transfers(rcpt) if t["token"] == token.lower() and t["to"] == self.pay_to.lower()
                and t["from"] == q.payer]
        if len(hits) != 1:
            raise Refusal(402, "no_matching_transfer", f"{len(hits)} matching token Transfers in the transaction")
        low, high = self._band(usd, decimals)
        value = hits[0]["value"]
        if not low <= value <= high or value != gateway_raw:
            raise Refusal(402, "wrong_value", f"{value} raw on-chain, want {low}..{high} and HSP's {gateway_raw}")
        if not BLOCK_RE.match(str(rcpt.get("blockNumber"))):
            raise Refusal(402, "tx_failed", "malformed receipt")
        block = int(rcpt["blockNumber"], 16)
        depth = head - block + 1
        if depth < self.confirmations:
            raise Refusal(402, "not_enough_confirmations", f"{depth} of {self.confirmations}", retry_after=15)
        return {"tx": tx, "status": status, "attribution_verified": True, "network": q.network, "chain_id": cid,
                "value_raw": str(value), "block": block, "confirmations": depth}

    def claim(self, resource, pr_id, proof, address):
        q = self.store.get(pr_id) if isinstance(pr_id, str) and ID_RE.match(pr_id) else None
        if q is None:
            raise Refusal(402, "unknown_quote", "no such quote: ask again for conditions")
        if q.resource != resource:
            raise Refusal(403, "wrong_resource", "this payment was quoted for another resource")
        try:
            signer = recover_text(claim_message(pr_id, resource), proof) if SIG_RE.match(proof or "") else None
        except Exception:
            signer = None
        if not signer or signer.lower() != q.payer:
            raise Refusal(403, "bad_proof", "X-HSP-Proof must be signed by the wallet the quote was issued to")
        if q.status == "served":
            raise Refusal(409, "already_served", "this quote was served; a new request needs a new payment")
        now = int(self.clock())
        if now > q.expires + config.A2A_CLAIM_GRACE_S:
            raise Refusal(410, "quote_expired", "the claim window for this quote has passed")
        with self.unpaid_lock:
            if self.unpaid_until.get(pr_id, 0) > now:         # strangers must not drive traffic to the gateway from our IP
                raise Refusal(402, "not_paid_yet", "asked HSP moments ago: retry later", retry_after=15)
        try:
            evidence = self.verify_paid(q)
        except Refusal as r:
            if r.rule == "not_paid_yet":
                with self.unpaid_lock:
                    if len(self.unpaid_until) > 5000:
                        self.unpaid_until.clear()
                    self.unpaid_until[pr_id] = now + config.A2A_UNPAID_RETRY_S
            raise
        ts = int(self.clock())
        self.store.begin_serving(pr_id, evidence["tx"], ts)
        try:
            data = self.reader(address)
        except Exception as e:
            self.store.release(pr_id, ts)
            self.log(f"data source failed: {type(e).__name__}")
            raise Refusal(502, "data_source_failed", "nothing lost: retry this claim", retry_after=15) from None
        if not self.store.finish(pr_id, ts):
            raise Refusal(409, "already_served", "another claim took over this quote")
        self.log(f"chain agrees: Transfer of {evidence['value_raw']} raw to pay_to, {evidence['confirmations']} confirmations, "
                 f"tx {evidence['tx']}")
        self.log(f"served {pr_id}, once")
        return ts, {"data": data, "payment": {"payment_request_id": pr_id, "merchant_name": config.MERCHANT_NAME, **evidence}}


class Handler(BaseHTTPRequestHandler):
    seller = None
    server_version = "hsk-meter-receiving-agent/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15

    def log_message(self, *a):
        pass

    def send_json(self, code, obj, retry_after=None):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            self.route()
        except Refusal as r:
            self.seller.log(f"refused {r.http} {r.rule}: {r.detail}")
            self.send_json(r.http, {"error": r.rule, "detail": r.detail}, r.retry_after)
        except Exception as e:          # never a traceback to a stranger
            self.seller.log(f"internal error {type(e).__name__}")
            self.send_json(500, {"error": "internal"})

    def route(self):
        s = self.seller
        now = s.clock()
        if len(self.path) > MAX_PATH:
            raise Refusal(414, "too_long")
        u = urlsplit(self.path)
        if u.path == "/health":
            return self.send_json(200, {"status": "ok"})
        if u.path == CARD_PATH:
            return self.send_json(200, s.card(f"http://{self.server.server_address[0]}:{self.server.server_address[1]}"))
        if u.path not in config.A2A_PRICES_USD:
            raise Refusal(404, "not_found")
        try:
            q = parse_qs(u.query, max_num_fields=4)
        except ValueError:
            raise Refusal(400, "bad_request", "too many query fields") from None
        address = (q.get("address") or [""])[0]
        network = (q.get("network") or ["base-sepolia"])[0]
        if not ADDR_RE.match(address):
            raise Refusal(400, "bad_address", "address must be 0x + 40 hex characters")
        if network not in config.A2A_TOKENS:
            raise Refusal(400, "bad_network", f"network must be one of {sorted(config.A2A_TOKENS)}")
        address = address.lower()
        resource = f"GET {u.path}?address={address}"
        ip = self.client_address[0]
        pr_id = self.headers.get("X-HSP-Payment-Request-Id")
        if pr_id:
            if not s.claim_limit.allow(ip, now):
                raise Refusal(429, "rate_limited", retry_after=60)
            ts, reply = s.claim(resource, pr_id, self.headers.get("X-HSP-Proof"), address)
            try:
                return self.send_json(200, reply)
            except OSError:               # the buyer is gone: the answer was not delivered, so the claim stays open
                s.store.release(pr_id, ts)
                return s.log(f"could not deliver {pr_id}: claim reopened")
        payer = self.headers.get("X-Payer") or ""
        if not ADDR_RE.match(payer):
            raise Refusal(400, "bad_payer", "send X-Payer: the wallet that will pay")
        if not s.quote_limit.allow(ip, now):
            raise Refusal(429, "rate_limited", retry_after=60)
        cond, sig = s.quote(u.path, resource, payer, network)
        self.send_json(402, {"error": "payment_required", "conditions": cond, "signature": sig})


def make_server(seller, host="127.0.0.1", port=0):
    handler = type("H", (Handler,), {"seller": seller})
    return ThreadingHTTPServer((host, port), handler)


def live_hsp():
    hsp = flow.Hsp(McpClient(config.MCP_URL, total_timeout=config.A2A_GATEWAY_TIMEOUT_S))
    hsp.discover()
    return hsp


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8411)
    args = ap.parse_args()
    account = wallets.load_account(config.MERCHANT_ENV)

    def log(msg):
        print(f"{flow.utc_iso()} [receiving agent] {msg}", flush=True)
    rows, _ = flow.parse_chain_config(live_hsp().call("get_chain_config"))
    offered = {r.address.lower() for r in rows}
    for name, t in config.A2A_TOKENS.items():
        if t[1].lower() not in offered:
            raise SystemExit(f"{name} token is not in the live get_chain_config: refusing to quote it")
    srv = make_server(Seller(account, live_hsp, db_path=os.path.join(config.STATE_DIR, "a2a_seller.sqlite3"), log=log),
                      args.host, args.port)
    log(f"listening on {args.host}:{srv.server_address[1]}; pay_to {account.address}; "
        f"merchant_name {config.MERCHANT_NAME!r}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
