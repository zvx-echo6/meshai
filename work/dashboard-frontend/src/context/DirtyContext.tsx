// DirtyContext — tracks whether any config page has unsaved changes.
//
// Usage:
//   - Wrap the app (or BrowserRouter) with <DirtyProvider>.
//   - In any config page: import { useDirty } from '@/context/DirtyContext'
//     then call setDirty(hasChanges) in a useEffect, and setDirty(false) in
//     cleanup / on save.
//   - In Layout.tsx nav links: check dirty before navigation and confirm.

import { createContext, useContext, useState, useEffect, ReactNode } from 'react'

interface DirtyContextValue {
  dirty: boolean
  setDirty: (v: boolean) => void
}

const DirtyContext = createContext<DirtyContextValue>({
  dirty: false,
  setDirty: () => {},
})

export function DirtyProvider({ children }: { children: ReactNode }) {
  const [dirty, setDirty] = useState(false)

  // Warn on tab close / refresh while dirty.
  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault()
        e.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', handler)
    return () => window.removeEventListener('beforeunload', handler)
  }, [dirty])

  return (
    <DirtyContext.Provider value={{ dirty, setDirty }}>
      {children}
    </DirtyContext.Provider>
  )
}

export function useDirty() {
  return useContext(DirtyContext)
}
