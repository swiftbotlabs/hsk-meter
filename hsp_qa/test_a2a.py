"""Offline tests of the A2A flow: Receiving Agent and Payment Agent over real HTTP on 127.0.0.1, against the local mock
of HSP's MCP server (live config shape, hex chainId) and a stand-in chain that returns real-shaped receipts.

Every refusal has a positive control: the happy path runs through the same Seller with nothing changed.
Run: python3 hsp_qa/test_a2a.py
"""
import atexit
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal

TMP = tempfile.mkdtemp(prefix="hsp_a2a_test_")
atexit.register(shutil.rmtree, TMP, True)
os.environ["HSP_QA_SECRETS_DIR"] = os.path.join(TMP, "secrets")
os.environ["HSP_QA_STATE_DIR"] = os.path.join(TMP, "state")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import chain as chain_rpc  # noqa: E402
import config  # noqa: E402
import firewall  # noqa: E402
import flow  # noqa: E402
import mcp_client  # noqa: E402
import mock_server  # noqa: E402
import payment_agent as pa  # noqa: E402
import receiving_agent as ra  # noqa: E402
from eth_account import Account  # noqa: E402

TARGET = "0x78c8476772bc209c3df09da5e129d0879cd57f8c"
RESOURCE = f"GET /v1/address-profile?address={TARGET}"
ATTACKER = "0x000000000000000000000000000000000000dead"
USDC = config.A2A_TOKENS["base-sepolia"][1]


def topic(addr):
    return "0x" + addr[2:].lower().rjust(64, "0")


def transfer_log(token, frm, to, value):
    return {"address": token, "topics": [chain_rpc.TRANSFER_TOPIC, topic(frm), topic(to)], "data": "0x%064x" % value}


class FakeChain:
    """Answers like the pinned RPC would, from what the mock gateway settled. Tests overwrite parts of the receipt."""

    def __init__(self, st):
        self.st = st
        self.tamper = None
        self.height = 1000
        self.block = 990
        self.down = False

    def head(self, cid):
        if self.down:
            raise chain_rpc.ChainError("RPC call failed: timeout")
        return self.height

    def receipt(self, cid, tx):
        pay = next((p for p in self.st.payments.values() if p["tx"] == tx), None)
        if pay is None:
            return None
        o = pay["order"]
        r = {"status": "0x1", "blockNumber": hex(self.block), "transactionHash": tx,
             "logs": [transfer_log(o["method"]["contract_address"], pay["payer"], o["pay_to"], pay["raw"])]}
        if self.tamper:
            self.tamper(r, pay)
        return r

    @staticmethod
    def transfers(r):
        return chain_rpc.transfers(r)


