import { useCallback, useEffect, useRef, useState } from 'react'
import { useEventSource } from '../api/useEventSource'
import { runEventsUrl } from './api'
import type { RunEvent } from './domain'

export interface RunFeed {
  events: RunEvent[]
  /** True while the stream is open or reconnecting. */
  live: boolean
  error: string | null
  reconnect: () => void
}

/**
 * The live change log of one run, over Server-Sent Events.
 *
 * Built on the shell's `useEventSource`, which sends the bearer token as a header (the
 * browser's `EventSource` cannot), replays with `Last-Event-ID`, and reconnects with
 * exponential backoff. This hook adds the two things a *log* needs: de-duplication by
 * `seq` (a reconnect replays from the last id, so duplicates are a certainty, not a
 * possibility) and ordering, so the feed reads oldest-to-newest regardless of how many
 * times the connection was re-established.
 */
export function useRunFeed(runId: string | null, enabled = true): RunFeed {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [error, setError] = useState<string | null>(null)
  const seen = useRef<Set<number>>(new Set())

  useEffect(() => {
    seen.current = new Set()
    setEvents([])
    setError(null)
  }, [runId])

  const handleMessage = useCallback((message: { data: unknown }) => {
    const payload = message.data
    if (!payload || typeof payload !== 'object') return
    const event = payload as RunEvent
    if (typeof event.seq !== 'number' || typeof event.type !== 'string') return
    if (seen.current.has(event.seq)) return
    seen.current.add(event.seq)
    setEvents((previous) => [...previous, event].sort((left, right) => left.seq - right.seq))
  }, [])

  const status = useEventSource(runId && enabled ? runEventsUrl(runId) : null, {
    onMessage: handleMessage,
    onError: (cause) => {
      setError(cause instanceof Error ? cause.message : 'event stream failed')
    },
    onOpen: () => setError(null),
    // A settled run stops producing events; back off slowly instead of hammering.
    backoffMs: 2_000,
    maxBackoffMs: 30_000,
  })

  const reconnect = useCallback(() => {
    setError(null)
    status.disconnect()
    status.connect()
  }, [status])

  return {
    events,
    live: status.readyState === 'open' || status.readyState === 'connecting',
    error,
    reconnect,
  }
}
