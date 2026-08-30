import type { ReactNode } from 'react';

interface RouteErrorPanelProps {
  title: string;
  message: string;
  error?: Error | null;
  onRetry: () => void;
  children?: ReactNode;
}

export function RouteErrorPanel({ title, message, error, onRetry, children }: RouteErrorPanelProps) {
  return (
    <div className="mx-auto my-8 flex max-w-2xl flex-col items-center rounded-xl border border-[var(--color-status-blocked)]/20 bg-[var(--color-status-blocked)]/10 p-6 text-center">
      <h2 className="text-lg font-semibold text-[var(--color-primary)]">{title}</h2>
      <p className="mt-2 max-w-md text-sm text-[var(--color-text-muted)]">{message}</p>
      {children}
      {error && <p className="mt-3 max-w-full overflow-x-auto rounded-md border border-gray-200 bg-white p-3 text-left font-mono text-xs text-[var(--color-text-muted)]">{error.message}</p>}
      <button className="mt-4 rounded-md border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-[var(--color-primary)] transition-colors hover:border-[var(--color-accent)]" onClick={onRetry} type="button">Try again</button>
    </div>
  );
}
