// USD estimates for the token meter.
//
// `docs/openapi/openapi.yaml` publishes token counts and a `token_cap`, but no
// per-provider price list and no USD cap - `config/policies.yaml` has no `usd` key
// either. So the console never invents a *cap*; it multiplies the reported token
// volume by the documented rate table below and says so in the UI. The table is an
// estimate for orientation, not a bill: it is listed as a spec change request in
// `docs/arena-log/step-12/` (a `usd_spent` / `usd_cap` pair on `RunUsageView`).

import type { UsageBreakdown } from './types'

export interface Rate {
  /** USD per 1,000,000 input tokens. */
  input: number
  /** USD per 1,000,000 output tokens. */
  output: number
  /** Cached prompt tokens are billed at this fraction of the input rate. */
  cachedMultiplier: number
}

/** Strong / mid / cheap tiers, mirroring `config/policies.yaml: model_tiers`. */
export const RATES: Record<'strong' | 'mid' | 'cheap', Rate> = {
  strong: { input: 15, output: 75, cachedMultiplier: 0.1 },
  mid: { input: 3, output: 15, cachedMultiplier: 0.1 },
  cheap: { input: 0.8, output: 4, cachedMultiplier: 0.1 },
}

/** Blended estimate: most calls in a run are mid-tier writers/researchers. */
export const DEFAULT_TIER: keyof typeof RATES = 'mid'

export function estimateCostUsd(
  usage: Pick<UsageBreakdown, 'in' | 'out' | 'cached'>,
  tier: keyof typeof RATES = DEFAULT_TIER,
): number {
  const rate = RATES[tier]
  const input = (usage.in / 1_000_000) * rate.input
  const output = (usage.out / 1_000_000) * rate.output
  const cached = (usage.cached / 1_000_000) * rate.input * rate.cachedMultiplier
  return input + output + cached
}

/**
 * USD value of a token cap, split 65/35 input/output - the same split the
 * `docs/openapi/examples/usage/usage.response.200.json` fixture reports. Used only to
 * give the cost meter a honest denominator when the API publishes none.
 */
export function capEquivalentUsd(tokenCap: number, tier: keyof typeof RATES = DEFAULT_TIER): number {
  return estimateCostUsd(
    { in: Math.round(tokenCap * 0.65), out: Math.round(tokenCap * 0.35), cached: 0 },
    tier,
  )
}
