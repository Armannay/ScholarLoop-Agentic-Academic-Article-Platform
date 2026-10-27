import { useCallback, useEffect, useRef, useState } from 'react'
import { streamRunEvents } from './api'
import type { RunEvent } from './types'

export interface RunEventsState {
  events: RunEvent[]
  /** True while a stream is open (or about to reconnect). */
  live: boolean
  error: string | null
  /** Re-open the stream from the last seen `seq`. */
  reconnect: () => void
}

const RECONNECT_MS = 1_500
/** Empty replays tolerated before the feed gives up; a settled run produces no more. */
const MAX_IDLE_REPLAYS = 3

/**
 * Live change-log feed over Server-Sent Events.
 *
 * The stream replays from `after=<last seq>` and the backend closes it when the run
 * settles, so the hook reconnects while new events keep arriving and stops after a few
 * empty replays. Events are de-duplicated by `seq`, which is the resume cursor, so a
 * reconnect can never duplicate a row in the feed.
 */
export function useRunEvents(runId: string | null, enabled = true): RunEventsState {
  const [events, setEvents] = useState<RunEvent[]>([])
  const [live, setLive] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const cursorRef = useRef<number>(-1)
  const seenRef = useRef<Set<number>>(new Set())

  const reconnect = useCallback(() => {
    setError(null)
    setNonce((value) => value + 1)
  }, [])

  // A different run is a different log: drop the cursor and the seen set.
  useEffect(() => {
    seenRef.current = new Set()
    cursorRef.current = -1
    setEvents([])
    setError(null)
  }, [runId])

  useEffect(() => {
    if (!runId || !enabled) {
      setLive(false)
      return
    }
    let cancelled = false
    const controller = new AbortController()
    let idleReplays = 0

    const push = (event: RunEvent): void => {
      if (seenRef.current.has(event.seq)) return
      seenRef.current.add(event.seq)
      cursorRef.current = Math.max(cursorRef.current, event.seq)
      idleReplays = 0
      setEvents((previous) => [...previous, event].sort((a, b) => a.seq - b.seq))
    }

    const run = async (): Promise<void> => {
      setLive(true)
      while (!cancelled && idleReplays < MAX_IDLE_REPLAYS) {
        try {
          await streamRunEvents(runId, push, {
            after: cursorRef.current,
            signal: controller.signal,
          })
          setError(null)
        } catch (cause) {
          if (cancelled) return
          if (controller.signal.aborted) return
          setError(cause instanceof Error ? cause.message : 'event stream failed')
          break
        }
        if (cancelled) return
        idleReplays += 1
        if (idleReplays >= MAX_IDLE_REPLAYS) break
        await new Promise((resolve) => setTimeout(resolve, RECONNECT_MS))
      }
      if (!cancelled) setLive(false)
    }

    void run()
    return () => {
      cancelled = true
      controller.abort()
      setLive(false)
    }
  }, [runId, enabled, nonce])

  return { events, live, error, reconnect }
}