class Env:
    def __init__(self, reader=None, confirmations=2):
        self.st = mock_server.MockState()
        with open(os.path.join(HERE, "fixtures", "live_chain_config.json")) as f:
            self.st.chain_config = json.load(f)["config"]
        self.st.hex_chain_id = True
        self.st.status_seq = ["payment-submitted", "payment-included", "payment-safe"]
        self.mock_srv, _, self.mock_url = mock_server.make_server(0, self.st)
        self.seller_acct, self.buyer = Account.create(), Account.create()
        self.chain = FakeChain(self.st)
        self.log = []
        self.db = tempfile.mktemp(dir=TMP, suffix=".sqlite3")
        self.reader = reader or (lambda a: {"address": a, "hskWei": "1"})
        self.confirmations = confirmations
        self.start_seller()
        self.hsp = self.client()
        cfg = self.hsp.call("get_chain_config")
        self.rows, _ = flow.parse_chain_config(cfg)
        self.receipts = flow.Receipts(tempfile.mktemp(dir=TMP, suffix=".jsonl"), "mock")
        self.policy = pa.Policy(seller=self.seller_acct.address)
        self.say = []

    def client(self):
        h = flow.Hsp(mcp_client.McpClient(self.mock_url, allow_loopback=True, total_timeout=10))
        h.discover()
        return h

    def start_seller(self):
        self.seller = ra.Seller(self.seller_acct, self.client, db_path=self.db, chain=self.chain, reader=self.reader,
                                log=self.log.append, confirmations=self.confirmations)
        self.srv = ra.make_server(self.seller)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.url = f"{self.base}/v1/address-profile?address={TARGET}&network=base-sepolia"

    def restart_seller(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.seller.store.db.close()
        self.start_seller()

    def agent(self, **kw):
        over = {"sleep": lambda s: None, "poll_interval": 0}
        return pa.PaymentAgent(self.buyer, self.hsp, self.rows, kw.pop("policy", self.policy), self.receipts,
                               balance_fn=lambda c, t, o: 10 ** 12, say=self.say.append, sleep=lambda s: None,
                               ctx_overrides=over, **kw)

    def quote(self, payer=None, url=None):
        return pa.http_get(url or self.url, {"X-Payer": payer or self.buyer.address})

    def proof(self, pr_id, acct=None, resource=RESOURCE):
        return ra.sign_text(acct or self.buyer, ra.claim_message(pr_id, resource))

    def claim(self, pr_id, acct=None, url=None, resource=RESOURCE):
        return pa.http_get(url or self.url, {"X-HSP-Payment-Request-Id": pr_id, "X-HSP-Proof": self.proof(pr_id, acct, resource)})

    def pay(self, cond):
        """Pay a quote through the real flow (order, firewall, signature, mock gateway) without claiming."""
        token = next(r for r in self.rows if r.network == cond["network"] and r.address.lower() == cond["token"].lower())
        ctx = flow.Ctx(hsp=self.hsp, account=self.buyer, pay_to=cond["pay_to"], receipts=self.receipts,
                       approve=lambda s, d: True, balance_fn=lambda c, t, o: 10 ** 12, sleep=lambda s: None,
                       poll_interval=0, out=lambda m: None)
        return flow.run_payment(ctx, token, Decimal(cond["amount"]["value"]), label=cond["resource"],
                                pr_id=cond["payment_request_id"], expiry=flow._rfc(cond["expires_at"]))

    def paid_quote(self):
        st, body = self.quote()
        assert st == 402
        cond = body["conditions"]
        self.pay(cond)
        return cond

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.seller.store.db.close()
        self.mock_srv.shutdown()
        self.mock_srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.addCleanup(self.e.close)


class TestEndToEnd(Base):
    def test_positive_control_full_a2a_flow_then_replay_refused(self):
        out = self.e.agent().buy(self.e.url, RESOURCE, replay_check=True)
        self.assertEqual(out["data"]["address"], TARGET)
        self.assertEqual(out["payment"]["attribution_verified"], True)
        self.assertEqual(out["payment"]["status"], "payment-safe")
        self.assertEqual(out["replay"], {"status": 409, "error": "already_served"})
        self.assertEqual(len(self.e.st.payments), 1)              # one payment, one answer
        steps = [r["step"] for r in self.e.receipts.rows()]
        for s in ("a2a_conditions", "firewall_ok", "signed", "submit_payment", "a2a_served"):
            self.assertIn(s, steps)
        order = next(iter(self.e.st.orders.values()))
        self.assertEqual(order["payment_request_id"], self.e.receipts.rows()[0]["payment_request_id"])
        self.assertEqual(order["pay_to"], self.e.seller_acct.address)

    def test_order_is_built_from_the_conditions(self):
        self.e.agent().buy(self.e.url, RESOURCE)
        a = next(a for n, a in self.e.st.calls if n == "create_order")
        cart = a["cart_mandate"]["contents"]
        self.assertEqual(cart["payment_request"]["details"]["display_items"][0]["label"], RESOURCE)
        self.assertEqual(cart["payment_request"]["details"]["total"]["amount"], {"currency": "USD", "value": "0.02"})
        self.assertEqual(a["merchant_name"], config.MERCHANT_NAME)

    def test_agent_card(self):
        st, card = pa.http_get(self.e.base + ra.CARD_PATH, {})
        self.assertEqual(st, 200)
        self.assertEqual(card["payment"]["pay_to"], self.e.seller_acct.address)
        self.assertEqual(card["payment"]["merchant_name"], config.MERCHANT_NAME)

    def test_a_tampered_gateway_reply_stops_at_the_firewall(self):
        self.e.st.tamper = lambda p: (p["eip712"]["message"].update(to=ATTACKER),
                                      p["payment_payload"]["payload"]["authorization"].update(to=ATTACKER))
        with self.assertRaises(flow.FlowStop) as cm:
            self.e.agent().buy(self.e.url, RESOURCE)
        self.assertEqual(cm.exception.info.get("rule"), "recipient")
        self.assertEqual([n for n, _ in self.e.st.calls if n == "finalize_payment"], [])


class TestSellerRefusals(Base):
    def assertClaim(self, cond, status, rule):
        st, body = self.e.claim(cond["payment_request_id"])
        self.assertEqual((st, body.get("error")), (status, rule), body)

    def test_unpaid_quote_is_not_served(self):
        st, body = self.e.quote()
        self.assertEqual(st, 402)
        self.assertClaim(body["conditions"], 402, "not_paid_yet")
        self.assertIn("payment_required", json.dumps(body))

    def test_still_in_flight_then_served(self):
        self.e.st.status_seq = ["payment-submitted", "payment-processing", "payment-included", "payment-safe"]
        st, body = self.e.quote()
        cond = body["conditions"]
        self.e.st.status_seq = ["payment-submitted", "payment-safe"]
        self.e.pay(cond)
        self.e.st.status_seq = ["payment-included", "payment-safe"]
        for p in self.e.st.payments.values():
            p["idx"] = 0
        self.assertClaim(cond, 402, "not_paid_yet")
        self.assertClaim(cond, 402, "not_paid_yet")                 # within the retry gap: the gateway is not asked again
        self.e.seller.clock = lambda: time.time() + config.A2A_UNPAID_RETRY_S + 1
        st, body = self.e.claim(cond["payment_request_id"])
        self.assertEqual(st, 200, body)

    def test_gateway_error_is_503_not_paid(self):
        cond = self.e.paid_quote()
        self.e.st.query_errors = 1
        self.assertClaim(cond, 503, "gateway_unavailable")
        self.assertEqual(self.e.claim(cond["payment_request_id"])[0], 200)      # nothing was lost

    def test_replay_refused_even_after_a_restart(self):
        cond = self.e.paid_quote()
        self.assertEqual(self.e.claim(cond["payment_request_id"])[0], 200)
        self.assertClaim(cond, 409, "already_served")
        self.e.restart_seller()
        self.assertClaim(cond, 409, "already_served")

    def test_concurrent_claims_serve_exactly_once(self):
        served = []
        e = self.e
        e.reader = None
        e.seller.reader = lambda a: (served.append(a), time.sleep(0.05), {"address": a})[2]
        cond = e.paid_quote()
        out, proof = [], e.proof(cond["payment_request_id"])
        h = {"X-HSP-Payment-Request-Id": cond["payment_request_id"], "X-HSP-Proof": proof}

        def go():
            out.append(pa.http_get(e.url, h)[0])
        th = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in th]
        [t.join() for t in th]
        self.assertEqual(out.count(200), 1, out)
        self.assertEqual(len(served), 1)

    def test_unknown_or_malformed_id(self):
        for bad in ("HSKM-PR-20261101110957-0000000000000000", "x", "' OR 1=1 --"):
            st, body = pa.http_get(self.e.url, {"X-HSP-Payment-Request-Id": bad, "X-HSP-Proof": "0x" + "1" * 130})
            self.assertEqual((st, body["error"]), (402, "unknown_quote"))

    def test_proof_from_another_wallet_refused(self):
        cond = self.e.paid_quote()
        st, body = self.e.claim(cond["payment_request_id"], acct=Account.create())
        self.assertEqual((st, body["error"]), (403, "bad_proof"))
        st, body = pa.http_get(self.e.url, {"X-HSP-Payment-Request-Id": cond["payment_request_id"]})
        self.assertEqual((st, body["error"]), (403, "bad_proof"))
        self.assertEqual(self.e.claim(cond["payment_request_id"])[0], 200)     # positive control: the payer still can

    def test_quote_cannot_buy_another_resource(self):
        cond = self.e.paid_quote()
        other = "0x" + "ab" * 20
        url = f"{self.e.base}/v1/address-profile?address={other}"
        st, body = self.e.claim(cond["payment_request_id"], url=url, resource=f"GET /v1/address-profile?address={other}")
        self.assertEqual((st, body["error"]), (403, "wrong_resource"))

    def test_client_supplied_tx_is_never_used(self):
        cond = self.e.paid_quote()
        st, body = pa.http_get(self.e.url, {"X-HSP-Payment-Request-Id": cond["payment_request_id"],
                                            "X-HSP-Proof": self.e.proof(cond["payment_request_id"]),
                                            "X-HSP-Tx": "0x" + "ee" * 32, "X-Tx-Hash": "0x" + "ee" * 32})
        self.assertEqual(st, 200)
        self.assertEqual(body["payment"]["tx"], next(iter(self.e.st.payments.values()))["tx"])

    def test_one_transaction_cannot_pay_two_quotes(self):
        self.e.st.force_tx = "0x" + "ab" * 32
        a = self.e.paid_quote()
        b = self.e.paid_quote()
        self.assertEqual(self.e.claim(a["payment_request_id"])[0], 200)
        st, body = self.e.claim(b["payment_request_id"])
        self.assertEqual((st, body["error"]), (409, "tx_already_used"))

    def test_one_transaction_in_another_spelling_cannot_pay_twice(self):
        self.e.st.force_tx = "0x" + "ab" * 32
        a = self.e.paid_quote()
        b = self.e.paid_quote()
        self.assertEqual(self.e.claim(a["payment_request_id"])[0], 200)
        self.e.st.query_tamper = lambda r: r["payment"].update(tx_signature="0x" + "AB" * 32)
        real = self.e.chain.receipt
        self.e.chain.receipt = lambda cid, tx: real(cid, tx.lower())
        st, body = self.e.claim(b["payment_request_id"])
        self.assertEqual((st, body["error"]), (409, "tx_already_used"))

    def test_slow_verification_does_not_leave_the_claim_stale(self):
        """Verification takes longer than the reclaim window. A second claim during the data read must still be refused."""
        e = self.e
        cond = e.paid_quote()
        pr = cond["payment_request_id"]
        offset = [0]
        e.seller.clock = lambda: time.time() + offset[0]
        real_verify = e.seller.verify_paid

        def slow(q):
            out = real_verify(q)
            if not offset[0]:                 # only the first claim's verification is slow
                offset[0] = 130
            return out
        e.seller.verify_paid = slow
        second = []

        def reader(address):
            second.append(e.claim(pr))
            return {"address": address}
        e.seller.reader = reader
        self.assertEqual(e.claim(pr)[0], 200)
        self.assertEqual(second[0][0], 409)

    def test_a_claim_that_lost_its_slot_cannot_finish_or_release(self):
        store = self.e.seller.store
        cond = self.e.paid_quote()
        pr = cond["payment_request_id"]
        store.begin_serving(pr, "0x" + "11" * 32, 1000)
        store.begin_serving(pr, "0x" + "11" * 32, 1200)          # the crash reclaim, two minutes later
        store.release(pr, 1000)
        self.assertEqual(store.get(pr).status, "serving")
        self.assertFalse(store.finish(pr, 1000))
        self.assertTrue(store.finish(pr, 1200))
        self.assertEqual(store.get(pr).status, "served")

    def test_a_claim_that_lost_its_slot_gets_no_data(self):
        e = self.e
        cond = e.paid_quote()
        pr = cond["payment_request_id"]

        def overtaken(address):           # another claim takes the slot while this one reads
            e.seller.store.db.execute("UPDATE quotes SET claim_ts = claim_ts + 1 WHERE id = ?", (pr,))
            return {"address": address}
        e.seller.reader = overtaken
        st, body = e.claim(pr)
        self.assertEqual((st, body.get("error")), (409, "already_served"))

    def test_undelivered_answer_reopens_the_claim(self):
        cond = self.e.paid_quote()
        pr = cond["payment_request_id"]
        real = ra.Handler.send_json
        failed = []

        def broken(handler, code, obj, retry_after=None):
            if code == 200 and not failed:
                failed.append(1)
                raise BrokenPipeError
            return real(handler, code, obj, retry_after)
        ra.Handler.send_json = broken
        try:
            with self.assertRaises(Exception):
                self.e.claim(pr)
            self.assertEqual(self.e.seller.store.get(pr).status, "open")
            st, body = self.e.claim(pr)
            self.assertEqual(st, 200, body)
        finally:
            ra.Handler.send_json = real
        self.assertEqual(self.e.seller.store.get(pr).status, "served")

    def test_unpaid_quote_cannot_drive_gateway_traffic(self):
        st, body = self.e.quote()
        pr = body["conditions"]["payment_request_id"]
        n = lambda: len([c for c, _ in self.e.st.calls if c == "query_payment"])
        before = n()
        for _ in range(3):
            self.assertEqual(self.e.claim(pr)[1]["error"], "not_paid_yet")
        self.assertEqual(n() - before, 1)

    def test_each_claim_closes_its_gateway_session(self):
        cond = self.e.paid_quote()
        before = len(self.e.st.sessions)
        self.assertEqual(self.e.claim(cond["payment_request_id"])[0], 200)
        self.assertEqual(len(self.e.st.sessions), before)

    def test_a_trailing_newline_is_not_an_address(self):
        url = f"{self.e.base}/v1/address-profile?address={TARGET}%0A"
        self.assertEqual(self.e.quote(url=url)[1]["error"], "bad_address")

    def test_expired_quotes_do_not_fill_the_cap(self):
        old = config.A2A_MAX_OPEN_QUOTES
        config.A2A_MAX_OPEN_QUOTES = 1
        try:
            self.assertEqual(self.e.quote()[0], 402)
            self.e.seller.clock = lambda: time.time() + config.A2A_QUOTE_TTL_S + 60     # expired, still in the claim grace
            self.assertEqual(self.e.quote()[0], 402)
        finally:
            config.A2A_MAX_OPEN_QUOTES = old

    def test_expired_claim_window(self):
        cond = self.e.paid_quote()
        self.e.seller.clock = lambda: time.time() + config.A2A_QUOTE_TTL_S + config.A2A_CLAIM_GRACE_S + 5
        self.assertClaim(cond, 410, "quote_expired")

    def test_failed_data_source_keeps_the_claim_open(self):
        calls = []

        def flaky(a):
            calls.append(a)
            if len(calls) == 1:
                raise chain_rpc.ChainError("RPC call failed")
            return {"address": a}
        self.e.seller.reader = flaky
        cond = self.e.paid_quote()
        self.assertClaim(cond, 502, "data_source_failed")
        st, body = self.e.claim(cond["payment_request_id"])
        self.assertEqual(st, 200, body)                  # served on retry, no second payment
        self.assertEqual(len(self.e.st.payments), 1)
        self.assertClaim(cond, 409, "already_served")

    def test_inputs(self):
        u = self.e.base + "/v1/address-profile?address="
        self.assertEqual(self.e.quote(url=u + "0x12")[1]["error"], "bad_address")
        self.assertEqual(self.e.quote(url=self.e.url + "x")[1]["error"], "bad_network")
        self.assertEqual(self.e.quote(payer="nope")[1]["error"], "bad_payer")
        self.assertEqual(pa.http_get(self.e.base + "/secret", {})[0], 404)
        self.assertEqual(pa.http_get(self.e.url + "&p=" + "a" * 400, {})[0], 414)

    def test_rate_limit(self):
        self.e.seller.quote_limit = ra.Limiter(2)
        self.assertEqual([self.e.quote()[0] for _ in range(3)], [402, 402, 429])

    def test_open_quote_cap(self):
        old = config.A2A_MAX_OPEN_QUOTES
        config.A2A_MAX_OPEN_QUOTES = 1
        try:
            self.assertEqual([self.e.quote()[0] for _ in range(2)], [402, 503])
        finally:
            config.A2A_MAX_OPEN_QUOTES = old


