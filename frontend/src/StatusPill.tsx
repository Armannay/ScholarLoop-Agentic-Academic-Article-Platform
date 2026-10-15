export type Tone = 'ok' | 'warn' | 'bad' | 'idle'

const LABELS: Record<Tone, string> = {
  ok: 'ok',
  warn: 'attention',
  bad: 'error',
  idle: 'idle',
}

export function StatusPill({ tone, label }: { tone: Tone; label?: string }): React.JSX.Element {
  const text = label === undefined ? LABELS[tone] : label
  return (
    <span className={`pill pill-${tone}`} role="status">
      <span className="dot" aria-hidden="true" />
      {text || LABELS[tone]}
    </span>
  )
}
