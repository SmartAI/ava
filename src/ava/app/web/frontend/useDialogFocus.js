import { useEffect, useRef } from 'react'

export function useDialogFocus(active = true) {
  const ref = useRef(null)
  useEffect(() => {
    const dialog = ref.current
    if (!active || !dialog) return
    const previous = document.activeElement
    dialog.focus()
    const trap = event => {
      if (event.key !== 'Tab') return
      const fields = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')]
        .filter(element => element.getClientRects().length)
      const first = fields[0]
      const last = fields.at(-1)
      if (!first) {
        event.preventDefault()
      } else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
        event.preventDefault()
        first.focus()
      }
    }
    dialog.addEventListener('keydown', trap)
    return () => {
      dialog.removeEventListener('keydown', trap)
      if (previous?.isConnected) previous.focus()
    }
  }, [active])
  return ref
}
