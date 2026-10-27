import type { ReactNode } from 'react'

export type BadgeVariant = 'ok' | 'warn' | 'bad' | 'idle' | 'neutral'

export interface BadgeProps {
  children: ReactNode
  variant?: BadgeVariant
  className?: string
}

const VARIANT_CLASS: Record<BadgeVariant, string> = {
  ok: 'badge badge--ok',
  warn: 'badge badge--warn',
  bad: 'badge badge--bad',
  idle: 'badge badge--idle',
  neutral: 'badge',
}

export function Badge({ children, variant = 'neutral', className }: BadgeProps): React.JSX.Element {
  return (
    <span className={`${VARIANT_CLASS[variant]}${className ? ` ${className}` : ''}`}>
      {children}
    </span>
  )
}
