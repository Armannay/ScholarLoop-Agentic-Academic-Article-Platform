// useEventSource - SSE client with automatic reconnect.
//
// Browsers' EventSource cannot send custom headers, so it cannot carry the bearer
// token. This hook implements the same text/event-stream semantics over fetch:
// it sends Authorization/Last-Event-ID headers, replays from the last id, and
// reconnects with exponential backoff. Fatal responses (401, 404) stop the stream.

import { useCallback, useEffect, useRef, useState } from 'react'
import { getAccessToken, subscribeToken } from './client'

export interface SseEvent {
  type: string
  data: unknown
  id: string | undefined
  raw: string
}

export interface UseEventSourceOptions {
  headers?: Record<string, string>
  onMessage?: (event: SseEvent) => void
  onOpen?: () => void
  onError?: (error: unknown) => void
  /** Initial reconnect delay in ms; doubles on each failure, capped at maxBackoffMs. */
  backoffMs?: number
  maxBackoffMs?: number
  /** Close after the stream ends naturally (for ?once=true endpoints). Default false. */
  once?: boolean
}

export type ReadyState = 'connecting' | 'open' | 'closed'

export interface EventSourceStatus {
  readyState: ReadyState
  reconnects: number
  lastEventId: string | null
  /** Manually (re)connect. */
  connect: () => void
  /** Close and stop reconnecting. */
  disconnect: () => void
}

const DEFAULT_BACKOFF_MS = 1_000
const MAX_BACKOFF_MS = 30_000

function parseFrame(text: string): SseEvent | null {
  const lines = text.split('\n')
  let type = ''
  const dataChunks: string[] = []
  let id: string | undefined

  for (const line of lines) {
    if (line.startsWith(':')) continue // comment
    if (line.startsWith('event:')) type = line.slice(6).trimStart()
    else if (line.startsWith('data:')) dataChunks.push(line.slice(5).trimStart())
    else if (line.startsWith('id:')) id = line.slice(3).trimStart()
  }

  if (type === '' && dataChunks.length === 0 && !id) return null
  let data: unknown
  try {
    data = dataChunks.length > 0 ? JSON.parse(dataChunks.join('\n')) : undefined
  } catch {
    data = dataChunks.join('\n')
  }
  return { type, data, id, raw: text }
}

export function useEventSource(
  url: string | null,
  options: UseEventSourceOptions = {},
): EventSourceStatus {
  const { backoffMs = DEFAULT_BACKOFF_MS, maxBackoffMs = MAX_BACKOFF_MS, once = false } = options
  const cbRef = useRef(options)
  cbRef.current = options

  const [readyState, setReadyState] = useState<ReadyState>('closed')
  const [reconnects, setReconnects] = useState(0)
  const [lastEventId, setLastEventId] = useState<string | null>(null)

  const controllerRef = useRef<AbortController | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const backoffRef = useRef(backoffMs)
  const closedRef = useRef(true)
  const connectingRef = useRef(false)

  const clearTimer = useCallback((): void => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const disconnect = useCallback((): void => {
    clearTimer()
    closedRef.current = true
    connectingRef.current = false
    controllerRef.current?.abort()
    controllerRef.current = null
    setReadyState('closed')
  }, [clearTimer])

  // The connect/fail callbacks are kept in refs so they can reference each other
  // without recreating (and re-running) the effects on every render.
  const connectRef = useRef<(() => void) | null>(null)
  const failRef = useRef<((cause: unknown) => void) | null>(null)

  const fail = useCallback(
    (cause: unknown): void => {
      if (closedRef.current) return
      cbRef.current.onError?.(cause)
      setReconnects((n) => n + 1)
      const delay = Math.min(backoffRef.current, maxBackoffMs)
      backoffRef.current = Math.min(backoffRef.current * 2, maxBackoffMs)
      setReadyState('connecting')
      timerRef.current = setTimeout(() => connectRef.current?.(), delay)
    },
    [maxBackoffMs],
  )
  failRef.current = fail

  const connect = useCallback((): void => {
    if (closedRef.current || connectingRef.current || !url) return
    connectingRef.current = true
    setReadyState('connecting')

    const abort = new AbortController()
    controllerRef.current = abort

    const headers = new Headers(cbRef.current.headers)
    headers.set('Accept', 'text/event-stream')
    const token = getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    if (lastEventId) headers.set('Last-Event-ID', lastEventId)

    void fetch(url, { headers, signal: abort.signal })
      .then(async (response) => {
        if (abort.signal.aborted) return
        if (!response.ok) {
          const fatal = response.status === 401 || response.status === 404
          if (fatal) {
            setReadyState('closed')
            connectingRef.current = false
            controllerRef.current = null
            cbRef.current.onError?.(new Error(`SSE stream failed with ${response.status}`))
            return
          }
          throw new Error(`SSE stream failed with ${response.status}`)
        }
        if (!response.body) {
          setReadyState('closed')
          connectingRef.current = false
          controllerRef.current = null
          if (!once) cbRef.current.onError?.(new Error('SSE stream had no body'))
          return
        }
        connectingRef.current = false
        backoffRef.current = backoffMs
        setReadyState('open')
        cbRef.current.onOpen?.()

        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        const pump = async (): Promise<void> => {
          try {
            while (!abort.signal.aborted && !closedRef.current) {
              const { value, done } = await reader.read()
              if (done) break
              buffer += decoder.decode(value, { stream: true })
              const parts = buffer.split('\n\n')
              buffer = parts.pop() ?? ''
              for (const part of parts) {
                if (abort.signal.aborted || closedRef.current) break
                const event = parseFrame(part)
                if (!event) continue
                if (event.id) setLastEventId(event.id)
                cbRef.current.onMessage?.(event)
              }
            }
          } catch {
            // Stream closed by the browser (e.g. navigation); fall through to end handling.
          }
          if (!abort.signal.aborted && !closedRef.current) {
            if (once) {
              setReadyState('closed')
              connectingRef.current = false
              controllerRef.current = null
            } else {
              failRef.current?.(new Error('SSE stream ended'))
            }
          } else {
            setReadyState('closed')
            connectingRef.current = false
            controllerRef.current = null
          }
        }
        void pump().catch(() => {
          // AbortError and similar are expected on unmount.
        })
      })
      .catch((cause) => {
        if (abort.signal.aborted) return
        setReadyState('connecting')
        connectingRef.current = false
        controllerRef.current = null
        failRef.current?.(cause)
      })
  }, [url, once, backoffMs, lastEventId])
  connectRef.current = connect

  const start = useCallback((): void => {
    if (!url) {
      disconnect()
      return
    }
    connectRef.current?.()
  }, [url, disconnect])

  // Connect on mount / URL change, disconnect on unmount.
  useEffect(() => {
    start()
    return () => {
      disconnect()
    }
  }, [start, disconnect])

  // A new bearer token (new session) reconnects the stream so it is re-authorized.
  useEffect(() => {
    return subscribeToken((): void => {
      if (!closedRef.current && url) {
        disconnect()
        connectRef.current?.()
      }
    })
  }, [url, disconnect])

  return {
    readyState,
    reconnects,
    lastEventId,
    connect,
    disconnect,
  }
}
