// Presentation helpers shared by the run, article and project screens.

import type { RunStateName, SectionStatus } from './types'
import type { Tone } from './StatusPill'

export function formatInt(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return Math.round(value).toLocaleString('en-US')
}

export function formatCompact(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (Math.abs(value) >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(Math.round(value))
}

export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  if (value >= 100) return `$${value.toFixed(2)}`
  if (value >= 1) return `$${value.toFixed(3)}`
  return `$${value.toFixed(4)}`
}

export function formatPercent(part: number, whole: number): string {
  if (!Number.isFinite(part) || !Number.isFinite(whole) || whole <= 0) return '—'
  return `${Math.round((part / whole) * 100)}%`
}

export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z')
}

/** Human label for a run state; the wire value stays the machine name. */
export function runStateLabel(state: RunStateName | string | null | undefined): string {
  if (!state) return 'unknown'
  const labels: Record<string, string> = {
    INTAKE: 'Intake',
    PLANNING: 'Planning',
    EXECUTING: 'Executing',
    VALIDATING: 'Validating',
    DECIDING: 'Deciding',
    CHECKPOINT: 'Waiting for you',
    REPLANNING: 'Replanning',
    DONE: 'Done',
    STOPPED_EROSION: 'Stopped · erosion',
    STOPPED_BUDGET: 'Stopped · budget',
    STOPPED_ERRORS: 'Stopped · errors',
    STOPPED_USER: 'Stopped · by you',
  }
  return labels[state] ?? state
}

export function runStateTone(state: RunStateName | string | null | undefined): Tone {
  if (!state) return 'idle'
  if (state === 'DONE') return 'ok'
  if (state === 'CHECKPOINT') return 'warn'
  if (state.startsWith('STOPPED')) return 'bad'
  return 'idle'
}

export function sectionStatusTone(status: SectionStatus): Tone {
  if (status === 'final') return 'ok'
  if (status === 'reviewed') return 'warn'
  if (status === 'draft') return 'idle'
  return 'idle'
}

export function statusLabel(status: SectionStatus): string {
  return status === 'empty' ? 'empty' : status
}

/** `text-align` for the article body; RTL languages mirror the whole block. */
export function readingDirection(language: string | null | undefined): 'rtl' | 'ltr' {
  if (!language) return 'ltr'
  const primary = language.trim().toLowerCase().split(/[-_]/)[0]
  return primary === 'fa' || primary === 'ar' ? 'rtl' : 'ltr'
}

export function gateLabel(gate: string): string {
  return gate.replace(/_/g, ' ')
}

export function shortId(id: string | null | undefined): string {
  if (!id) return '—'
  return id.length > 12 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id
}
