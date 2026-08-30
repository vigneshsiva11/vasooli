import { Component, type ErrorInfo, type ReactNode } from 'react';
import { RouteErrorPanel } from '@/components/RouteErrorPanel';

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
      return <RouteErrorPanel error={this.state.error} message="We couldn't reach the backend to load recovery metrics. Check the connection and try again." onRetry={() => this.setState({ hasError: false, error: null })} title="Unable to load dashboard" />;
    }

    return this.props.children;
  }
}
