import React from 'react';
import { RouteErrorPanel } from '@/components/RouteErrorPanel';

export class PromiseErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  state = { error: null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() { return this.state.error ? <RouteErrorPanel error={this.state.error} message="We couldn't reach the backend to load promise data. Check the connection and try again." onRetry={() => this.setState({ error: null })} title="Unable to load promise data" /> : this.props.children; }
}
