/* Try HSK Meter without a wallet. Every call is a free GET to the public endpoint; nothing is signed or paid.
   Server text is only ever written with textContent. */
'use strict';
(function () {
  const API = 'https://hsk-meter.swiftbotlab.workers.dev';
  const CONTRACT = '0x78c8476772bc209c3df09da5e129d0879cd57f8c';
  const OTHER_ADDRESS = '0xa8a49a6ca872d54a8fd5fb121139de9bff62a48d';   // the service payee: any other valid address works
  const PATH = '/v1/hsk/address-profile';
  const SAMPLES = {
    paid: { id: '0x0c70d8276b168b04888595a5a3e2cf4d0dedcd148ab0946f5bdfdca7f388a282' },
    refunded: { id: '0x543e14fbf0aa9e15932405e70df912bca1d0b1da40be8b33ad1d4b88e557f9b5' },
    lie: { id: '0x0c70d8276b168b04888595a5a3e2cf4d0dedcd148ab0946f5bdfdca7f388a282', amount: '0.01' }
  };
  const B32 = /^0x[0-9a-fA-F]{64}$/;
  const ADDR = /^0x[0-9a-fA-F]{40}$/;
  const AMOUNT = /^[0-9]{1,8}(\.[0-9]{1,6})?$/;
  const TX_LINK = /^https:\/\/hsk\.blockscout\.com\/tx\/0x[0-9a-f]{64}$/;
  const $ = (id) => document.getElementById(id);

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = String(text);
    return e;
  }
  function usdc(base) {
    if (!/^[0-9]{1,30}$/.test(String(base))) return String(base);
    const s = String(base).padStart(7, '0');
    const whole = s.slice(0, -6).replace(/^0+(?=\d)/, '');
    const frac = s.slice(-6).replace(/0+$/, '');
    return whole + (frac ? '.' + frac : '') + ' USDC.e';
  }
  function toBase(v) {
    if (!AMOUNT.test(v)) return null;
    const [w, f = ''] = v.split('.');
    const b = BigInt(w) * 1000000n + BigInt((f + '000000').slice(0, 6));
    return b > 0n ? b.toString() : null;
  }
  function randomAddress() {
    const b = new Uint8Array(20);
    do { crypto.getRandomValues(b); } while (b.every((x) => x === 0));
    return '0x' + Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  }
  async function get(url, headers) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 15000);
    try {
      const r = await fetch(url, { method: 'GET', headers: headers || {}, signal: ac.signal, cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer' });
      let body = null;
      try { body = await r.json(); } catch (e) { body = null; }
      return { status: r.status, body, retryAfter: r.headers.get('Retry-After') };
    } catch (e) {
      return { status: 0, body: null, error: e && e.name === 'AbortError' ? 'timed out after 15 s' : 'network error' };
    } finally { clearTimeout(t); }
  }
  function show(box, nodes) {
    box.replaceChildren(...nodes);
    box.hidden = false;
  }
  function failNode(res) {
    if (res.status === 0) return el('p', 'try-err', 'Could not reach the endpoint: ' + res.error + '.');
    if (res.status === 429) return el('p', 'try-err', 'Too many tries from your network. Wait ' + (res.retryAfter || '30') + ' s and try again.');
    const code = res.body && res.body.error ? res.body.error : 'no JSON body';
    return el('p', 'try-err', 'HTTP ' + res.status + ': ' + code + (res.body && res.body.message ? ' (' + res.body.message + ')' : ''));
  }

  /* ---------------- 1. receipt verifier */
  function verdictNode(v) {
    const ok = v === 'ACCEPT';
    const p = el('p', 'try-verdict ' + (ok ? 'is-accept' : 'is-reject'));
    p.append(el('span', 'try-verdict-word', ok ? 'ACCEPT' : 'REJECT'));
    return p;
  }
  function checksNode(checks) {
    const ul = el('ul', 'try-checks');
    for (const c of (Array.isArray(checks) ? checks : []).slice(0, 20)) {
      const li = el('li', c.ok ? 'ok' : 'bad');
      li.append(el('span', 'try-mark', c.ok ? '✓' : '✗'), el('span', 'try-check-name', c.check), el('span', 'try-check-detail', c.detail));
      ul.append(li);
    }
    return ul;
  }
  function paymentNode(p) {
    const box = el('div', 'try-payment');
    const head = el('p', 'try-payment-head');
    head.append(el('span', 'try-tag ' + (p.verdict === 'ACCEPT' ? 'is-accept' : 'is-reject'), p.verdict), el('span', 'mono long', ' requestId ' + p.requestId));
    box.append(head);
    if (p.payment) {
      box.append(el('p', 'try-payment-sub', usdc(p.payment.amount) + ' for endpoint ' + p.payment.endpointId + ', paid by ' + p.payment.payer +
        ' at ' + p.payment.paidAtIso + (p.confirmations !== undefined ? ' (' + p.confirmations + ' confirmations)' : '')));
    }
    box.append(checksNode(p.checks));
    if (p.servedByThisEndpoint && p.servedByThisEndpoint.state) box.append(el('p', 'try-payment-sub', 'Served by this endpoint: ' + p.servedByThisEndpoint.state));
    return box;
  }
  function receiptNodes(b) {
    const out = [verdictNode(b.verdict)];
    const rs = el('ul', 'try-reasons');
    for (const r of (Array.isArray(b.reasons) ? b.reasons : []).slice(0, 6)) rs.append(el('li', '', r));
    out.push(rs);
    if (b.kind === 'requestId') out.push(paymentNode(b));
    for (const p of (Array.isArray(b.payments) ? b.payments : []).slice(0, 2)) out.push(paymentNode(p));
    const link = b.explorer || (b.payment && b.payment.explorer);
    if (typeof link === 'string' && TX_LINK.test(link)) {
      const a = el('a', '', 'Open the transaction on the explorer');
      a.href = link; a.target = '_blank'; a.rel = 'noopener';
      const p = el('p', 'try-link'); p.append(a); out.push(p);
    }
    return out;
  }
  async function checkReceipt() {
    const box = $('r-out');
    const id = $('r-id').value.trim();
    if (!B32.test(id)) return show(box, [el('p', 'try-err', 'Enter 0x followed by 64 hex characters (a transaction hash or a requestId).')]);
    const q = new URLSearchParams();
    const payer = $('r-payer').value.trim(), amount = $('r-amount').value.trim(), ep = $('r-endpoint').value.trim();
    if (payer) { if (!ADDR.test(payer)) return show(box, [el('p', 'try-err', 'Paid by: 0x followed by 40 hex characters.')]); q.set('payer', payer.toLowerCase()); }
    if (amount) { const b = toBase(amount); if (!b) return show(box, [el('p', 'try-err', 'Amount: a positive number with up to 6 decimals.')]); q.set('amount', b); }
    if (ep) { if (!/^[1-9][0-9]{0,5}$/.test(ep)) return show(box, [el('p', 'try-err', 'Endpoint: a whole number such as 1.')]); q.set('endpoint', ep); }
    show(box, [el('p', 'try-wait', 'Reading HashKey Chain…')]);
    const res = await get(API + '/v1/receipt/' + id.toLowerCase() + (q.toString() ? '?' + q : ''));
    if (res.status !== 200 || !res.body || !res.body.verdict) return show(box, [failNode(res)]);
    show(box, receiptNodes(res.body));
  }
  $('r-form').addEventListener('submit', (e) => { e.preventDefault(); checkReceipt(); });
  for (const b of document.querySelectorAll('[data-sample]')) {
    b.addEventListener('click', () => {
      const s = SAMPLES[b.dataset.sample];
      $('r-id').value = s.id;
      $('r-payer').value = ''; $('r-endpoint').value = '';
      $('r-amount').value = s.amount || '';
      if (s.amount) document.querySelector('.try-claim').open = true;
      checkReceipt();
    });
  }

  /* ---------------- 2. tamper with a quote */
  let quote = null;
  const attackButtons = () => document.querySelectorAll('[data-attack]');
  async function getQuote() {
    const box = $('q-quote');
    const payer = randomAddress();
    show(box, [el('p', 'try-wait', 'Asking for a price…')]);
    const res = await get(API + PATH + '?address=' + CONTRACT, { 'X-Payer': payer });
    const a = res.body && Array.isArray(res.body.accepts) ? res.body.accepts[0] : null;
    const x = a && a.extra;
    if (res.status !== 402 || !x || !B32.test(String(x.token)) || !B32.test(String(x.requestId))) { quote = null; return show(box, [failNode(res)]); }
    quote = { payer, token: String(x.token).toLowerCase(), requestId: String(x.requestId).toLowerCase(), amount: String(a.amount) };
    const dl = el('dl', 'facts try-facts');
    const row = (k, v, mono) => { dl.append(el('dt', '', k), el('dd', mono ? 'mono long' : '', v)); };
    row('HTTP status', '402 Payment Required');
    row('Price', usdc(quote.amount) + ' on HashKey Chain (chain id 177)');
    row('Your throwaway payer', payer, true);
    row('Quote token', quote.token, true);
    row('requestId', quote.requestId, true);
    row('requestId starts with the payer', quote.requestId.slice(0, 42) === payer ? 'yes' : 'no', false);
    show(box, [dl]);
    for (const b of attackButtons()) b.disabled = false;
    $('q-out').hidden = true;
  }
  function flipLast(h) {
    const c = h[h.length - 1];
    return h.slice(0, -1) + (c === '0' ? '1' : '0');
  }
  const EXPLAIN = {
    unknown_token: 'Refused. The server does not recognise this quote: the token is a keyed MAC over endpoint, payer, request and price, so the change broke it.',
    payment_not_found: 'Refused. The quote is genuine, but getPayment(requestId) on the settlement contract shows no payment, so nothing is served.',
    bad_payment_headers: 'Refused. The payment headers are malformed.'
  };
  async function attack(kind) {
    const box = $('q-out');
    if (!quote) return;
    let url = API + PATH + '?address=' + CONTRACT;
    const h = { 'X-Payer': quote.payer, 'X-Paywall-Token': quote.token };
    let what;
    if (kind === 'flip') { h['X-Paywall-Token'] = flipLast(quote.token); what = 'Sent the quote with its last character changed.'; }
    else if (kind === 'payer') { h['X-Payer'] = randomAddress(); what = 'Sent the quote from a different payer address.'; }
    else if (kind === 'resource') { url = API + PATH + '?address=' + OTHER_ADDRESS; what = 'Sent the quote for a different address than the one it priced.'; }
    else { what = 'Sent the genuine quote, without paying first.'; }
    show(box, [el('p', 'try-wait', what + ' Waiting for the answer…')]);
    const res = await get(url, h);
    const code = res.body && res.body.error;
    const nodes = [el('p', 'try-sent', what)];
    if (res.status === 200) {
      nodes.push(verdictNode('ACCEPT'), el('p', 'try-err', 'The endpoint served data. That should not happen: please open an issue on GitHub.'));
    } else if (res.status === 402 && EXPLAIN[code]) {
      const p = el('p', 'try-verdict is-reject');
      p.append(el('span', 'try-verdict-word', 'REFUSED'), el('span', 'try-verdict-code mono', ' HTTP 402 · ' + code));
      nodes.push(p, el('p', 'try-explain', EXPLAIN[code]));
    } else nodes.push(failNode(res));
    show(box, nodes);
  }
  $('q-get').addEventListener('click', getQuote);
  for (const b of attackButtons()) b.addEventListener('click', () => attack(b.dataset.attack));

  /* Section 3: a real HSP payment, read straight from a public testnet node. Everything from the node or the receipts
     file is validated and written with textContent. */
  const NODES = {
    'base-sepolia': { url: 'https://base-sepolia-rpc.publicnode.com', chainId: '0x14a34', name: 'Base Sepolia' },
    sepolia: { url: 'https://ethereum-sepolia-rpc.publicnode.com', chainId: '0xaa36a7', name: 'Ethereum Sepolia' },
    'arbitrum-sepolia': { url: 'https://arbitrum-sepolia-rpc.publicnode.com', chainId: '0x66eee', name: 'Arbitrum Sepolia' }
  };
  const TRANSFER = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef';
  const WRONG_TO = '0x000000000000000000000000000000000000dead';
  const HSP_RELAYER = '0x85afd346143690afbec392524a8bae1e9ea8f500';   // the sender listed as permit2_spender in HSP's get_chain_config
  let hspRows = [];

  async function rpc(node, method, params) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), 15000);
    try {
      const r = await fetch(node.url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: ac.signal, credentials: 'omit',
        referrerPolicy: 'no-referrer', body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }) });
      const body = await r.json();
      return body && body.result;
    } finally { clearTimeout(t); }
  }
  function transfersOf(receipt) {
    const out = [];
    for (const l of (Array.isArray(receipt.logs) ? receipt.logs : []).slice(0, 200)) {
      if (!l || !Array.isArray(l.topics) || l.topics.length !== 3 || l.topics[0] !== TRANSFER) continue;
      if (!/^0x[0-9a-fA-F]{64}$/.test(String(l.data)) || !ADDR.test(String(l.address))) continue;
      out.push({ token: l.address.toLowerCase(), from: '0x' + l.topics[1].slice(-40).toLowerCase(), to: '0x' + l.topics[2].slice(-40).toLowerCase(), value: BigInt(l.data) });
    }
    return out;
  }
  function checkLine(list, ok, name, detail) {
    const li = el('li', ok ? 'ok' : 'bad');
    li.appendChild(el('span', 'try-mark', ok ? '✓' : '✗'));
    li.appendChild(el('span', 'try-check-name', name));
    if (detail) li.appendChild(el('span', 'try-check-detail', detail));
    list.appendChild(li);
    return ok;
  }
  async function checkHsp(wantTo) {
    const box = $('h-out');
    box.hidden = false;
    box.textContent = '';
    const r = hspRows[Number($('h-pick').value)];
    if (!r || !ADDR.test(wantTo)) { box.appendChild(el('p', 'try-err', 'Pick a payment and enter a 0x address of 40 hex characters.')); return; }
    const node = NODES[r.network];
    box.appendChild(el('p', 'try-wait', 'Reading ' + node.name + '…'));
    let receipt = null, tx = null, cid = null;
    try {
      [cid, receipt, tx] = await Promise.all([rpc(node, 'eth_chainId', []), rpc(node, 'eth_getTransactionReceipt', [r.tx]), rpc(node, 'eth_getTransactionByHash', [r.tx])]);
    } catch (e) { receipt = null; }
    box.textContent = '';
    if (!receipt || typeof receipt !== 'object' || !tx || typeof tx !== 'object') { box.appendChild(el('p', 'try-err', 'The public node did not answer. Try again in a moment.')); return; }
    const list = el('ul', 'try-checks');
    const hits = transfersOf(receipt).filter((t) => t.token === r.token.toLowerCase() && t.to === wantTo.toLowerCase());
    const payerHit = hits.filter((t) => t.from === r.payer.toLowerCase());
    let ok = checkLine(list, cid === node.chainId, 'The node is on ' + node.name, 'chain id ' + cid);
    ok = checkLine(list, receipt.status === '0x1', 'The transaction succeeded', 'block ' + parseInt(receipt.blockNumber, 16)) && ok;
    ok = checkLine(list, String(receipt.to).toLowerCase() === r.token.toLowerCase(), 'It calls the USDC contract HSP lists', r.token) && ok;
    ok = checkLine(list, payerHit.length === 1, 'One USDC transfer from the payer to ' + wantTo,
      payerHit.length === 1 ? usdc(payerHit[0].value.toString()).replace('USDC.e', 'USDC') + ' (' + payerHit[0].value + ' raw)' : hits.length + ' matching transfers') && ok;
    ok = checkLine(list, String(tx.from).toLowerCase() === HSP_RELAYER, 'Sent by HSP\'s gateway, not by the payer', 'broadcast from ' + tx.from + '; the payer only signed') && ok;
    const v = el('p', 'try-verdict ' + (ok ? 'is-accept' : 'is-reject'), ok ? 'ACCEPT' : 'REJECT');
    box.appendChild(v);
    box.appendChild(list);
    const link = el('p', 'try-link');
    const a = el('a', '', 'Open the transaction on the explorer');
    if (/^https:\/\/(base-sepolia|eth-sepolia|arbitrum-sepolia)\.blockscout\.com\/tx\/0x[0-9a-fA-F]{64}$/.test(r.tx_url)) {
      a.href = r.tx_url; a.target = '_blank'; a.rel = 'noopener'; link.appendChild(a); box.appendChild(link);
    }
  }
  async function loadHsp() {
    const pick = $('h-pick');
    if (!pick) return;
    let d = null;
    try {
      const r = await fetch('hsp_receipts.json', { credentials: 'omit', cache: 'no-store' });
      if (r.ok) d = await r.json();
    } catch (e) { d = null; }
    hspRows = (d && Array.isArray(d.receipts) ? d.receipts : []).filter((r) => r && B32.test(r.tx) && ADDR.test(r.token) && ADDR.test(r.payer) &&
      ADDR.test(r.pay_to) && Object.hasOwn(NODES, r.network)).reverse();
    hspRows.forEach((r, i) => {
      const o = el('option', '', r.time.slice(5, 16).replace('T', ' ') + ' UTC · ' + NODES[r.network].name + ' · ' + r.status.replace('payment-', '') + ' · ' + r.tx.slice(0, 10) + '…');
      o.value = String(i);
      pick.appendChild(o);
    });
    const sync = () => { const r = hspRows[Number(pick.value)]; if (r) $('h-to').value = r.pay_to; };
    pick.addEventListener('change', sync);
    sync();
    $('h-form').addEventListener('submit', (e) => { e.preventDefault(); checkHsp($('h-to').value.trim()); });
    $('h-lie').addEventListener('click', () => { $('h-to').value = WRONG_TO; checkHsp(WRONG_TO); });
  }
  loadHsp();
})();
