// App-wide render-error safety net. Wraps <Routes> in App.tsx so an
// unhandled error thrown while rendering any page degrades to a recoverable
// "Something went wrong" card instead of a blank white screen.
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('MeshAI dashboard render error:', error, errorInfo)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex items-center justify-center h-64">
          <div className="bg-bg-card border border-red-500/20 rounded p-6 max-w-md w-full text-center space-y-3">
            <AlertTriangle className="mx-auto text-red-400" size={28} />
            <div className="text-slate-200 font-medium">Something went wrong</div>
            <div className="text-xs text-slate-500">
              {this.state.error?.message ?? 'An unexpected error occurred while rendering this page.'}
            </div>
            <button
              onClick={() => window.location.reload()}
              className="px-4 py-2 bg-accent hover:bg-accent/80 rounded text-white text-sm transition-colors"
            >
              Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
