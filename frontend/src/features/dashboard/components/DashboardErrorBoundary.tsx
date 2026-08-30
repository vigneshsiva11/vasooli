import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertCircle } from 'lucide-react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class DashboardErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null
  };

  public static getDerivedStateFromError(error: Error): State {
    // Update state so the next render will show the fallback UI.
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('Dashboard Error:', error, errorInfo);
  }

  public render() {
    if (this.state.hasError) {
      return (
        <div className="bg-red-50/50 border border-red-100 rounded-xl p-8 flex flex-col items-center justify-center text-center max-w-2xl mx-auto mt-12">
          <AlertCircle className="h-10 w-10 text-[var(--color-status-blocked)] mb-4" />
          <h2 className="text-xl font-semibold text-gray-900 mb-2">Unable to load dashboard</h2>
          <p className="text-[var(--color-text-muted)] mb-6 max-w-md">
            We couldn't reach the backend to load your metrics. Please check your connection or try again.
          </p>
          <div className="bg-white p-3 rounded-md border border-gray-200 text-xs font-mono text-gray-500 w-full text-left overflow-x-auto">
            {this.state.error?.message || 'Unknown error occurred'}
          </div>
          <button
            className="mt-6 px-4 py-2 bg-[var(--color-primary)] text-white rounded-lg text-sm font-medium hover:bg-black transition-colors"
            onClick={() => this.setState({ hasError: false, error: null })}
          >
            Try Again
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
