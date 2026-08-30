import React from 'react';
import { RouteErrorPanel } from '@/components/RouteErrorPanel';

interface Props { children: React.ReactNode; }
interface State { hasError: boolean; error?: Error; }

export class BaselineErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false };
  static getDerivedStateFromError(error: Error): State { return { hasError: true, error }; }
  render() {
    if (this.state.hasError) return <RouteErrorPanel error={this.state.error} message="We couldn't reach the backend to load comparison metrics. Check the connection and try again." onRetry={() => this.setState({ hasError: false, error: undefined })} title="Unable to load baseline comparison" />;
    return this.props.children;
  }
}
