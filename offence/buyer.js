'use strict';
const el = id => document.getElementById(id);
let owner = location.hash.slice(1) || sessionStorage.getItem('offence-owner') || '';
history.replaceState(null, '', '/');
let settings, agentKey;
const amounts = ['max_price_msat','request_limit_msat','daily_limit_msat','fee_per_batch_msat','total_fee_limit_msat'];
const arrays = ['model_ids','providers','trusted_providers','seeds','approved_origins'];
const numbers = ['max_price_msat','request_limit_msat','daily_limit_msat','fee_per_batch_msat','total_fee_limit_msat','max_output_tokens','daily_output_tokens','min_context_tokens','request_deadline_s','max_concurrent'];
function message(text) { el('message').textContent = text; }
async function api(path, options = {}, key = owner) {
  const r = await fetch(path, {...options, headers: {'Content-Type':'application/json',Authorization:'Bearer '+key}});
  const body = await r.json();
  if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Request rejected. Check your policy values.');
  return body;
}
function lines(id) { return el(id).value.split('\n').map(v => v.trim()).filter(Boolean); }
function add(id, value) { const values = lines(id); if (!values.includes(value)) values.push(value); el(id).value = values.join('\n'); message('Added to the form. Save your policy to apply.'); }
function showOffers(providers) {
  const target = el('offers'); target.replaceChildren();
  if (!providers.length) { target.textContent = 'No model offers found yet. A reachable seed may currently have no available model.'; return; }
  for (const p of providers) {
    const box = document.createElement('article'); box.className = 'offer';
    const title = document.createElement('h3'); title.textContent = p.name; box.append(title);
    for (const text of [(Number(p.output_msat_per_token_exact ?? p.output_msat_per_token)/1000)+' sats/token · '+p.context_tokens+' context tokens · '+p.network,
      (p.available && p.text_chat ? 'Advertised available for text chat' : 'Not currently eligible for text chat'),
      'Model: '+p.model_id, 'Supplier: '+p.provider, 'Origin: '+p.endpoint,
      p.local_observations && p.local_observations.latency_ms !== null ? 'Your measured first output: '+p.local_observations.latency_ms+' ms' : 'No local latency measurement yet']) {
      const detail = document.createElement('small'); detail.textContent = text; box.append(detail);
    }
    for (const [label, field, value] of [['Allow model','model_ids',p.model_id],['Trust supplier','trusted_providers',p.provider],['Pin supplier','providers',p.provider]]) {
      const button = document.createElement('button'); button.textContent = label; button.onclick = () => add(field,value); box.append(button);
    }
    target.append(box);
  }
}
async function load() {
  const s = await api('/admin/state'); settings = s.settings; agentKey = s.agent_key;
  sessionStorage.setItem('offence-owner',owner); el('unlock').hidden = true; el('workspace').hidden = false;
  for (const id of arrays) el(id).value = settings[id].join('\n');
  for (const id of numbers) el(id).value = amounts.includes(id) ? settings[id]/1000 : settings[id];
  el('wallet_managed_fees').checked = settings.wallet_managed_fees;
  el('supplier-credit').textContent = (s.supplier_credit || []).map(c => 'Supplier '+c.provider+': '+(c.available_estimate_msat/1000)+' sats locally accounted available credit; '+c.pending_sessions+' unresolved reservations.').join(' ');
  el('wallet-status').textContent = s.nwc_saved ? 'Wallet connection saved locally. '+(settings.wallet === 'nwc-mainnet' && s.wallet_ready ? 'Payments enabled under your saved policy.' : 'Choose NWC and save your budget to enable payments.') : 'No NWC wallet connected.';
  for (const id of ['strategy','privacy','assurance','wallet']) el(id).value = settings[id];
  el('max_latency_ms').value = settings.max_latency_ms ?? '';
  el('tor_proxy').value = settings.tor_proxy || '';
  el('allow_provider_key_release').checked = settings.allow_provider_key_release;
  el('allow_prepaid_compute').checked = settings.allow_prepaid_compute;
  el('allow_unknown_suppliers').checked = settings.allow_unknown_suppliers;
  el('base-url').value = s.base_url; el('agent-key').value = agentKey;
  el('peers').textContent = s.known_peers; el('tokens').textContent = s.received_tokens;
  el('cost').textContent = (s.received_output_msat/1000).toLocaleString()+' sats';
  el('discovery-status').textContent = (s.discovery_error ? 'Some graph connections failed. ' : '')+(s.wallet_ready ? 'Wallet mode ready: ' : 'Wallet connection needs attention: ')+settings.wallet;
  showOffers((await api('/admin/providers')).providers);
}
async function action(fn) { try { await fn(); } catch (e) { message(e.message); } }
el('unlock-button').onclick = () => action(async () => {owner = el('owner-key').value.trim(); await load();});
el('refresh').onclick = () => action(async () => {
  el('refresh').disabled = true;
  try { await api('/admin/refresh',{method:'POST',body:'{}'}); const s = await api('/admin/state'); el('peers').textContent=s.known_peers; showOffers((await api('/admin/providers')).providers); message(s.discovery_error ? 'Refresh completed with connection failures. Your form edits are unchanged.' : 'Graph refreshed. Your form edits are unchanged.'); }
  finally {el('refresh').disabled=false;}
});
el('settings').onsubmit = e => {e.preventDefault(); action(async () => {
  const next = {...settings}; for (const id of arrays) next[id] = lines(id);
  for (const id of numbers) {const raw = el(id).value; if (amounts.includes(id)) { if (!/^\d+(\.\d{1,3})?$/.test(raw)) throw new Error('Use positive sats with up to three decimal places.'); const [whole, fraction = ''] = raw.split('.'); next[id] = Number(whole)*1000 + Number(fraction.padEnd(3,'0')); } else next[id] = Number(raw); if (!Number.isSafeInteger(next[id])) throw new Error('Invalid limit.');}
  for (const id of ['strategy','privacy','assurance','wallet']) next[id] = el(id).value;
  next.max_latency_ms = el('max_latency_ms').value ? Number(el('max_latency_ms').value) : null;
  next.tor_proxy = el('tor_proxy').value.trim() || null;
  next.wallet_managed_fees = el('wallet_managed_fees').checked;
  next.allow_prepaid_compute = el('allow_prepaid_compute').checked;
  next.allow_unknown_suppliers = el('allow_unknown_suppliers').checked;
  next.allow_provider_key_release = el('allow_provider_key_release').checked;
  if (['lnd-mainnet','nwc-mainnet'].includes(next.wallet) && !confirm('Allow your agent to spend real Lightning funds under these limits? Suppliers receive the text you send.')) return;
  await api('/admin/settings',{method:'PUT',body:JSON.stringify(next)}); await load(); message('Policy saved. Your agent can only buy within these rules.');
});};
el('copy-key').onclick = () => action(async () => {await navigator.clipboard.writeText(agentKey); message('Agent key copied. Keep it private.');});
el('test').onclick = () => action(async () => {
  const prompt=el('test-prompt').value.trim(); if (!prompt) throw new Error('Enter a test prompt.');
  if (!confirm('Send this prompt using your saved policy? Paid output uses your wallet.')) return;
  el('test').disabled=true; el('test-output').textContent='Waiting for signed output…';
  try {const r=await api('/v1/chat/completions',{method:'POST',body:JSON.stringify({model:'auto',messages:[{role:'user',content:prompt}],max_tokens:Math.min(64,settings.max_output_tokens)})},agentKey); el('test-output').textContent=r.choices[0].message.content; await load(); message('Received '+r.offence.output_tokens+' tokens, '+r.offence.spent_msat+' msat output charge. Total compute charge: '+r.offence.compute_charge_msat+' msat.');}
  catch(e) {el('test-output').textContent=e.message; throw e;}
  finally {el('test').disabled=false;}
});
el('connect-wallet').onclick = () => action(async () => {
  const connection = el('nwc-connection').value.trim(); el('nwc-connection').value = '';
  if (!connection) throw new Error('Paste a connection from your wallet first.');
  el('connect-wallet').disabled = true;
  try {await api('/admin/wallet/connect', {method:'POST',body:JSON.stringify({connection})}); await load(); el('wallet').value='nwc-mainnet'; message('Wallet connected. Set your budget, accept seller claims and wallet-managed fees, then save. No funds spent.');}
  finally {el('connect-wallet').disabled=false;}
});
el('disconnect-wallet').onclick = () => action(async () => {
  const result = await api('/admin/wallet/disconnect',{method:'POST',body:'{}'}); await load();
  message('Payments stopped. Revoke the connection in your wallet too.'+(result.credential_retained_for_recovery ? ' Local credentials retained to recover uncertain payments.' : ' Local connection removed.'));
});
el('recover-payments').onclick = () => action(async () => {
  const result = await api('/admin/payments/recover',{method:'POST',body:'{}'});
  await load();
  message('Checked '+result.payments.length+' payments without sending funds. Unknown outcomes stay reserved.');
});
if (owner) action(load);
