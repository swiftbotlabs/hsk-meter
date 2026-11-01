#!/usr/bin/env python3
"""Payment Agent: buys one resource from a Receiving Agent, paying through the official HSP gateway.

  1. Ask for the resource and read the 402 payment conditions.
  2. Refuse before any order unless the conditions are signed by the seller we pinned, name our merchant, this exact
     resource and our wallet, stay within the price limit and session cap, and match get_chain_config.
  3. Build the Cart Mandate from them (the seller's payment_request_id becomes the order's), let the MCP prepare the
     payment, pass the reply through the signing firewall, sign with our key, finalize, submit: HSP's Gateway executes.
  4. Wait for payment-safe, then claim with a proof that we hold the paying wallet. The seller verifies on its own.

  payment_agent.py --seller 0x... --address 0x... [--url http://127.0.0.1:8411] [--network base-sepolia]
                   [--max-usd 0.05] [--session-cap 0.25] [--replay-check]
"""
import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import chain  # noqa: E402
import config  # noqa: E402
import firewall  # noqa: E402
import flow  # noqa: E402
import receiving_agent as ra  # noqa: E402
import wallets  # noqa: E402
from mcp_client import McpClient, McpError  # noqa: E402

COND_KEYS = {"type", "merchant_name", "pay_to", "amount", "network", "chain_id", "token", "coin", "decimals", "protocol",
             "resource", "payer", "payment_request_id", "expires_at", "gateway"}
MIN_TIME_LEFT_S = 60
CLAIM_TRIES = 12
CLAIM_WAIT_S = 15


class Refused(Exception):
    """The conditions failed a policy rule: no order was created and nothing was signed."""

    def __init__(self, rule, detail):
        super().__init__(f"{rule}: {detail}")
        self.rule, self.detail = rule, detail


@dataclass
class Policy:
    seller: str                       # pinned: the address that must sign the conditions and receive the money
    max_usd: Decimal = Decimal("0.05")
    session_cap_usd: Decimal = Decimal("0.25")
    merchant_name: str = config.MERCHANT_NAME
    networks: tuple = tuple(config.A2A_TOKENS)


