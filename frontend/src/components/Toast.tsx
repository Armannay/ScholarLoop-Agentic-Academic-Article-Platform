import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

export type ToastVariant = 'ok' | 'bad' | 'warn' | 'info'

export interface Toast {
  id: string
  title?: string
  message?: string
  variant?: ToastVariant
  duration?: number
}

interface ToastContextValue {
  toasts: Toast[]
  toast: (toast: Omit<Toast, 'id'>) => string
  dismiss: (id: string) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)
let nextId = 0

export function ToastProvider({ children }: { children: ReactNode }): React.JSX.Element {
  const [toasts, setToasts] = useState<Toast[]>([])

  const toast = useCallback((item: Omit<Toast, 'id'>): string => {
    const id = `toast-${nextId += 1}`
    const entry: Toast = { ...item, id, duration: item.duration ?? 5000 }
    setToasts((current) => [...current, entry])
    return id
  }, [])

  const dismiss = useCallback((id: string): void => {
    setToasts((current) => current.filter((t) => t.id !== id))
  }, [])

  const value = useMemo<ToastContextValue>(() => ({ toasts, toast, dismiss }), [toasts, toast, dismiss])

  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) {
    throw new Error('useToast must be used inside <ToastProvider>')
  }
  return ctx
}

export function ToastList(): React.JSX.Element {
  const { toasts, dismiss } = useToast()

  useEffect(() => {
    const timers = new Map<string, ReturnType<typeof setTimeout>>()
    const schedule = (id: string, duration: number): void => {
      timers.set(id, setTimeout(() => dismiss(id), duration))
    }
    for (const t of toasts) {
      if (!timers.has(t.id)) schedule(t.id, t.duration ?? 5000)
    }
    for (const id of timers.keys()) {
      if (!toasts.some((t) => t.id === id)) {
        clearTimeout(timers.get(id))
        timers.delete(id)
      }
    }
    return () => {
      for (const timer of timers.values()) clearTimeout(timer)
    }
  }, [toasts, dismiss])

  return (
    <div className="toast-container" aria-label="Notifications">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast--${t.variant ?? 'info'}`}>
          <div>
            {t.title && <div className="toast-title">{t.title}</div>}
            {t.message && <div className="toast-message">{t.message}</div>}
          </div>
          <button type="button" className="btn btn-sm toast-dismiss" onClick={() => dismiss(t.id)}>
            &times;
          </button>
        </div>
      ))}
    </div>
  )
}
