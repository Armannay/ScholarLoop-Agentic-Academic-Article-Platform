import { useEffect, useState, type ReactElement } from 'react'

export type RouteParams = Record<string, string>

export interface Route {
  path: string
  /**
   * The screen. Either an element (static route) or a function of the path params, so a
   * route like `/runs/:runId` can hand the id to the page without a second read of the
   * hash.
   */
  element: ReactElement | ((params: RouteParams) => ReactElement)
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

function segments(path: string): string[] {
  return normalize(path).split('/').filter((part) => part !== '')
}

/**
 * Match a route pattern against a path. Segments that start with `:` capture a value
 * (URL-decoded), everything else must be equal.
 */
export function matchPath(pattern: string, path: string): RouteParams | null {
  const want = segments(pattern)
  const got = segments(path)
  if (want.length !== got.length) return null
  const params: RouteParams = {}
  for (let index = 0; index < want.length; index += 1) {
    const expected = want[index] as string
    const actual = got[index] as string
    if (expected.startsWith(':')) {
      params[expected.slice(1)] = decodeURIComponent(actual)
    } else if (expected !== actual) {
      return null
    }
  }
  return params
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
    const listener = (): void => setHash(readHash())
    hashListeners.add(listener)
    return (): void => {
      window.removeEventListener('hashchange', onHashChange)
      hashListeners.delete(listener)
    }
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

  for (const route of routes) {
    const params = matchPath(route.path, path)
    if (!params) continue
    return typeof route.element === 'function' ? route.element(params) : route.element
  }

  return (
    <section className="panel panel-bad">
      <h2>Not found</h2>
      <p className="dim">
        There is no page at <code>#{hash}</code>.
      </p>
    </section>
  )
}