class TestSellerMismatches(Base):
    """Each case: a real paid quote where exactly one fact is wrong. The seller must refuse and the quote stays unserved."""

    tampered = None

    def refused(self, rule, http=402):
        cond = self.e.paid_quote()
        self.e.st.query_tamper = self.tampered           # after the buyer's own polling: the seller's query sees it
        st, body = self.e.claim(cond["payment_request_id"])
        self.assertEqual((st, body.get("error")), (http, rule), body)
        self.assertEqual(self.e.seller.store.get(cond["payment_request_id"]).status, "open")

    def gateway(self, mutate):
        self.tampered = mutate

    def test_positive_control(self):
        cond = self.e.paid_quote()
        self.assertEqual(self.e.claim(cond["payment_request_id"])[0], 200)

    def test_attribution_false(self):
        self.gateway(lambda r: r.update(attribution_verified=False))
        self.refused("attribution")

    def test_attribution_missing(self):
        self.gateway(lambda r: r.pop("attribution_verified"))
        self.refused("attribution")

    def test_other_merchant(self):
        self.gateway(lambda r: r.update(merchant_name="Another Team"))
        self.refused("attribution")

    def test_gateway_reports_other_recipient(self):
        self.gateway(lambda r: r["payment"].update(to_pay_address=ATTACKER))
        self.refused("mismatch_to_pay_address")

    def test_gateway_reports_other_token(self):
        self.gateway(lambda r: r["payment"].update(token_address=ATTACKER))
        self.refused("mismatch_token_address")

    def test_gateway_reports_other_chain(self):
        self.gateway(lambda r: r["payment"].update(chain="eip155:1"))
        self.refused("mismatch_chain")

    def test_gateway_reports_other_payer(self):
        self.gateway(lambda r: r["payment"].update(payer_address=ATTACKER))
        self.refused("mismatch_payer_address")

    def test_gateway_reports_other_request(self):
        self.gateway(lambda r: r["payment"].update(payment_request_id="HSKM-PR-other"))
        self.refused("mismatch_payment_request_id")

    def test_gateway_reports_short_amount(self):
        self.gateway(lambda r: r["payment"].update(usd_amount="0.01"))
        self.refused("mismatch_amount")

    def test_gateway_reports_garbage_amount(self):
        self.gateway(lambda r: r["payment"].update(usd_amount="free"))
        self.refused("mismatch_amount")

    def test_gateway_amount_bomb_and_nan(self):
        for bad in ("1E+999990", "NaN", "Infinity", "-1E+99"):
            self.gateway(lambda r, b=bad: r["payment"].update(pay_amount=b))
            started = time.time()
            self.refused("mismatch_amount")
            self.assertLess(time.time() - started, 5)

    def test_gateway_and_chain_contradict_each_other_on_status(self):
        self.gateway(lambda r: r["payment"].update(status="payment-failed"))
        self.refused("payment_not_safe")

    def test_negative_block_number(self):
        self.e.chain.tamper = lambda r, p: r.update(blockNumber="-0x1")
        self.refused("tx_failed")

    def test_payment_failed(self):
        self.gateway(lambda r: r.update(status="payment-failed"))
        self.refused("payment_not_safe")

    def test_unknown_status(self):
        self.gateway(lambda r: r.update(status="payment-maybe"))
        self.refused("payment_not_safe")

    def test_tx_reverted(self):
        self.e.chain.tamper = lambda r, p: r.update(status="0x0")
        self.refused("tx_failed")

    def test_chain_does_not_know_the_tx(self):
        self.e.chain.receipt = lambda cid, tx: None
        self.refused("not_on_chain_yet")

    def test_no_transfer_in_the_tx(self):
        self.e.chain.tamper = lambda r, p: r.update(logs=[])
        self.refused("no_matching_transfer")

    def test_transfer_to_someone_else(self):
        self.e.chain.tamper = lambda r, p: r.update(logs=[transfer_log(USDC, p["payer"], ATTACKER, p["raw"])])
        self.refused("no_matching_transfer")

    def test_transfer_of_another_token(self):
        self.e.chain.tamper = lambda r, p: r.update(logs=[transfer_log(ATTACKER, p["payer"], p["order"]["pay_to"], p["raw"])])
        self.refused("no_matching_transfer")

    def test_transfer_from_someone_else(self):
        self.e.chain.tamper = lambda r, p: r.update(logs=[transfer_log(USDC, ATTACKER, p["order"]["pay_to"], p["raw"])])
        self.refused("no_matching_transfer")

    def test_two_matching_transfers_are_ambiguous(self):
        def twice(r, p):
            r["logs"] = r["logs"] * 2
        self.e.chain.tamper = twice
        self.refused("no_matching_transfer")

    def test_short_value_on_chain(self):
        self.e.chain.tamper = lambda r, p: r.update(logs=[transfer_log(USDC, p["payer"], p["order"]["pay_to"], 19000)])
        self.refused("wrong_value")

    def test_gateway_and_chain_agree_on_a_short_value(self):
        self.gateway(lambda r: r["payment"].update(pay_amount="0.015"))
        self.e.chain.tamper = lambda r, p: r.update(logs=[transfer_log(USDC, p["payer"], p["order"]["pay_to"], 15000)])
        self.refused("wrong_value")

    def test_value_differs_from_the_gateway(self):
        self.e.chain.tamper = lambda r, p: r.update(logs=[transfer_log(USDC, p["payer"], p["order"]["pay_to"], p["raw"] + 1)])
        self.refused("wrong_value")

    def test_receipt_of_another_tx(self):
        self.e.chain.tamper = lambda r, p: r.update(transactionHash="0x" + "cd" * 32)
        self.refused("tx_failed")

    def test_too_few_confirmations(self):
        self.e.chain.block = self.e.chain.height
        self.refused("not_enough_confirmations")

    def test_rpc_down(self):
        self.e.chain.down = True
        self.refused("chain_unavailable", 503)


