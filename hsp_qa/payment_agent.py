#!/usr/bin/env python3
"""Payment Agent: buys one resource from a Receiving Agent, paying through the official HSP gateway.

  1. Ask for the resource and read the 402 payment conditions.
  2. Refuse before any order unless the conditions are signed by the seller we pinned, name our merchant, this exact
     resource and our wallet, stay within the price limit and session cap, and match get_chain_config.
  3. Build the Cart Mandate from them (the seller's payment_request_id becomes the order's), let the MCP prepare the
     payment, pass the reply through the signing firewall, sign with our key, finalize, submit: HSP's Gateway executes.
  4. Wait for payment-safe, then claim with a proof that we hold the paying wallet. The seller verifies on its own.

  payment_agent.py --seller 0x... --address 0x... [--url http://127.0.0.1:8411] [--network base-sepolia]
                   [--max-usd 0.05] [--session-cap 0.25] [--replay-check] [--max-wait 1800]
                   [--refuse-unserved-within SECONDS] [--resume PAYMENT_REQUEST_ID]

  Exit codes: 0 paid and served; 2 stopped (nothing paid, or HSP says the payment failed); 3 still settling (paid once,
  not yet safe or not yet served: run again with --resume <payment_request_id>, which only reads and claims).
  --resume never creates an order and never signs: it reads HSP for a payment this wallet already signed, then claims.
  --refuse-unserved-within refuses a NEW purchase while a payment signed in that window has not been served yet.
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
SETTLE_NOTE_S = 60                # say "still settling" with the identifiers after this long
DEMO_QUERY_ERRORS = 12            # consecutive failed HSP reads (15 s apart) before giving up on reading
EXPLORER_TX = {"arbitrum-sepolia": "https://arbitrum-sepolia.blockscout.com/tx/",
               "base-sepolia": "https://base-sepolia.blockscout.com/tx/",
               "sepolia": "https://eth-sepolia.blockscout.com/tx/",
               "hashkey-testnet": "https://testnet-explorer.hskchain.net/tx/"}
SIGNED_STEPS = ("signed",)
SERVED_STEPS = ("a2a_served",)


class Refused(Exception):
    """The conditions failed a policy rule: no order was created and nothing was signed."""

    def __init__(self, rule, detail):
        super().__init__(f"{rule}: {detail}")
        self.rule, self.detail = rule, detail


class StillSettling(Exception):
    """Paid once (submitted), but not yet payment-safe or not yet served. Resume reads and claims; it never pays."""

    def __init__(self, flow_id, pr_id, status, why):
        super().__init__(f"{why}: status {status}, flow_id {flow_id}, payment_request_id {pr_id}")
        self.flow_id, self.pr_id, self.status, self.why = flow_id, pr_id, status, why


def unserved_signed(rows, now, window_s):
    """payment_request_ids this wallet signed for within window_s seconds and that were never served."""
    signed, served = {}, set()
    for r in rows:
        pr = r.get("payment_request_id")
        if not isinstance(pr, str):
            continue
        if r.get("step") in SERVED_STEPS:
            served.add(pr)
        elif r.get("step") in SIGNED_STEPS:
            ts = flow._rfc(r.get("ts"))
            if ts is not None and now - ts <= window_s:
                signed[pr] = r.get("flow_id")
    return [(pr, fid) for pr, fid in signed.items() if pr not in served]


def signed_flow(rows, pr_id):
    """flow_id of a payment this wallet signed for pr_id, or None. A resume is only allowed for such an id."""
    for r in rows:
        if r.get("payment_request_id") == pr_id and r.get("step") in SIGNED_STEPS and isinstance(r.get("flow_id"), str):
            return r["flow_id"]
    return None


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
        self._settled(res, pr_id)
        return self.claim(url, resource, pr_id, res["flow_id"], replay_check)

    def _settled(self, res, pr_id):
        """Raise unless HSP reports payment-safe/-finalized with attribution_verified true."""
        status = res.get("status")
        if status in config.PAID_STATUSES and res.get("attribution_verified") is True:
            self.say(f"[4/5] HSP Gateway executed it: {status}, attribution_verified true, tx {res.get('tx_signature')}")
            return
        self.receipts.log("a2a_unpaid", payment_request_id=pr_id, status=status)
        if status in config.PAID_STATUSES:
            raise Refused("attribution", f"HSP status {status} but attribution_verified is "
                                         f"{res.get('attribution_verified')}, flow_id {res.get('flow_id')}")
        if status == "payment-failed" or status not in config.POLLING_STATUSES | {"query_failing", "unknown", None}:
            raise Refused("not_paid", f"HSP status {status}, flow_id {res.get('flow_id')}")
        raise StillSettling(res.get("flow_id"), pr_id, status, "not payment-safe yet")

    def resume(self, url, resource, pr_id, replay_check=False):
        """Finish a payment this wallet already signed: read HSP, then claim. Never creates an order, never signs."""
        flow_id = signed_flow(self.receipts.rows(), pr_id)
        if flow_id is None:
            raise Refused("resume", "this wallet never signed a payment for that payment_request_id")
        self.say(f"[resume] reading HSP for flow_id {flow_id}, payment_request_id {pr_id} (no new order, no signature)")
        ctx = flow.Ctx(hsp=self.hsp, account=self.account, pay_to=self.policy.seller, receipts=self.receipts,
                       approve=lambda *a: False, balance_fn=None, out=lambda m: self.say("      " + m))
        for k, v in self.ctx_overrides.items():
            setattr(ctx, k, v)
        res = flow.poll(ctx, {"flow_id": flow_id, "payment_request_id": pr_id})
        self._settled(res, pr_id)
        return self.claim(url, resource, pr_id, flow_id, replay_check)

    def claim(self, url, resource, pr_id, flow_id, replay_check=False):
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
            rule = out.get("error") if isinstance(out, dict) else None
            if st in (402, 502, 503) and rule in ("not_paid_yet", "gateway_unavailable", None):
                raise StillSettling(flow_id, pr_id, f"seller {st} {flow._snip(str(rule), 40)}", "paid, not served yet")
            raise Refused("claim_failed", f"the seller answered {st}: {flow._snip(str(out), 200)}")
        depth = out["payment"].get("confirmations")
        self.receipts.log("a2a_served", payment_request_id=pr_id, flow_id=flow_id, resource=resource,
                          tx_signature=out["payment"]["tx"], seller_confirmations=depth)
        self.say(f"[5/5] paid data served once, after the seller's own check on-chain: {flow._snip(str(depth), 20)} confirmations")
        if replay_check:
            st2, again = self._request(url, resource, pr_id)
            err = flow._snip(str(again.get("error")), 60) if isinstance(again, dict) else None
            self.say(f"      same claim again: {st2} {err}")
            out["replay"] = {"status": st2, "error": err}
        return out

    def onchain_sender(self, network, tx, spenders, rpc=chain.rpc):
        """Read who broadcast the payment from the pinned testnet RPC; label HSP's relayer from get_chain_config."""
        cid = config.A2A_TOKENS.get(network, (None,))[0]
        link = EXPLORER_TX.get(network, "") + tx if network in EXPLORER_TX else None
        frm = None
        if cid is not None and ra.TX_RE.match(str(tx)):
            try:
                t = rpc(cid, "eth_getTransactionByHash", [tx])
                frm = t.get("from") if isinstance(t, dict) else None
            except Exception as e:      # chain.ChainError or transport: the explorer link still works
                self.say(f"      on-chain read failed ({type(e).__name__}); open the explorer link")
        if isinstance(frm, str) and firewall.ADDR_RE.match(frm):
            who = "HSP's relayer (listed in get_chain_config)" if frm.lower() in {a.lower() for a in spenders} \
                else "not an address HSP's config lists"
            self.say(f"      on-chain: sent by {frm}, {who}; the payer paid no gas")
        if link:
            self.say(f"      explorer: {link}")
        return frm, link


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8411")
    ap.add_argument("--seller", required=True, help="pinned Receiving Agent address")
    ap.add_argument("--address", required=True, help="the address to buy a profile of")
    ap.add_argument("--network", default="base-sepolia")
    ap.add_argument("--max-usd", default="0.05")
    ap.add_argument("--session-cap", default="0.25")
    ap.add_argument("--replay-check", action="store_true")
    ap.add_argument("--max-wait", type=int, default=config.MAX_WAIT_S, help="seconds to keep reading HSP (30..3600)")
    ap.add_argument("--refuse-unserved-within", type=int, default=0, metavar="SECONDS",
                    help="refuse a new purchase while a payment signed this recently is unserved (0 = off)")
    ap.add_argument("--resume", metavar="PAYMENT_REQUEST_ID", help="read + claim a payment already signed; never pays")
    args = ap.parse_args()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.address) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.seller):
        raise SystemExit("--seller and --address must be 0x + 40 hex characters")
    if not 30 <= args.max_wait <= 3600 or not 0 <= args.refuse_unserved_within <= 86400:
        raise SystemExit("--max-wait must be 30..3600 and --refuse-unserved-within 0..86400")
    if args.resume is not None and not ra.ID_RE.match(args.resume):
        raise SystemExit("--resume takes a payment_request_id")
    if args.network not in EXPLORER_TX:
        raise SystemExit(f"--network must be one of {', '.join(sorted(EXPLORER_TX))}")
    resource = f"GET /v1/address-profile?address={args.address.lower()}"
    url = args.url.rstrip("/") + "/v1/address-profile?" + urllib.parse.urlencode(
        {"address": args.address.lower(), "network": args.network})
    receipts = flow.Receipts(config.RECEIPTS_LIVE, "live")
    t0 = time.time()

    def say(m):
        print(f"{flow.utc_iso()} +{time.time() - t0:5.1f}s [payment agent] {m}", flush=True)

    if args.resume is not None and signed_flow(receipts.rows(), args.resume) is None:
        say(f"REFUSED: this wallet never signed a payment for {args.resume}; --resume only finishes our own payments")
        return 2
    if args.resume is None and args.refuse_unserved_within:
        open_ids = unserved_signed(receipts.rows(), time.time(), args.refuse_unserved_within)
        if open_ids:
            pr, fid = open_ids[-1]
            say(f"REFUSED: a payment signed in the last {args.refuse_unserved_within // 60} min is not served yet "
                f"(flow_id {fid}, payment_request_id {pr}). Paying again would pay twice. Finish it with --resume {pr}")
            return 2
    account = wallets.load_payer()
    hsp = flow.Hsp(McpClient(config.MCP_URL))
    hsp.discover()
    cfg = hsp.call("get_chain_config")
    rows, rejects = flow.parse_chain_config(cfg)
    flow.require_rows(cfg, rows, rejects)
    policy = Policy(seller=args.seller, max_usd=Decimal(args.max_usd), session_cap_usd=Decimal(args.session_cap))
    agent = PaymentAgent(account, hsp, rows, policy, receipts, say=say,
                         ctx_overrides={"max_wait": args.max_wait, "max_query_errors": DEMO_QUERY_ERRORS,
                                        "settle_note_s": SETTLE_NOTE_S})
    try:
        if args.resume is not None:
            out = agent.resume(url, resource, args.resume, replay_check=args.replay_check)
        else:
            out = agent.buy(url, resource, replay_check=args.replay_check)
    except StillSettling as e:
        say(f"STILL SETTLING ({e.why}, {e.status}). Paid once; nothing will be sent again. flow_id {e.flow_id}")
        say(f"finish it later (reads + claims only): --resume {e.pr_id}")
        return 3
    except (Refused, flow.FlowStop, flow.HspError, flow.SchemaMismatch, McpError, firewall.Refused) as e:
        say(f"STOP {type(e).__name__}: {e}")
        return 2
    tx = out["payment"]["tx"]
    token = next((r for r in rows if r.network == args.network), None)
    agent.onchain_sender(args.network, tx, token.spenders if token else ())
    print(json.dumps(out, indent=1)[:4000])    # the seller's answer is untrusted text: capped, JSON-escaped
    return 0


if __name__ == "__main__":
    sys.exit(main())
