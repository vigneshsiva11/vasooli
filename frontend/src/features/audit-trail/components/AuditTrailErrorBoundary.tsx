import { Component, type ReactNode } from 'react';
import { RouteErrorPanel } from '@/components/RouteErrorPanel';

export class AuditTrailErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('Audit Trail error:', error, errorInfo);
  }

  render() {
    if (this.state.error) {
      return <RouteErrorPanel error={this.state.error} message="We couldn't reach the backend to assemble this event trail. Check the connection and try again." onRetry={() => this.setState({ error: null })} title="Unable to load audit trail" />;
    }

    return this.props.children;
  }
}