class TestBuyerPolicy(Base):
    """Conditions that fail a rule: refused before any order reaches the MCP server."""

    def body(self):
        return self.e.quote()[1]

    def signed(self, mutate, acct=None):
        b = self.body()
        mutate(b["conditions"])
        b["signature"] = ra.sign_text(acct or self.e.seller_acct, ra.canonical(b["conditions"]))
        return b

    def refused(self, body, rule, policy=None, spent=Decimal(0)):
        with self.assertRaises(pa.Refused) as cm:
            pa.check_conditions(body, policy or self.e.policy, RESOURCE, self.e.buyer.address, self.e.rows, time.time(), spent)
        self.assertEqual(cm.exception.rule, rule, str(cm.exception))

    def test_positive_control(self):
        c, tok, usd = pa.check_conditions(self.body(), self.e.policy, RESOURCE, self.e.buyer.address, self.e.rows, time.time())
        self.assertEqual((tok.chain_id, usd), (84532, Decimal("0.02")))

    def test_unsigned_changed_pay_to(self):
        b = self.body()
        b["conditions"]["pay_to"] = ATTACKER
        self.refused(b, "seller_signature")

    def test_changed_price_breaks_the_signature(self):
        b = self.body()
        b["conditions"]["amount"]["value"] = "0.01"
        self.refused(b, "seller_signature")

    def test_signed_by_someone_else(self):
        self.refused(self.signed(lambda c: c.update(pay_to=ATTACKER), acct=Account.create()), "seller_signature")

    def test_seller_redirects_the_money(self):
        self.refused(self.signed(lambda c: c.update(pay_to=ATTACKER)), "pay_to")

    def test_wrong_merchant(self):
        self.refused(self.signed(lambda c: c.update(merchant_name="Other")), "merchant")

    def test_other_resource(self):
        self.refused(self.signed(lambda c: c.update(resource="GET /v1/other")), "resource")

    def test_other_payer(self):
        self.refused(self.signed(lambda c: c.update(payer=ATTACKER)), "payer")

    def test_other_gateway(self):
        self.refused(self.signed(lambda c: c.update(gateway="https://evil.invalid/")), "gateway")

    def test_over_price_limit(self):
        self.refused(self.signed(lambda c: c["amount"].update(value="0.06")), "price_limit")

    def test_sub_cent(self):
        self.refused(self.signed(lambda c: c["amount"].update(value="0.001")), "amount")

    def test_not_usd(self):
        self.refused(self.signed(lambda c: c["amount"].update(currency="EUR")), "amount")

    def test_nan_price(self):
        self.refused(self.signed(lambda c: c["amount"].update(value="NaN")), "amount")

    def test_session_cap(self):
        self.refused(self.body(), "session_cap", spent=Decimal("0.24"))

    def test_network_outside_policy(self):
        p = pa.Policy(seller=self.e.seller_acct.address, networks=("sepolia",))
        self.refused(self.body(), "network", policy=p)

    def test_token_not_in_chain_config(self):
        self.refused(self.signed(lambda c: c.update(token=ATTACKER)), "token")

    def test_chain_differs_from_chain_config(self):
        self.refused(self.signed(lambda c: c.update(chain_id=1)), "token")

    def test_expiring_conditions(self):
        self.refused(self.signed(lambda c: c.update(expires_at=flow.utc_iso(time.time() + 10))), "expiry")

    def test_extra_field(self):
        self.refused(self.signed(lambda c: c.update(note="please pay")), "malformed")

    def test_garbage(self):
        for b in (None, [], {}, {"conditions": "x"}):
            self.refused(b, "malformed")

    def test_refused_conditions_create_no_order(self):
        self.e.policy = pa.Policy(seller=ATTACKER)     # we pinned someone else: the real seller's conditions fail
        with self.assertRaises(pa.Refused) as cm:
            self.e.agent().buy(self.e.url, RESOURCE)
        self.assertEqual(cm.exception.rule, "seller_signature")
        self.assertEqual([n for n, _ in self.e.st.calls if n == "create_order"], [])

    def test_spending_stops_at_the_session_cap(self):
        self.e.policy = pa.Policy(seller=self.e.seller_acct.address, session_cap_usd=Decimal("0.03"))
        agent = self.e.agent()
        agent.buy(self.e.url, RESOURCE)
        with self.assertRaises(pa.Refused) as cm:
            agent.buy(self.e.url, RESOURCE)
        self.assertEqual(cm.exception.rule, "session_cap")
        self.assertEqual(len(self.e.st.payments), 1)


