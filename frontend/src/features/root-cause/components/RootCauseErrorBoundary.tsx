import React from 'react';
import { RouteErrorPanel } from '@/components/RouteErrorPanel';

interface ErrorBoundaryProps {
  children: React.ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error?: Error;
}

export class RootCauseErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error("RootCause caught an error:", error, errorInfo);
  }

  render() {
    if (this.state.hasError) {
      return <RouteErrorPanel error={this.state.error} message="We couldn't reach the backend to load root-cause and intervention metrics. Check the connection and try again." onRetry={() => this.setState({ hasError: false, error: undefined })} title="Unable to load root-cause data" />;
    }

    return this.props.children;
  }
}
