import { useCallback, useEffect, useRef, useState } from 'react'

export interface AsyncState<T> {
  data: T | null
  error: string | null
  loading: boolean
  /** Epoch ms of the last successful load, or null. */
  updatedAt: number | null
  reload: () => void
}

/**
 * Load a resource once and keep it refreshable.
 *
 * `pollMs` re-fetches quietly (no loading flash) so the console can show a live
 * status without redrawing the whole page on every tick.
 */
export function useAsync<T>(loader: (signal?: AbortSignal) => Promise<T>, pollMs = 0): AsyncState<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  const [nonce, setNonce] = useState(0)

  // Keep the loader in a ref so the effect does not depend on an unstable closure.
  const loaderRef = useRef(loader)
  useEffect(() => {
    loaderRef.current = loader
  }, [loader])

  useEffect(() => {
    let cancelled = false
    const controller = new AbortController()

    const run = async (quiet: boolean): Promise<void> => {
      if (!quiet) setLoading(true)
      try {
        const result = await loaderRef.current(controller.signal)
        if (cancelled) return
        setData(result)
        setError(null)
        setUpdatedAt(Date.now())
      } catch (cause) {
        if (cancelled) return
        setError(cause instanceof Error ? cause.message : 'request failed')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void run(false)
    let timer: number | undefined
    if (pollMs > 0) {
      timer = window.setInterval(() => void run(true), pollMs)
    }

    return () => {
      cancelled = true
      controller.abort()
      if (timer !== undefined) window.clearInterval(timer)
    }
  }, [pollMs, nonce])

  const reload = useCallback(() => setNonce((value) => value + 1), [])
  return { data, error, loading, updatedAt, reload }
}
