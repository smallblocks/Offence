"use strict";
document.getElementById("agent-url").textContent = window.location.origin + "/v1";
let providers = [];
let modelIds = {};
function element(tag, text, cls) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function render() {
  const area = document.getElementById("providers");
  area.replaceChildren();
  const filter = document.getElementById("filter").value.toLowerCase();
  for (const ad of providers) {
    const offer = ad.body.offer;
    if (!offer || !(offer.manifest.name + ad.signer + modelIds[ad.signer]).toLowerCase().includes(filter)) continue;
    const card = element("article", "", "card");
    card.append(element("div", offer.available ? "Advertised available" : "Unavailable", "label"));
    card.append(element("h2", offer.manifest.name));
    card.append(element("div", `${offer.output_msat_per_token_exact ?? offer.output_msat_per_token} msat / token`, "metric"));
    card.append(element("p", `${offer.manifest.context_tokens.toLocaleString()} context tokens · ${offer.manifest.quantization}`));
    card.append(element("p", `Deadline ${offer.generation_deadline_s}s · Batch ${offer.batch_tokens} tokens`));
    card.append(element("code", ad.signer));
    card.append(element("p", "Model identity", "label"));
    card.append(element("code", modelIds[ad.signer] || ""));
    card.append(element("p", "Execution proof unavailable", "small"));
    area.append(card);
  }
  if (!area.children.length) area.append(element("p", "No matching provider advertisements. Add a peer address in Configure Node."));
}
async function refresh() {
  try {
    const [status, listing] = await Promise.all([fetch("/v1/status").then(r => r.json()), fetch("/v1/providers").then(r => r.json())]);
    document.getElementById("network-name").textContent = status.network === "offence-v1" ? "Seller-claim network" : "Lab network";
    document.getElementById("payment-status").textContent = status.production_payments === "configured" ? "Mainnet wallet configured. Check the model offer before purchasing." : "Mainnet payments are disabled.";
    document.getElementById("identity").replaceChildren(element("code", status.identity));
    const cards = document.getElementById("status"); cards.replaceChildren();
    for (const [label, value] of [["Known peers", status.known_peers], ["Active sessions", status.active_sessions], ["Backend", status.backend]]) {
      const card = element("div", "", "card");
      card.append(element("div", label, "label"), element("p", String(value), "metric")); cards.append(card);
    }
    document.getElementById("wallet-status").textContent = status.wallet === "strike"
      ? `Strike: ${status.receiving_address}. Supplier-dependent key recovery requires buyer opt-in.`
      : status.wallet === "disabled" ? "Receiving payments is disabled." : `Wallet: ${status.wallet || "disabled"}`;
    const totals = status.token_totals;
    const tokenCards = document.getElementById("token-totals"); tokenCards.replaceChildren();
    if (totals) {
      for (const [label, count] of [["Total tokens served", totals.served_tokens], ["Settled paid tokens", totals.paid_tokens]]) {
        const card = element("div", "", "card");
        card.append(element("div", label, "label"), element("p", count.toLocaleString(), "metric"));
        tokenCards.append(card);
      }
      const byModel = document.getElementById("token-models"); byModel.replaceChildren();
      for (const model of totals.models) {
        const row = element("p", `${model.model_name || "Model"}: ${model.served_tokens.toLocaleString()} served · ${model.paid_tokens.toLocaleString()} paid · `);
        row.append(element("code", model.model_id)); byModel.append(row);
      }
    }
    document.getElementById("error").textContent = status.discovery_error ? `Last discovery issue: ${status.discovery_error}` : "";
    providers = listing.providers; modelIds = listing.model_ids; render();
  } catch (_) { document.getElementById("error").textContent = "Cannot reach this node. Retrying shortly."; }
}
document.getElementById("filter").addEventListener("input", render);
refresh(); setInterval(refresh, 10000);
