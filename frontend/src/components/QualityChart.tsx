import { GATE_NAMES, type QualityView } from '../lib/domain'
import { formatInt, gateLabel } from '../lib/format'

export interface QualityChartProps {
  quality: QualityView | null
  qMin?: number | undefined
  /** Iteration the run is on, highlighted on the chart. */
  currentIter?: number | undefined
}

const WIDTH = 460
const HEIGHT = 160
const PAD_X = 28
const PAD_Y = 18

function pointsFor(series: readonly { q: number }[]): string {
  if (series.length === 0) return ''
  const maxIndex = Math.max(1, series.length - 1)
  return series
    .map((point, index) => {
      const x = PAD_X + (index / maxIndex) * (WIDTH - PAD_X * 2)
      const y = PAD_Y + (1 - point.q / 100) * (HEIGHT - PAD_Y * 2)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

/**
 * Q per iteration, with a chip per hard gate for the latest report.
 *
 * Drawn as an SVG polyline (no chart library, no network) and mirrored by a real
 * `<table>` underneath, so the numbers are available to a screen reader and to tests
 * that never look at pixels.
 */
export function QualityChart({ quality, qMin = 70, currentIter }: QualityChartProps): React.JSX.Element {
  if (!quality || quality.series.length === 0) {
    return (
      <div className="empty-state" role="status">
        No quality measurements yet — the validator scores the article at the end of each iteration.
      </div>
    )
  }
  const { series, latest } = quality
  const polyline = pointsFor(series)
  const last = series[series.length - 1]
  const best = Math.max(...series.map((point) => point.q))
  const yFor = (q: number): number => PAD_Y + (1 - q / 100) * (HEIGHT - PAD_Y * 2)

  return (
    <div className="stack-sm">
      <div className="chart-wrap">
        <svg
          className="q-chart"
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          role="img"
          aria-label={`Quality score per iteration: ${series.map((p) => `iteration ${p.iter} scored ${p.q}`).join(', ')}`}
        >
          <line x1={PAD_X} y1={yFor(100)} x2={WIDTH - PAD_X} y2={yFor(100)} className="chart-axis" />
          <line x1={PAD_X} y1={yFor(0)} x2={WIDTH - PAD_X} y2={yFor(0)} className="chart-axis" />
          <line
            x1={PAD_X}
            y1={yFor(qMin)}
            x2={WIDTH - PAD_X}
            y2={yFor(qMin)}
            className="chart-threshold"
          />
          <text x={PAD_X} y={yFor(qMin) - 4} className="chart-label">
            Q min {qMin}
          </text>
          <polyline points={polyline} className="chart-line" fill="none" />
          {series.map((point, index) => {
            const maxIndex = Math.max(1, series.length - 1)
            const x = PAD_X + (index / maxIndex) * (WIDTH - PAD_X * 2)
            const y = PAD_Y + (1 - point.q / 100) * (HEIGHT - PAD_Y * 2)
            const isCurrent = currentIter !== undefined && point.iter === currentIter
            return (
              <g key={point.iter}>
                <circle
                  cx={x}
                  cy={y}
                  r={isCurrent ? 5 : 3.5}
                  className={point.q >= qMin ? 'chart-dot chart-dot-ok' : 'chart-dot'}
                />
                <text x={x} y={HEIGHT - 4} className="chart-label" textAnchor="middle">
                  {point.iter}
                </text>
              </g>
            )
          })}
        </svg>
        <dl className="chart-stats">
          <div>
            <dt>Current</dt>
            <dd className="mono">{last ? formatInt(last.q) : '—'}</dd>
          </div>
          <div>
            <dt>Best</dt>
            <dd className="mono">{formatInt(best)}</dd>
          </div>
          <div>
            <dt>Iterations</dt>
            <dd className="mono">{series.length}</dd>
          </div>
        </dl>
      </div>

      <table className="table sr-table">
        <caption className="dim small">Quality per iteration</caption>
        <thead>
          <tr>
            <th scope="col">Iteration</th>
            <th scope="col">Q</th>
            <th scope="col">Gates</th>
            <th scope="col">Cited sources verified</th>
          </tr>
        </thead>
        <tbody>
          {series.map((point) => (
            <tr key={point.iter}>
              <th scope="row">{point.iter}</th>
              <td className="mono">{point.q}</td>
              <td>{point.gates_passed ? 'all pass' : 'failing'}</td>
              <td className="mono">
                {point.verified_citation_ratio === undefined
                  ? '—'
                  : `${Math.round(point.verified_citation_ratio * 100)}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="gate-chips" aria-label="Hard gates for the latest iteration">
        {GATE_NAMES.map((gate) => {
          const passed = latest?.gates?.[gate] === true
          const known = latest?.gates ? gate in latest.gates : false
          return (
            <span
              key={gate}
              className={known ? (passed ? 'gate-chip gate-chip-ok' : 'gate-chip gate-chip-bad') : 'gate-chip gate-chip-idle'}
              title={known ? (passed ? 'gate passed' : 'gate failed') : 'gate not reported yet'}
            >
              <span aria-hidden="true">{known ? (passed ? '✓' : '✕') : '·'}</span>
              <span className="mono">{gateLabel(gate)}</span>
            </span>
          )
        })}
      </div>
    </div>
  )
}
