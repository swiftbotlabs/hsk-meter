# HSP Agentic Payment and A2A over HSP

Everything here runs through **HSP Agentic Payment**, HashKey's official server for hackathon teams
(`https://mcp-qa.hashkeymerchant.com/`, MCP over streamable HTTP, QA environment, testnets, allowlisted by IP): real
transactions broadcast by HSP's Gateway, attributed to the team name `Swift Bot Labs (HSK Meter)`.
Receipts: [docs/hsp_receipts.json](../docs/hsp_receipts.json), [live table](https://apps.swiftbotlab.workers.dev/hsk-meter/#hsp-h).

## The A2A flow

1. **Receiving Agent** (`receiving_agent.py`) answers a request with `402` and its payment conditions, signed by the key
   of the address that gets paid: amount in USD, `pay_to`, `merchant_name`, resource, network, token, `payment_request_id`.
2. **Payment Agent** (`payment_agent.py`) checks them against the seller it pinned, its spend limits and
   `get_chain_config`. It refuses here, before any order exists, if anything differs. Every signature counts against the
   limits: 0.05 USD per purchase by default, a session cap per run, 5 USD in total across runs.
3. It builds the Cart Mandate from the conditions, keeping the seller's `payment_request_id`. `create_order`, `get_order`
   and `prepare_payment` return the data to sign. The **signing firewall** (`firewall.py`) checks that data against the
   order; the wallet signs only then. `finalize_payment` and `submit_payment` hand the Payment Mandate to HSP's Gateway,
   which broadcasts the transfer.
4. Once `query_payment` says `payment-safe`, the Payment Agent repeats its request with `X-HSP-Payment-Request-Id` and a
   wallet proof. The Receiving Agent checks the payment itself, with HSP and on a pinned RPC, and serves the data once.
   The caller never supplies a transaction hash. The same claim again gets `409 already_served`.

## What the firewall is for

HSP's server holds the merchant keys and hands the payer the EIP-712 data to sign. The guide has a person look at the
recipient, payer and amount first. `firewall.py` does that check in code, against our own order and the token from
`get_chain_config`, and refuses on any difference: primary type, chain id, token contract, payer, recipient (or Permit2
spender), amount, validity window (measured from our own clock), the x402 payload, or the base64 copy of it inside the
Payment Mandate.
`python3 hsp_qa/firewall_demo.py` shows it on a real reply from HSP's server.

## Run

```bash
python3 -m pip install -r requirements.txt -c constraints.txt   # eth-account, from the repository root
python3 hsp_qa/test_hsp_qa.py                                    # offline: client, firewall, flow, real-reply fixtures
python3 hsp_qa/test_a2a.py                                       # offline: both agents over HTTP against the local mock
python3 hsp_qa/firewall_demo.py                                  # offline: tampered copies of a real HSP reply
python3 hsp_qa/pay.py pay --mock --yes                           # the payment flow against the mock

# live, from an allowlisted IP
python3 hsp_qa/pay.py wallets --create                           # QA payer and pay_to keys, testnet only, mode 600
python3 hsp_qa/pay.py pay --network base-sepolia --coin USDC --dry   # stops before signing
python3 hsp_qa/receiving_agent.py --port 8411
python3 hsp_qa/payment_agent.py --seller <pay_to> --address 0x78C8476772BC209C3df09Da5e129d0879cd57F8c \
    --network base-sepolia --replay-check
python3 hsp_qa/publish_receipts.py                               # re-checks every payment, writes docs/hsp_receipts.json
```

The payer needs test USDC of the exact contract `get_chain_config` lists; for Base Sepolia, Ethereum Sepolia and Arbitrum
Sepolia that is Circle's public faucet. No native gas is needed: the payer signs and HSP's Gateway broadcasts. Keys live
in `$HSP_QA_SECRETS_DIR` (default `~/.hsk-meter/hsp_qa`). `fixtures/` holds real replies from HSP's QA server (no secrets).
What the live server does differently from the guide is listed in [HSP_STATUS.md](../HSP_STATUS.md).