class TestBuyerCaps(Base):
    """Caps that hold however the gateway behaves."""

    def test_a_fee_the_seller_never_asked_for_is_refused_before_signing(self):
        self.e.st.gas_fee_advanced = False
        self.e.st.gas_fee_usd = Decimal("0.10")
        with self.assertRaises(flow.FlowStop) as cm:
            self.e.agent().buy(self.e.url, RESOURCE)
        self.assertEqual(cm.exception.reason, "firewall_refused")
        self.assertEqual([n for n, _ in self.e.st.calls if n == "finalize_payment"], [])

    def test_every_signature_counts_against_the_session_cap(self):
        self.e.st.finalize_30043 = 2                   # the gateway makes us sign three times for one purchase
        self.e.policy = pa.Policy(seller=self.e.seller_acct.address, session_cap_usd=Decimal("0.07"))
        agent = self.e.agent()
        agent.buy(self.e.url, RESOURCE)
        self.assertEqual(agent.spent, Decimal("0.06"))
        self.assertEqual(self.e.receipts.committed_usd(), Decimal("0.06"))
        with self.assertRaises(pa.Refused) as cm:
            agent.buy(self.e.url, RESOURCE)
        self.assertEqual(cm.exception.rule, "session_cap")

    def test_signatures_on_a_failed_purchase_still_count(self):
        self.e.st.finalize_30043 = 99
        agent = self.e.agent()
        with self.assertRaises(flow.FlowStop):
            agent.buy(self.e.url, RESOURCE)
        self.assertEqual(agent.spent, Decimal("0.06"))

    def test_a_malformed_200_is_a_failed_claim_not_a_crash(self):
        real = pa.http_get

        def fake(url, headers, timeout=20):
            st, body = real(url, headers, timeout)
            return (200, {"nonsense": True}) if st == 200 else (st, body)
        with self.assertRaises(pa.Refused) as cm:
            self.e.agent(get=fake).buy(self.e.url, RESOURCE)
        self.assertEqual(cm.exception.rule, "claim_failed")


