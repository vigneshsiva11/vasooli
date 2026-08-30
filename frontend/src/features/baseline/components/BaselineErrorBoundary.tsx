import React from 'react';

interface Props { children: React.ReactNode; }
interface State { hasError: boolean; }

export class BaselineErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false };
  static getDerivedStateFromError(): State { return { hasError: true }; }
  render() {
    if (this.state.hasError) return <div className="my-8 rounded-xl border border-[var(--color-status-blocked)]/20 bg-[var(--color-status-blocked)]/10 p-6 text-center"><h2 className="text-lg font-semibold text-[var(--color-primary)]">Unable to load baseline comparison</h2><p className="mt-2 text-sm text-[var(--color-text-muted)]">The backend comparison metrics could not be displayed.</p><button onClick={() => this.setState({ hasError: false })} className="mt-4 rounded-md border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-[var(--color-primary)]">Try again</button></div>;
    return this.props.children;
  }
}