def http_get(url, headers, timeout=20):
    """(status, parsed JSON or None). No redirects, capped body. The seller's answer is untrusted data."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as r:
            status, raw = r.status, r.read(1_000_001)
    except urllib.error.HTTPError as e:
        with e:
            status, raw = e.code, e.read(1_000_001)
    if len(raw) > 1_000_000:
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, None


def check_conditions(body, policy, resource, payer, rows, now, spent=Decimal(0)):
    """Everything is checked before the MCP hears of this purchase. Returns (conditions, token row, USD amount)."""
    if not isinstance(body, dict) or not isinstance(body.get("conditions"), dict):
        raise Refused("malformed", "no payment conditions in the 402")
    c, sig = body["conditions"], body.get("signature")
    if set(c) != COND_KEYS or c.get("type") != "hsp-payment-conditions/v1":
        raise Refused("malformed", "unexpected condition fields")
    try:
        signer = ra.recover_text(ra.canonical(c), sig) if isinstance(sig, str) and ra.SIG_RE.match(sig) else None
    except Exception:
        signer = None
    if not signer or signer.lower() != policy.seller.lower():
        raise Refused("seller_signature", "the conditions are not signed by the seller we pinned")
    if str(c.get("pay_to")).lower() != policy.seller.lower():
        raise Refused("pay_to", "pay_to is not the seller we pinned")
    if c.get("merchant_name") != policy.merchant_name:
        raise Refused("merchant", "unexpected merchant_name")
    if c.get("resource") != resource:
        raise Refused("resource", "the conditions are for another resource")
    if str(c.get("payer")).lower() != payer.lower():
        raise Refused("payer", "the conditions name another payer")
    if c.get("gateway") != config.MCP_URL:
        raise Refused("gateway", "the conditions name another gateway")
    if not isinstance(c.get("payment_request_id"), str) or not ra.ID_RE.match(c["payment_request_id"]):
        raise Refused("malformed", "bad payment_request_id")
    amount = c.get("amount") if isinstance(c.get("amount"), dict) else {}
    try:
        usd = Decimal(str(amount.get("value")))
    except ArithmeticError:
        raise Refused("amount", "the price is not a number") from None
    if amount.get("currency") != "USD" or not usd.is_finite() or usd != usd.quantize(Decimal("0.01")):
        raise Refused("amount", "the price must be USD in whole cents")
    if not config.MIN_PAYMENT_USD <= usd <= policy.max_usd:
        raise Refused("price_limit", f"{usd} USD is outside 0.01..{policy.max_usd}")
    if spent + usd > policy.session_cap_usd:
        raise Refused("session_cap", f"{spent} + {usd} USD exceeds the session cap {policy.session_cap_usd}")
    if c.get("network") not in policy.networks:
        raise Refused("network", "network not allowed by our policy")
    token = next((r for r in rows if r.network == c["network"] and r.address.lower() == str(c.get("token")).lower()), None)
    if token is None or token.chain_id != c.get("chain_id") or token.protocol != c.get("protocol") \
            or token.decimals != c.get("decimals"):
        raise Refused("token", "the conditions do not match get_chain_config")
    expires = flow._rfc(c.get("expires_at"))
    if expires is None or expires - now < MIN_TIME_LEFT_S:
        raise Refused("expiry", "the conditions expire too soon to pay")
    return c, token, usd


class PaymentAgent:
    def __init__(self, account, hsp, rows, policy, receipts, *, balance_fn=chain.balance_of, get=http_get,
                 say=print, sleep=time.sleep, clock=time.time, ctx_overrides=None):
        self.account, self.hsp, self.rows, self.policy, self.receipts = account, hsp, rows, policy, receipts
        self.balance_fn, self.get, self.say, self.sleep, self.clock = balance_fn, get, say, sleep, clock
        self.ctx_overrides = ctx_overrides or {}
        self.spent = Decimal(0)

    def _request(self, url, resource, pr_id=None):
        h = {"X-Payer": self.account.address, "User-Agent": config.USER_AGENT}
        if pr_id:
            h["X-HSP-Payment-Request-Id"] = pr_id
            h["X-HSP-Proof"] = ra.sign_text(self.account, ra.claim_message(pr_id, resource))
            return self.get(url, h, timeout=config.A2A_CLAIM_TIMEOUT_S)
        return self.get(url, h)

    def _approve(self, stage, s):
        """The policy check above is the approval; this only reports each step."""
        if stage == "sign":
            self.say(f"      firewall passed: {s['amount_human']} {s['coin']} to {s['recipient']} on chain {s['chain_id']}; "
                     "wallet signs")
        else:
            self.say("      Payment Mandate ready; submit_payment hands it to HSP's Gateway")
        return True

    def buy(self, url, resource, replay_check=False):
        st, body = self._request(url, resource)
        if st != 402:
            raise Refused("no_conditions", f"expected 402, got {st}")
        self.say(f"[1/5] 402 from the Receiving Agent: conditions for {resource}")
        try:
            c, token, usd = check_conditions(body, self.policy, resource, self.account.address, self.rows,
                                             self.clock(), self.spent)
        except Refused as e:
            self.receipts.log("a2a_refused", rule=e.rule, detail=e.detail, resource=resource)
            raise
        pr_id = c["payment_request_id"]
        self.say(f"[2/5] conditions verified: signed by the pinned seller {self.policy.seller}, {usd} USD, "
                 f"{c['network']} {c['coin']}, payment_request_id {pr_id}")
        self.receipts.log("a2a_conditions", payment_request_id=pr_id, resource=resource, amount_usd=str(usd),
                          network=c["network"], seller=self.policy.seller)
        ctx = flow.Ctx(hsp=self.hsp, account=self.account, pay_to=c["pay_to"], receipts=self.receipts,
                       approve=self._approve, balance_fn=self.balance_fn, out=lambda m: self.say("      " + m))
        for k, v in self.ctx_overrides.items():
            setattr(ctx, k, v)
        self.say("[3/5] Cart Mandate -> prepare_payment -> signing firewall -> wallet signature -> "
                 "finalize_payment -> submit_payment")
        try:
            res = flow.run_payment(ctx, token, usd, label=resource, pr_id=pr_id, expiry=flow._rfc(c["expires_at"]),
                                   cap_usd=self.policy.max_usd * (1 + config.PRICE_SLACK), fee_allowance_usd=Decimal(0))
        finally:
            self.spent += self.receipts.committed_usd(pr_id)    # every signature counts, also a re-signed quote
        if res.get("status") not in config.PAID_STATUSES or res.get("attribution_verified") is not True:
            self.receipts.log("a2a_unpaid", payment_request_id=pr_id, status=res.get("status"))
            raise Refused("not_paid", f"HSP status {res.get('status')}, flow_id {res.get('flow_id')}")
        self.say(f"[4/5] HSP Gateway executed it: {res['status']}, attribution_verified true, tx {res.get('tx_signature')}")
        st, out = None, None
        for _ in range(CLAIM_TRIES):
            try:
                st, out = self._request(url, resource, pr_id)
            except (OSError, http.client.HTTPException) as e:
                self.receipts.log("a2a_claim_error", payment_request_id=pr_id, error=type(e).__name__)
                self.say(f"      claim got no answer ({type(e).__name__}); keeping the identifiers, trying again")
                self.sleep(CLAIM_WAIT_S)
                continue
            if st == 200 and isinstance(out, dict) and isinstance(out.get("payment"), dict) \
                    and ra.TX_RE.match(str(out["payment"].get("tx"))):
                break
            rule = out.get("error") if isinstance(out, dict) else None
            self.say(f"      seller says {st} {flow._snip(str(rule), 60)}")
            if st not in (402, 502, 503) or rule in ("payment_not_safe", "attribution", "tx_failed", "wrong_value",
                                                     "no_matching_transfer") or str(rule).startswith("mismatch"):
                break
            self.sleep(CLAIM_WAIT_S)
        paid_ok = st == 200 and isinstance(out, dict) and isinstance(out.get("payment"), dict) \
            and ra.TX_RE.match(str(out["payment"].get("tx")))
        if not paid_ok:
            self.receipts.log("a2a_claim_failed", payment_request_id=pr_id, status=st)
            raise Refused("claim_failed", f"the seller answered {st}: {flow._snip(str(out), 200)}")
        depth = out["payment"].get("confirmations")
        self.receipts.log("a2a_served", payment_request_id=pr_id, flow_id=res["flow_id"], resource=resource,
                          tx_signature=out["payment"]["tx"], seller_confirmations=depth)
        self.say(f"[5/5] paid data served once, after the seller's own check on-chain: {flow._snip(str(depth), 20)} confirmations")
        if replay_check:
            st2, again = self._request(url, resource, pr_id)
            err = flow._snip(str(again.get("error")), 60) if isinstance(again, dict) else None
            self.say(f"      same claim again: {st2} {err}")
            out["replay"] = {"status": st2, "error": err}
        return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8411")
    ap.add_argument("--seller", required=True, help="pinned Receiving Agent address")
    ap.add_argument("--address", required=True, help="the address to buy a profile of")
    ap.add_argument("--network", default="base-sepolia")
    ap.add_argument("--max-usd", default="0.05")
    ap.add_argument("--session-cap", default="0.25")
    ap.add_argument("--replay-check", action="store_true")
    args = ap.parse_args()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.address) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.seller):
        raise SystemExit("--seller and --address must be 0x + 40 hex characters")
    resource = f"GET /v1/address-profile?address={args.address.lower()}"
    url = args.url.rstrip("/") + "/v1/address-profile?" + urllib.parse.urlencode(
        {"address": args.address.lower(), "network": args.network})
    account = wallets.load_payer()
    hsp = flow.Hsp(McpClient(config.MCP_URL))
    hsp.discover()
    cfg = hsp.call("get_chain_config")
    rows, rejects = flow.parse_chain_config(cfg)
    flow.require_rows(cfg, rows, rejects)
    policy = Policy(seller=args.seller, max_usd=Decimal(args.max_usd), session_cap_usd=Decimal(args.session_cap))
    agent = PaymentAgent(account, hsp, rows, policy, flow.Receipts(config.RECEIPTS_LIVE, "live"),
                         say=lambda m: print(f"{flow.utc_iso()} [payment agent] {m}", flush=True))
    try:
        out = agent.buy(url, resource, replay_check=args.replay_check)
    except (Refused, flow.FlowStop, flow.HspError, flow.SchemaMismatch, McpError, firewall.Refused) as e:
        print(f"{flow.utc_iso()} [payment agent] STOP {type(e).__name__}: {e}", flush=True)
        return 2
    print(json.dumps(out, indent=1)[:4000])    # the seller's answer is untrusted text: capped, JSON-escaped
    return 0


if __name__ == "__main__":
    sys.exit(main())
