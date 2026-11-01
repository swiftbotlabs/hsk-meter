#!/usr/bin/env python3
"""Signing firewall on real data, offline: one prepare_payment reply captured from HSP's QA server, then copies of it
with one fact changed. The untouched copy passes; every tampered copy is refused before anything is signed.

  python3 hsp_qa/firewall_demo.py
"""
import base64
import copy
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import firewall  # noqa: E402
import flow  # noqa: E402

ATTACKER = "0x000000000000000000000000000000000000dEaD"


def both(p, **fields):
    """Change the signed message and the x402 copy together, so only the firewall's own expectation can catch it."""
    p["eip712"]["message"].update(fields)
    p["payment_payload"]["payload"]["authorization"].update(fields)


def recipient(p):
    both(p, to=ATTACKER)


def value(p):
    both(p, value="50000")
    p["token_total_raw"] = "50000"
    p["payment_payload"]["accepted"]["amount"] = "50000"


def chain(p):
    p["eip712"]["domain"]["chainId"] = "0x1"


def token(p):
    p["eip712"]["domain"]["verifyingContract"] = ATTACKER


def mandate(p):
    d = p["payment_mandate_contents"]["payment_response"]["details"]
    inner = json.loads(base64.b64decode(d["payment_payload"]))
    inner["payload"]["authorization"]["to"] = ATTACKER
    d["payment_payload"] = base64.b64encode(json.dumps(inner).encode()).decode()


def expiry(p):
    both(p, validBefore=str(int(p["eip712"]["message"]["validBefore"]) + 86400))


CASES = (("recipient swapped", recipient), ("amount raised to 0.05 on a 0.01 order", value), ("chain changed to Ethereum mainnet", chain),
         ("token contract swapped", token), ("base64 payload inside the mandate swapped", mandate),
         ("authorization valid for one more day", expiry))


def main():
    with open(os.path.join(HERE, "fixtures", "live_chain_config.json")) as f:
        rows, _ = flow.parse_chain_config(json.load(f)["config"])
    with open(os.path.join(HERE, "fixtures", "live_prepare_payment.json")) as f:
        prep = json.load(f)
    tok = flow.select_token(rows, "base-sepolia", "USDC")
    exp = firewall.Expectation(payer=prep["payer"], pay_to=prep["pay_to"], token=tok, order_usd=Decimal("0.01"),
                               gas_fee_advanced=True, payment_request_id=prep["payment_request_id"])
    qexp = flow._rfc(prep["quote_expires_at"])
    now = qexp - 60
    print(f"real prepare_payment reply from HSP QA, {prep['payment_request_id']}, {prep['token_total_raw']} raw USDC on Base Sepolia\n")
    v = firewall.check(prep, exp, now=now, quote_expires_at=qexp)
    print(f"  {'unchanged':<44} PASS, would sign {v.summary['token_total_raw']} raw to {v.summary['recipient'][:10]}...")
    refused = 0
    for label, mutate in CASES:
        p = copy.deepcopy(prep)
        mutate(p)
        try:
            firewall.check(p, exp, now=now, quote_expires_at=qexp)
            print(f"  {label:<44} PASSED (this must not happen)")
        except firewall.Refused as e:
            refused += 1
            print(f"  {label:<44} REFUSED [{e.rule}] nothing signed")
    print(f"\n{refused} of {len(CASES)} tampered copies refused before signing")
    return 0 if refused == len(CASES) else 1


if __name__ == "__main__":
    sys.exit(main())
