import { Component, type ReactNode } from 'react'

export interface ErrorBoundaryProps {
  children: ReactNode
  fallback?: (props: { error: Error; reset: () => void }) => ReactNode
}

export interface ErrorBoundaryState {
  error: Error | null
  info: string | null
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props)
    this.state = { error: null, info: null }
  }

  override componentDidCatch(error: Error, errorInfo: { componentStack?: string }): void {
    console.error('[ScholarLoop ErrorBoundary]', error, errorInfo.componentStack)
    this.setState({ error, info: errorInfo.componentStack ?? null })
  }

  reset = (): void => {
    this.setState({ error: null, info: null })
  }

  override render(): ReactNode {
    if (this.state.error) {
      const Fallback = this.props.fallback
      if (Fallback) {
        return Fallback({ error: this.state.error, reset: this.reset })
      }
      return (
        <section className="panel panel-bad error-boundary">
          <h2>Something went wrong</h2>
          <p className="mono bad">{this.state.error.message}</p>
          <button type="button" className="btn btn-primary" onClick={this.reset}>
            Try again
          </button>
        </section>
      )
    }
    return this.props.children
  }
}
