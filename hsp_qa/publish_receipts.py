#!/usr/bin/env python3
"""Build docs/hsp_receipts.json from the live payment log, re-checking every line first.

For each payment that reached submit_payment: ask HSP again (query_payment, expected_merchant_name = our team name) and
read the transaction from a pinned RPC. A payment goes into the public file only if HSP says payment-safe or
payment-finalized with attribution_verified true AND the chain shows a successful transaction with the matching token
Transfer from the payer to pay_to. Anything else is printed and left out.

  publish_receipts.py [--out ../docs/hsp_receipts.json]
"""
import argparse
import json
import os
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import chain  # noqa: E402
import config  # noqa: E402
import flow  # noqa: E402
from mcp_client import McpClient, McpError  # noqa: E402

EXPLORERS = {84532: "https://base-sepolia.blockscout.com/tx/", 11155111: "https://eth-sepolia.blockscout.com/tx/",
             421614: "https://arbitrum-sepolia.blockscout.com/tx/", 133: "https://testnet-explorer.hsk.xyz/tx/"}


def load_runs(path):
    with open(path) as f:
        rows = [json.loads(x) for x in f if x.strip()]
    starts = {r["run_id"]: r for r in rows if r.get("step") == "start"}
    a2a = {r["payment_request_id"] for r in rows if r.get("step") == "a2a_served"}
    logged = {r["flow_id"]: r for r in rows if r.get("step") == "query_payment"}      # last status we saw for each flow
    runs = []
    for r in rows:
        if r.get("step") in ("submit_payment", "submit_uncertain", "submit_error"):
            s = starts.get(r.get("run_id"))
            if s:
                runs.append({**s, "submit_ts": r["ts"], "flow_id": r["flow_id"], "payment_request_id": r["payment_request_id"],
                             "kind": "a2a" if r["payment_request_id"] in a2a else "direct",
                             "logged": logged.get(r["flow_id"])})
    return runs


def hsp_status(hsp, run):
    """(status, tx, attribution, source). If HSP's query errors now, use what it told us at the time and say so."""
    try:
        q = flow.query_once(type("C", (), {"hsp": hsp}), {"flow_id": run["flow_id"],
                                                          "payment_request_id": run["payment_request_id"]})
    except flow.HspError as e:
        seen = run.get("logged")
        if not seen or not seen.get("tx_signature"):
            raise
        return seen.get("status"), seen["tx_signature"], seen.get("attribution_verified") is True, \
            f"query_payment at {seen['ts']} (re-query now answers {e.code})"
    status, pay = flow._status_of(q)
    ok = q.get("attribution_verified") is True and q.get("merchant_name") == config.MERCHANT_NAME
    return status, pay.get("tx_signature"), ok, f"query_payment at {flow.utc_iso()}"


def verify(hsp, run):
    status, tx, attributed, source = hsp_status(hsp, run)
    if status not in config.PAID_STATUSES:
        return None, f"HSP status {status}"
    if not attributed:
        return None, "attribution not verified"
    rcpt = chain.receipt(run["chain_id"], tx)
    if rcpt is None or rcpt.get("status") != "0x1":
        return None, f"transaction {tx} not successful on-chain"
    hits = [t for t in chain.transfers(rcpt) if t["token"] == run["token"].lower() and t["to"] == run["pay_to"].lower()
            and t["from"] == run["payer"].lower()]
    if len(hits) != 1:
        return None, f"{len(hits)} matching Transfers in {tx}"
    return {"time": run["submit_ts"], "kind": run["kind"], "network": run["network"], "chain_id": run["chain_id"],
            "flow_id": run["flow_id"], "payment_request_id": run["payment_request_id"], "status": status,
            "status_source": source, "attribution_verified": True, "amount_usd": run["amount_usd"], "value_raw": str(hits[0]["value"]),
            "token": run["token"], "coin": run["coin"], "payer": run["payer"], "pay_to": run["pay_to"], "tx": tx,
            "block": int(rcpt["blockNumber"], 16), "relayer": rcpt.get("from"),
            "tx_url": EXPLORERS[run["chain_id"]] + tx}, None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(HERE, "..", "docs", "hsp_receipts.json"))
    args = ap.parse_args()
    runs = load_runs(config.RECEIPTS_LIVE)
    hsp = flow.Hsp(McpClient(config.MCP_URL))
    hsp.discover()
    good, left_out = [], 0
    for run in runs:
        try:
            row, why = verify(hsp, run)
        except (flow.HspError, McpError, chain.ChainError) as e:
            row, why = None, f"{type(e).__name__}: {e}"
        if row is None:
            left_out += 1
            print(f"left out {run['payment_request_id']}: {why}")
            continue
        good.append(row)
        time.sleep(0.2)
    nets = sorted({r["network"] for r in good})
    out = {"generated": flow.utc_iso(), "environment": "HSP QA server (https://mcp-qa.hashkeymerchant.com/), testnets",
           "merchant_name": config.MERCHANT_NAME, "count": len(good),
           "attributed": sum(r["attribution_verified"] is True for r in good),
           "a2a": sum(r["kind"] == "a2a" for r in good), "networks": nets,
           "total_usd": str(sum((Decimal(r["amount_usd"]) for r in good), Decimal(0))),
           "note": "Test tokens on public testnets. HSP QA is HashKey's test environment. The HSK Meter contract settles on "
                   "HashKey Chain mainnet separately; these payments did not go through it. status_source says where each "
                   "status comes from: HSP's query stops answering 15 to 40 minutes after a payment (error 10003 'payment "
                   "timeout' once the order's cart_expiry has passed, 30001 'payment invalid' before that on longer carts).",
           "receipts": good}
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(f"{len(good)} receipts ({out['a2a']} agent to agent) on {nets}, {left_out} left out -> {os.path.normpath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
