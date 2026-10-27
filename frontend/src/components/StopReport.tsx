import { formatInt, formatTimestamp, formatUsd, gateLabel, runStateLabel } from '../lib/format'
import { estimateCostUsd } from '../lib/costs'
import type { QualityView, RunUsageView, RunView } from '../lib/domain'

export interface StopReportProps {
  run: RunView
  quality?: QualityView | null
  usage?: RunUsageView | null
  onOpenCheckpoint?: (() => void) | undefined
}

function evidenceRows(evidence: Record<string, unknown>): Array<[string, string]> {
  const rows: Array<[string, string]> = []
  for (const [key, value] of Object.entries(evidence)) {
    if (value === null || value === undefined) continue
    if (Array.isArray(value)) {
      rows.push([key, value.map((item) => String(item)).join(', ') || '—'])
    } else if (typeof value === 'object') {
      const nested = Object.entries(value as Record<string, unknown>)
        .map(([k, v]) => `${k}=${String(v)}`)
        .join(' ')
      rows.push([key, nested])
    } else {
      rows.push([key, String(value)])
    }
  }
  return rows
}

/**
 * Why the loop stopped, with the evidence behind the decision.
 *
 * Everything here comes from the run's `stop_report` (or, at a checkpoint, from the
 * same report plus the latest quality view): reasons, Q series, best snapshot, token
 * spend, the verdict's risks and the failed gates. No number is recomputed from a
 * guess except the USD estimate, which is labelled as such.
 */
export function StopReport({ run, quality, usage, onOpenCheckpoint }: StopReportProps): React.JSX.Element {
  const report = run.stop_report
  if (!report) {
    return (
      <div className="empty-state" role="status">
        No stop report yet. One is written whenever the loop pauses or ends, and it always
        names the reasons and the evidence.
      </div>
    )
  }
  const failed = quality?.latest ? Object.entries(quality.latest.gates).filter(([, passed]) => !passed) : []
  const risks = report.verdict?.risks ?? run.verdict?.risks ?? []
  const missing = report.verdict?.missing ?? run.verdict?.missing ?? []

  return (
    <div className="stack-sm">
      <div className="stop-header">
        <div>
          <div className="field-label">Why the run {run.state === 'CHECKPOINT' ? 'paused' : 'stopped'}</div>
          <div className="strong">{runStateLabel(run.state)}</div>
        </div>
        <div className="mono small dim">{formatTimestamp(report.created_at)}</div>
      </div>

      <ul className="reason-list" aria-label="Decision reasons">
        {report.reasons.map((reason) => (
          <li key={reason} className="badge mono">
            {reason}
          </li>
        ))}
      </ul>

      <dl className="kv">
        <dt>Q current / best</dt>
        <dd className="mono">
          {formatInt(report.current_q)} / {formatInt(report.best_q)}
        </dd>
        <dt>Q series</dt>
        <dd className="mono">[{report.q_series.join(', ')}]</dd>
        <dt>Best iteration</dt>
        <dd className="mono">{report.best_iter ?? '—'}</dd>
        <dt>Best snapshot</dt>
        <dd className="mono small">{report.best_snapshot_id ?? 'none'}</dd>
        <dt>Iterations</dt>
        <dd className="mono">{report.iterations}</dd>
        <dt>Tokens</dt>
        <dd className="mono">
          {formatInt(report.tokens_spent)}
          {usage ? (
            <span className="dim">
              {' '}
              of {formatInt(usage.token_cap)} ({Math.round((usage.total / usage.token_cap) * 100)}%)
            </span>
          ) : null}
        </dd>
        <dt>Est. cost</dt>
        <dd className="mono">
          {usage ? formatUsd(estimateCostUsd(usage.agents_total) + estimateCostUsd(usage.planner)) : '—'}
        </dd>
        <dt>Options offered</dt>
        <dd className="mono small">{report.options.join(', ') || 'none'}</dd>
      </dl>

      {risks.length > 0 && (
        <div>
          <h4 className="field-label">Risks the Planner named</h4>
          <ul className="list">
            {risks.map((risk) => (
              <li key={risk}>{risk}</li>
            ))}
          </ul>
        </div>
      )}

      {missing.length > 0 && (
        <div>
          <h4 className="field-label">Still missing</h4>
          <ul className="list">
            {missing.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {failed.length > 0 && (
        <div>
          <h4 className="field-label">Failed hard gates</h4>
          <ul className="list mono small">
            {failed.map(([gate]) => (
              <li key={gate}>{gateLabel(gate)}</li>
            ))}
          </ul>
        </div>
      )}

      <details className="evidence">
        <summary>Evidence ({Object.keys(report.evidence).length} keys)</summary>
        {Object.keys(report.evidence).length === 0 ? (
          <p className="dim small">The decision carried no evidence payload.</p>
        ) : (
          <table className="table">
            <tbody>
              {evidenceRows(report.evidence).map(([key, value]) => (
                <tr key={key}>
                  <th scope="row" className="mono">
                    {key}
                  </th>
                  <td className="mono small">{value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </details>

      {onOpenCheckpoint && report.options.length > 0 && (
        <div className="btn-row">
          <button type="button" className="btn btn-primary" onClick={onOpenCheckpoint}>
            Answer the run…
          </button>
        </div>
      )}
    </div>
  )
}
