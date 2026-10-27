import { capEquivalentUsd, estimateCostUsd } from './costs'
import { formatCompact, formatInt, formatPercent, formatUsd } from './format'
import type { RunUsageView } from './types'

export interface UsageMetersProps {
  usage: RunUsageView | null
  /** Tokens the run view reports; used while `/usage` is still loading. */
  tokensSpent?: number | undefined
}

function Meter({
  label,
  value,
  cap,
  valueText,
  hint,
  tone,
}: {
  label: string
  value: number
  cap: number
  valueText: string
  hint?: string | undefined
  tone: 'ok' | 'warn' | 'bad'
}): React.JSX.Element {
  const ratio = cap > 0 ? Math.min(1, Math.max(0, value / cap)) : 0
  const percent = Math.round(ratio * 100)
  return (
    <div className="meter">
      <div className="meter-head">
        <span className="field-label">{label}</span>
        <span className="mono small">
          {valueText} <span className="dim">/ {formatCompact(cap)}</span>
        </span>
      </div>
      <div
        className={`meter-track meter-${tone}`}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        aria-valuetext={`${percent}% of cap`}
      >
        <span style={{ width: `${percent}%` }} />
      </div>
      <div className="meter-foot small dim">
        <span>{percent}% of cap</span>
        {hint && <span>{hint}</span>}
      </div>
    </div>
  )
}

function toneFor(ratio: number): 'ok' | 'warn' | 'bad' {
  if (ratio >= 0.95) return 'bad'
  if (ratio >= 0.75) return 'warn'
  return 'ok'
}

/**
 * Token and cost meters against the caps the run reports.
 *
 * Tokens and iterations have a real cap on the wire (`token_cap`, `max_iterations`,
 * both already raised by any `add_budget` grant). USD does not: policies v1 has no
 * spend ceiling, so the cost meter is labelled as an estimate measured against the
 * USD equivalent of the token cap (see `costs.ts` and the spec-change request).
 */
export function UsageMeters({ usage, tokensSpent = 0 }: UsageMetersProps): React.JSX.Element {
  if (!usage) {
    return (
      <div className="empty-state" role="status">
        Loading usage… the meters appear once the first provider call is billed.
      </div>
    )
  }
  const spent = usage.total || tokensSpent
  const cap = usage.token_cap
  const tokenRatio = cap > 0 ? spent / cap : 0
  const cost = estimateCostUsd(usage.agents_total) + estimateCostUsd(usage.planner)
  const costCap = capEquivalentUsd(cap)
  const iterRatio = usage.max_iterations > 0 ? usage.iterations / usage.max_iterations : 0

  return (
    <div className="stack-sm">
      <Meter
        label="Tokens"
        value={spent}
        cap={cap}
        valueText={formatInt(spent)}
        hint={`${formatPercent(spent, cap)} used${spent >= cap ? ' · cap reached' : ''}`}
        tone={toneFor(tokenRatio)}
      />
      <Meter
        label="Iterations"
        value={usage.iterations}
        cap={usage.max_iterations}
        valueText={String(usage.iterations)}
        hint="policy limit plus any extra-iteration grant"
        tone={toneFor(iterRatio)}
      />
      <Meter
        label="Est. cost"
        value={cost}
        cap={costCap}
        valueText={formatUsd(cost)}
        hint="USD estimate at mid tier · no USD cap in policies v1, shown against the token-cap equivalent"
        tone={toneFor(costCap > 0 ? cost / costCap : 0)}
      />

      <div className="usage-split">
        <h4 className="field-label">By role</h4>
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">Tokens</th>
              <th scope="col">Est.</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(usage.by_role).map(([role, breakdown]) => (
              <tr key={role}>
                <th scope="row" className="mono">
                  {role}
                </th>
                <td className="mono">{formatInt(breakdown.total)}</td>
                <td className="mono dim">{formatUsd(estimateCostUsd(breakdown))}</td>
              </tr>
            ))}
            <tr>
              <th scope="row" className="mono">
                planner
              </th>
              <td className="mono">{formatInt(usage.planner.total)}</td>
              <td className="mono dim">{formatUsd(estimateCostUsd(usage.planner))}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  )
}
