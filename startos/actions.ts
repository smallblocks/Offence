import { z } from '@start9labs/start-sdk'
import { sdk } from './sdk'
import { configFile, backendKeyFile, gatewayKeyFile, walletSecretFile } from './config'

const { InputSpec, Value } = sdk
const offerShape = z.object({
  manifest: z.object({
    format: z.literal('offence/model/v1').optional(),
    name: z.string().min(1).max(128), architecture: z.string().min(1).max(128),
    quantization: z.string().min(1).max(128), context_tokens: z.number().int().min(1).max(10000000),
    artifacts: z.array(z.object({
      path: z.string().min(1).max(256).refine(p => !p.startsWith('/') && !p.split('/').includes('..') && !p.includes('\\')),
      sha256: z.string().regex(/^[0-9a-f]{64}$/), size: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
      role: z.enum(['weights', 'tokenizer', 'config', 'adapter', 'template', 'runtime']),
    }).strict()).min(1).max(256).refine(a => a.some(f => f.role === 'weights') && new Set(a.map(f => f.path)).size === a.length),
    sources: z.array(z.string()).max(16).optional(),
  }).strict(),
  output_msat_per_token: z.number().int().min(0).max(1000000000),
  output_msat_per_token_exact: z.string().max(64).regex(/^[0-9]+(?:\.[0-9]+)?$/).nullable().optional(),
  batch_tokens: z.number().int().min(1).max(128).optional(),
  max_output_tokens: z.number().int().min(1).max(32768).optional(),
  generation_deadline_s: z.number().int().min(1).max(3600).optional(),
  payment_timeout_s: z.number().int().min(5).max(600).optional(),
  text_chat: z.boolean().optional(), proof: z.literal('unavailable').optional(), available: z.boolean().optional(),
  hardware: z.string().max(256).nullable().optional(),
}).strict()
const configure = sdk.Action.withInput(
  'configure-node',
  async () => ({
    name: 'Configure Node',
    description: 'Set peer addresses, GPU backend and a signed model offer.',
    warning: null, allowedStatuses: 'any', group: null, visibility: 'enabled',
  }),
  InputSpec.of({
    endpoint: Value.text({ name: 'Advertised address', description: 'Your reachable onion or HTTPS origin. Leave empty to discover peers without advertising.', required: false, default: null }),
    seeds: Value.text({ name: 'Peer addresses', description: 'Comma-separated peer origins used to join the network.', required: false, default: null }),
    privatePeers: Value.text({ name: 'Approved LAN peers', description: 'Exact peer origins permitted on your private network, including this node if advertising its LAN address.', required: false, default: null }),
    torProxy: Value.text({ name: 'Tor SOCKS proxy', description: 'Reachable socks5h:// address for outbound onion connections. An onion interface alone does not provide an outbound proxy.', required: false, default: null }),
    backend: Value.select({ name: 'Inference backend', default: 'none', values: { none: 'Discovery only', fixture: 'Protocol fixture (no model)', llamacpp: 'llama.cpp GPU server', vllm: 'vLLM GPU server' } }),
    backendUrl: Value.text({ name: 'GPU server address', description: 'Fixed HTTP(S) origin of your inference server, without /v1. Never expose its management API to buyers.', required: false, default: null }),
    backendModel: Value.text({ name: 'vLLM served model', description: 'Exact model identifier returned by the configured vLLM server.', required: false, default: null }),
    supplierLimits: Value.text({ name: 'Supplier limits JSON', description: 'Optional max_sessions (1-64), max_requests_per_hour (1-100000), max_work_tokens_per_hour (1-100000000), max_storage_mb (16-4096). Each admitted request reserves the full offered context against the hourly work limit.', required: false, default: null }),
    backendKey: Value.text({ name: 'GPU server API key', description: 'Write-only. Leave empty to preserve the saved key.', required: false, default: null, masked: true }),
    offer: Value.text({ name: 'Model offer JSON', description: 'Paste an offer containing an exact model manifest. Examples and field definitions are in the package instructions.', required: false, default: null }),
    allowLab: Value.toggle({ name: 'Allow unverified lab inference', description: 'Allows free experiments. This does not enable paid production or verify model execution.', default: false }),
  }),
  async ({ effects }) => {
    const config = await configFile.read().const(effects)
    if (!config) return {}
    return {
      endpoint: String(config.endpoint || ''),
      seeds: (config.seeds as string[] || []).join(', '),
      privatePeers: (config.allowed_private_peers as string[] || []).join(', '),
      torProxy: String(config.tor_proxy || ''),
      backend: config.backend as 'none' | 'fixture' | 'llamacpp' | 'vllm',
      backendUrl: String(config.backend_url || ''),
      backendModel: String(config.backend_model || ''),
      supplierLimits: JSON.stringify({ max_sessions: config.max_sessions || 2, max_requests_per_hour: config.max_requests_per_hour || 120, max_work_tokens_per_hour: config.max_work_tokens_per_hour || 1000000, max_storage_mb: config.max_storage_mb || 128 }),
      offer: config.offer ? JSON.stringify(config.offer, null, 2) : '',
      allowLab: Boolean(config.allow_lab_unverified),
    }
  },
  async ({ effects, input }) => {
    const list = (value: string | null | undefined) => (value || '').split(',').map(x => x.trim().replace(/\/+$/, '')).filter(Boolean)
    const offer = input.offer?.trim() ? offerShape.parse(JSON.parse(input.offer)) : null
    if (list(input.seeds).length > 32 || list(input.privatePeers).length > 32) throw new Error('At most 32 configured peers are supported.')
    if (['llamacpp', 'vllm'].includes(input.backend)) {
      const url = new URL(input.backendUrl || '')
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname) || url.port === '0') throw new Error('Set a fixed HTTP(S) origin without credentials or a path.')
    }
    if (input.backend === 'vllm' && !input.backendModel?.trim()) throw new Error('Set the exact vLLM served model.')
    const limits = z.object({
      max_sessions: z.number().int().min(1).max(64).optional(),
      max_requests_per_hour: z.number().int().min(1).max(100000).optional(),
      max_work_tokens_per_hour: z.number().int().min(1).max(100000000).optional(),
      max_storage_mb: z.number().int().min(16).max(4096).optional(),
    }).strict().parse(input.supplierLimits?.trim() ? JSON.parse(input.supplierLimits) : {})
    if (input.backendKey?.trim()) await backendKeyFile.write(effects, input.backendKey.trim())
    await configFile.write(effects, {
      ...await configFile.read().once(), ...limits,
      endpoint: (input.endpoint || '').trim().replace(/\/+$/, ''),
      seeds: list(input.seeds), allowed_private_peers: list(input.privatePeers),
      tor_proxy: input.torProxy?.trim() || null, backend: input.backend,
      backend_url: (input.backendUrl || '').trim().replace(/\/+$/, ''),
      backend_model: input.backendModel?.trim() || '',
      offer, allow_lab_unverified: input.allowLab,
    })
    return { version: '1', title: 'Node configured', message: 'Configuration saved. Open the dashboard to check discovery and payment configuration.', result: null }
  },
)
const configureBuyer = sdk.Action.withInput(
  'configure-buyer',
  async () => ({ name: 'Configure Buyer API', description: 'Give agents a private API key, routing policies and shared job limits. Free lab inference only.', warning: null, allowedStatuses: 'any', group: null, visibility: 'enabled' }),
  InputSpec.of({
    policies: Value.text({ name: 'Routing policies JSON', description: 'Policies select signed offers automatically. Each needs an alias and ordered exact model_ids. Optional strategy: cheapest, fastest, preferred-model or balanced. Use privacy trusted-only and trusted_providers to restrict disclosure.', required: true, default: '[]' }),
    limits: Value.text({ name: 'Buyer and job limits JSON', description: 'Optional max_concurrent, daily_output_tokens, request_deadline_s, max_active_jobs, max_job_tokens and max_job_retries. Limits apply across jobs and direct chat.', required: false, default: null }),
    routes: Value.text({ name: 'Model routes JSON', description: 'Array of alias, endpoint, provider public key, model_id and optional max_output_tokens. LAN origins must also be listed in Approved LAN peers.', required: true, default: '[]' }),
    apiKey: Value.text({ name: 'Buyer API key', description: 'At least 32 random characters. Leave empty to preserve the saved key. Give this key only to your own agents.', required: false, default: null, masked: true }),
    allowLab: Value.toggle({ name: 'Enable free lab purchases', description: 'Allow configured agents to request unverified free inference. All paid quotes remain blocked.', default: false }),
  }),
  async ({ effects }) => {
    const config = await configFile.read().const(effects)
    const gateway = (config?.gateway || {}) as { [key: string]: unknown; routes?: unknown[]; policies?: unknown[]; allow_free_lab?: boolean }
    return { policies: JSON.stringify(gateway.policies || [], null, 2), limits: JSON.stringify(Object.fromEntries(['max_concurrent', 'daily_output_tokens', 'request_deadline_s', 'max_active_jobs', 'max_job_tokens', 'max_job_retries'].filter(k => gateway[k] !== undefined).map(k => [k, gateway[k]])), null, 2), routes: JSON.stringify(gateway.routes || [], null, 2), allowLab: Boolean(gateway.allow_free_lab) }
  },
  async ({ effects, input }) => {
    const routes = z.array(z.object({
      alias: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/),
      endpoint: z.string().url().max(512), provider: z.string().regex(/^[0-9a-f]{64}$/),
      model_id: z.string().regex(/^[0-9a-f]{64}$/), max_output_tokens: z.number().int().min(1).max(32768).optional(),
    }).strict()).max(64).refine(r => new Set(r.map(x => x.alias)).size === r.length).parse(JSON.parse(input.routes))
    const identities = z.array(z.string().regex(/^[0-9a-f]{64}$/)).max(128).refine(a => new Set(a).size === a.length)
    const policies = z.array(z.object({
      alias: z.string().regex(/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/),
      model_ids: identities.refine(a => a.length >= 1 && a.length <= 32),
      providers: identities.optional(), trusted_providers: identities.optional(),
      strategy: z.enum(['cheapest', 'fastest', 'preferred-model', 'balanced']).optional(),
      privacy: z.enum(['any', 'trusted-only']).optional(),
      max_output_tokens: z.number().int().min(1).max(32768).optional(),
      min_context_tokens: z.number().int().min(1).max(10000000).optional(),
    }).strict().refine(p => p.privacy !== 'trusted-only' || Boolean(p.trusted_providers?.length))).max(32).parse(JSON.parse(input.policies))
    const aliases = [...routes, ...policies].map(r => r.alias)
    if (new Set(aliases).size !== aliases.length) throw new Error('Routes and policies need unique aliases.')
    const limits = z.object({
      max_concurrent: z.number().int().min(1).max(16).optional(),
      daily_output_tokens: z.number().int().min(1).max(10000000).optional(),
      request_deadline_s: z.number().int().min(1).max(3600).optional(),
      max_active_jobs: z.number().int().min(1).max(8).optional(),
      max_job_tokens: z.number().int().min(1).max(100000).optional(),
      max_job_retries: z.number().int().min(0).max(2).optional(),
    }).strict().parse(input.limits?.trim() ? JSON.parse(input.limits) : {})
    const key = input.apiKey?.trim() || await gatewayKeyFile.read().once()
    if ((routes.length || policies.length) && (!key || key.length < 32)) throw new Error('Set a random buyer API key of at least 32 characters.')
    const config = await configFile.read().once() || {}
    const approved = (config.allowed_private_peers || []) as string[]
    for (const route of routes) {
      const url = new URL(route.endpoint)
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) throw new Error('Model routes require HTTP(S) origins.')
      if (!approved.includes(route.endpoint.replace(/\/+$/, ''))) throw new Error('Add each buyer route to Approved LAN peers first. Buyer routes are explicitly operator-approved.')
    }
    if (input.apiKey?.trim()) await gatewayKeyFile.write(effects, input.apiKey.trim())
    await configFile.write(effects, { ...config, gateway: { ...(config.gateway as object || {}), ...limits, routes, policies, allow_free_lab: input.allowLab } })
    return { version: '1', title: 'Buyer API configured', message: 'Connect your agent to this service URL with /v1, the saved API key and a configured model alias. Paid inference remains blocked.', result: null }
  },
)
const payments = sdk.Action.withInput(
  'configure-payments',
  async () => ({ name: 'Configure Payments', description: 'Set Lightning and seller-claim pricing. Use regtest until payment validation is complete.', warning: 'Seller claims are not cryptographic execution proofs.', allowedStatuses: 'any', group: null, visibility: 'enabled' }),
  InputSpec.of({
    network: Value.select({ name: 'Lightning network', default: 'disabled', values: { disabled: 'Disabled', 'lnd-regtest': 'Regtest', 'lnd-mainnet': 'Self-hosted LND (mainnet)', strike: 'Strike hosted wallet (mainnet)' } }),
    strikeAddress: Value.text({ name: 'Strike Lightning Address', description: 'username@strike.me. Invoices route to this receiving account. Strike manages its liquidity.', required: false, default: null }),
    strikeKey: Value.text({ name: 'Strike receiving API key', description: 'Account profile read, create invoice for receiver, invoice quote and invoice read permissions only. No sending or payout permissions. Blank preserves saved key.', required: false, default: null, masked: true }),
    url: Value.text({ name: 'LND HTTPS address', required: false, default: null }),
    macaroon: Value.text({ name: 'LND invoice macaroon (hex)', description: 'Receive-only credential with invoice read/write and getinfo access. Blank preserves saved credentials.', required: false, default: null, masked: true }),
    certificate: Value.text({ name: 'LND TLS certificate (PEM)', required: false, default: null }),
    mode: Value.select({ name: 'Pricing mode', default: 'sats-per-token', values: { 'sats-per-token': 'Sats per token', 'cents-per-kwh': 'US cents per kWh' } }),
    rate: Value.text({ name: 'Price', description: 'Positive decimal in the selected pricing unit.', required: true, default: '0.001' }),
    joules: Value.text({ name: 'Measured joules per output token', description: 'Required for energy pricing. Include prompt processing in your measurement.', required: false, default: null }),
    exchange: Value.text({ name: 'USD per BTC', description: 'Operator-supplied conversion rate for the one-cent prepaid deposit and energy pricing. Update when needed; accepted quotes retain their price.', required: false, default: null }),
  }),
  async () => {
    const config = await configFile.read().once() || {}
    const pricing = (config.pricing || {}) as Record<string, unknown>
    return { network: z.enum(['disabled', 'lnd-regtest', 'lnd-mainnet', 'strike']).parse(config.lightning || 'disabled'), strikeAddress: config.strike_address ? String(config.strike_address) : null,
      strikeKey: null, url: null, macaroon: null, certificate: null,
      mode: z.enum(['sats-per-token', 'cents-per-kwh']).parse(pricing.mode || 'sats-per-token'),
      rate: String((pricing.mode === 'cents-per-kwh' ? pricing.cents_per_kwh : pricing.sats_per_token) || '0.001'),
      joules: pricing.joules_per_token ? String(pricing.joules_per_token) : null, exchange: pricing.usd_per_btc ? String(pricing.usd_per_btc) : null }
  },
  async ({ effects, input }) => {
    const positive = (value: string | null | undefined) => {
      if (!value || !/^(?:[0-9]+)(?:\.[0-9]+)?$/.test(value) || !Number.isFinite(Number(value)) || Number(value) <= 0 || Number(value) > 1e15) throw new Error('Enter positive decimal pricing inputs.')
      return value
    }
    positive(input.rate)
    if (['lnd-mainnet', 'strike'].includes(input.network)) positive(input.exchange)
    if (input.mode === 'cents-per-kwh') { positive(input.joules); positive(input.exchange) }
    const saved = JSON.parse(await walletSecretFile.read().once() || '{}')
    const wallet = { url: input.url?.trim() || saved.url, macaroon: input.macaroon?.trim() || saved.macaroon, certificate: input.certificate?.trim() || saved.certificate, strikeKey: input.strikeKey?.trim() || saved.strikeKey }
    if (['lnd-mainnet', 'lnd-regtest'].includes(input.network)) {
      const url = new URL(wallet.url || '')
      if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) throw new Error('Use a fixed HTTPS LND origin.')
      if (!/^(?:[0-9a-fA-F]{2})+$/.test(wallet.macaroon || '') || !wallet.certificate?.includes('BEGIN CERTIFICATE')) throw new Error('Provide an invoice macaroon and TLS certificate.')
    }
    const config = await configFile.read().once() || {}
    const strikeAddress = (input.strikeAddress?.trim() || String(config.strike_address || '')).toLowerCase()
    if (input.network === 'strike' && (!/^[a-z0-9_.-]{1,64}@strike\.me$/.test(strikeAddress) || !wallet.strikeKey || /\s/.test(wallet.strikeKey))) throw new Error('Provide a Strike Lightning Address and receiving API key.')
    if (['lnd-mainnet', 'strike'].includes(input.network) && config.backend === 'fixture') throw new Error('A fixture cannot accept mainnet payments.')
    await walletSecretFile.write(effects, JSON.stringify(wallet))
    await configFile.write(effects, { ...config, lightning: input.network, strike_address: strikeAddress, allow_seller_claim: input.network !== 'disabled',
      allow_lab_unverified: input.network === 'lnd-regtest' ? true : config.allow_lab_unverified || false,
      pricing: { mode: input.mode, sats_per_token: input.mode === 'sats-per-token' ? input.rate : null,
        cents_per_kwh: input.mode === 'cents-per-kwh' ? input.rate : null,
        joules_per_token: input.joules || null, usd_per_btc: input.exchange || null } })
    return { version: '1', title: 'Payment configuration saved', message: 'Check wallet connectivity and pricing. Strike uses supplier-dependent key recovery and requires explicit buyer opt-in. No real-funds test has been performed.', result: null }
  },
)
export const actions = sdk.Actions.of().addAction(configure).addAction(configureBuyer).addAction(payments)
