import { useEffect, useState, type ReactElement } from 'react'

export type Route = {
  path: string
  element: ReactElement
}

const ROOT = '/'

function readHash(): string {
  if (typeof window === 'undefined') return ROOT
  const hash = window.location.hash.replace(/^#\/?/, '')
  return hash || ROOT
}

function normalize(hash: string): string {
  return hash === '' || hash === '/' ? ROOT : hash
}

/**
 * Minimal hash router. Hash routing keeps the console working on any host and any
 * path (the preview proxy, a subdirectory reverse proxy, or file://), without
 * server-side fallback configuration.
 *
 * `useHash()` reads the current route. `navigate()` replaces the hash and notifies
 * listeners (replaceState does not fire `hashchange`).
 */

const hashListeners = new Set<() => void>()

export function useHash(): string {
  const [hash, setHash] = useState<string>(() => readHash())

  useEffect(() => {
    const onHashChange = (): void => setHash(readHash())
    window.addEventListener('hashchange', onHashChange)
    return (): void => window.removeEventListener('hashchange', onHashChange)
  }, [])

  return hash
}

export function navigate(to: string): void {
  if (typeof window === 'undefined') return
  const next = to.startsWith('/') ? `#${to}` : `#/${to}`
  window.history.replaceState(null, '', next)
  for (const listener of hashListeners) listener()
}

export function Router({ routes }: { routes: Route[] }): React.JSX.Element {
  const hash = useHash()
  const path = normalize(hash)
  const route = routes.find((r) => normalize(r.path) === path)
  if (!route) {
    return (
      <section className="panel panel-bad">
        <h2>Not found</h2>
        <p className="dim">There is no page at <code>#{hash}</code>.</p>
      </section>
    )
  }
  return route.element
}
