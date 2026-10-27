import { useCallback, useEffect, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

export interface ModalProps {
  open: boolean
  onClose: () => void
  title?: string
  children: ReactNode
}

let rootContainer: HTMLElement | null = null

function getContainer(): HTMLElement {
  if (!rootContainer) {
    rootContainer = document.createElement('div')
    rootContainer.setAttribute('data-modal-host', '')
    document.body.appendChild(rootContainer)
  }
  return rootContainer
}

export function Modal({
  open,
  onClose,
  title,
  children,
}: ModalProps): ReactNode {
  const handleKeyDown = useCallback(
    (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose()
    },
    [onClose],
  )

  useEffect(() => {
    if (open) {
      document.addEventListener('keydown', handleKeyDown)
      document.body.style.overflow = 'hidden'
    }
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = ''
    }
  }, [open, handleKeyDown])

  if (!open) return null

  return createPortal(
    <div className="modal-overlay" onClick={(event) => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={title ?? undefined}>
        {title && (
          <div className="modal-head">
            <h2 className="modal-title">{title}</h2>
            <button type="button" className="btn btn-sm" onClick={onClose} aria-label="Close">
              &times;
            </button>
          </div>
        )}
        <div className="modal-body">{children}</div>
      </div>
    </div>,
    getContainer(),
  )
}
