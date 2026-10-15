import { useCallback, useEffect, useState } from 'react'
import { fetchContractSchema } from './api'

/**
 * Show one contract's JSON Schema.
 *
 * Schemas are the machine-readable half of CONTRACTS v1; being able to open one while
 * reading the policies is what makes the console useful during development. The view
 * truncates long documents: it is an inspector, not an editor.
 */
export function SchemaViewer({ name }: { name: string }): React.JSX.Element {
  const [pretty, setPretty] = useState<string>('')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const schema = await fetchContractSchema(name, signal)
        setPretty(JSON.stringify(schema, null, 2))
        setError(null)
      } catch (cause) {
        if (signal.aborted) return
        setError(cause instanceof Error ? cause.message : 'request failed')
      }
    },
    [name],
  )

  useEffect(() => {
    const controller = new AbortController()
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  if (error) return <p className="mono bad">{error}</p>
  if (!pretty) return <p className="dim">loading {name}…</p>

  const lines = pretty.split('\n')
  const shown = lines.slice(0, 120).join('\n')
  return (
    <div className="schema">
      <div className="schema-head">
        <span className="mono">{name}.json</span>
        <a className="mono" href={`/api/contracts/${name}/schema`} target="_blank" rel="noreferrer">
          open raw
        </a>
      </div>
      <pre className="mono schema-body">{shown}</pre>
      {lines.length > 120 && <p className="dim">… {lines.length - 120} more lines</p>}
    </div>
  )
}
