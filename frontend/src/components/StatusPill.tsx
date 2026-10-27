import { Badge, type BadgeVariant } from './Badge'

/**
 * A status pill: the shell's `Badge` with an always-visible dot and an optional label.
 *
 * Kept as its own component because the run/article screens use it as a *state*
 * indicator (`run state`, `section status`, `stream state`) rather than as a tag, and
 * a dot makes "which of these is the state" unambiguous at a glance.
 */
export function StatusPill({
  tone,
  label,
}: {
  tone: BadgeVariant
  label?: string | undefined
}): React.JSX.Element {
  return (
    <Badge variant={tone}>
      <span className="status-dot" aria-hidden="true" />
      {label ?? tone}
    </Badge>
  )
}