class TestAddressProfile(unittest.TestCase):
    def test_a_failed_read_is_an_error_not_wrong_data(self):
        real = chain_rpc._post
        chain_rpc._post = lambda url, method, params, timeout: None if method == "eth_getCode" else "0x1"
        try:
            with self.assertRaises(chain_rpc.ChainError):
                ra.address_profile(TARGET)
        finally:
            chain_rpc._post = real

    def test_good_read(self):
        real = chain_rpc._post
        chain_rpc._post = lambda url, method, params, timeout: "0x60" if method == "eth_getCode" else "0x10"
        try:
            self.assertEqual(ra.address_profile(TARGET)["isContract"], True)
        finally:
            chain_rpc._post = real


class TestChainParsing(unittest.TestCase):
    """chain.transfers on the real receipt of the first HSP payment."""

    def test_real_receipt(self):
        with open(os.path.join(HERE, "fixtures", "live_receipt_base_sepolia.json")) as f:
            r = json.load(f)
        got = chain_rpc.transfers(r)
        self.assertIn({"token": USDC.lower(), "from": "0x97ec2b82563bad972fdb279e8709cb84edfa86ba",
                       "to": "0x58232cf98671bc0bd0e7aaa7cd6ade9edb2e64a6", "value": 10003}, got)
        self.assertEqual(r["status"], "0x1")

    def test_malformed_logs_are_skipped(self):
        good = transfer_log(USDC, ATTACKER, ATTACKER, 5)
        bad = [dict(good, data="0x12"), dict(good, address="nope"), dict(good, topics=good["topics"][:2]),
               dict(good, topics=["0xdead"] + good["topics"][1:]), "x", None]
        self.assertEqual(len(chain_rpc.transfers({"logs": bad + [good]})), 1)

    def test_bad_tx_hash_never_reaches_the_network(self):
        with self.assertRaises(chain_rpc.ChainError):
            chain_rpc.receipt(84532, "0x1234")


if __name__ == "__main__":
    unittest.main(verbosity=1)
