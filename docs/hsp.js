/* Real HSP payments table. Reads docs/hsp_receipts.json (written by hsp_qa/publish_receipts.py after re-checking every
   line with HSP and on-chain). Every value is validated and written with textContent; links only to known explorers. */
'use strict';
(function () {
  const ID = /^[A-Za-z0-9_.:\-]{1,128}$/;
  const TX = /^0x[0-9a-fA-F]{64}$/;
  const TX_URL = /^https:\/\/(base-sepolia|eth-sepolia|arbitrum-sepolia)\.blockscout\.com\/tx\/0x[0-9a-fA-F]{64}$/;
  const STATUS = /^payment-(safe|finalized)$/;
  const NET = { 'base-sepolia': 'Base Sepolia', sepolia: 'Ethereum Sepolia', 'arbitrum-sepolia': 'Arbitrum Sepolia' };
  const $ = (id) => document.getElementById(id);
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = String(text);
    return e;
  }
  function good(r) {
    return r && typeof r === 'object' && ID.test(r.flow_id) && ID.test(r.payment_request_id) && TX.test(r.tx) &&
      TX_URL.test(r.tx_url) && STATUS.test(r.status) && r.attribution_verified === true && Object.hasOwn(NET, r.network) &&
      /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$/.test(r.time);
  }
  function shortId(s) { return s.length > 16 ? s.slice(0, 8) + '…' + s.slice(-6) : s; }
  function cell(tr, node) {
    const td = el('td');
    if (typeof node === 'string') td.textContent = node; else td.appendChild(node);
    tr.appendChild(td);
  }
  function row(r) {
    const tr = el('tr');
    cell(tr, r.time.slice(5, 16).replace('T', ' '));
    cell(tr, NET[r.network] + (r.kind === 'a2a' ? ' · agent to agent' : ''));
    const f = el('span', 'mono', shortId(r.flow_id)); f.title = r.flow_id; cell(tr, f);
    const p = el('span', 'mono', shortId(r.payment_request_id)); p.title = r.payment_request_id; cell(tr, p);
    cell(tr, r.status.replace('payment-', ''));
    cell(tr, 'true');
    const a = el('a', 'mono', r.tx.slice(0, 10) + '…');
    a.href = r.tx_url; a.target = '_blank'; a.rel = 'noopener';
    cell(tr, a);
    return tr;
  }
  async function load() {
    const box = $('hsp-sum');
    if (!box) return;
    let d = null;
    try {
      const r = await fetch('hsp_receipts.json', { credentials: 'omit', cache: 'no-store' });
      if (r.ok) d = await r.json();
    } catch (e) { d = null; }
    const rows = d && Array.isArray(d.receipts) ? d.receipts.filter(good) : [];
    if (!rows.length) { box.textContent = 'The receipts file could not be loaded.'; return; }
    const a2a = rows.filter((r) => r.kind === 'a2a').length;
    const nets = new Set(rows.map((r) => r.network)).size;
    box.textContent = rows.length + ' payments, ' + a2a + ' of them agent to agent, on ' + nets +
      (nets === 1 ? ' testnet' : ' testnets') + ', all with attribution_verified true. Newest first.';
    const body = $('hsp-rows');
    rows.slice().reverse().forEach((r) => body.appendChild(row(r)));
  }
  load();
})();
