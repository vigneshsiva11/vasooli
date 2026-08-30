import React, { Suspense } from 'react';
import { useBaselineQuery } from '../api/getBaseline';
import { ActualRecoveryPanel } from './ActualRecoveryPanel';
import { BaselineErrorBoundary } from './BaselineErrorBoundary';
import { BaselineSkeleton } from './BaselineSkeleton';
import { ComparisonChart } from './ComparisonChart';
import { CoverageNote } from './CoverageNote';
import { MultiplierCallout } from './MultiplierCallout';

const BaselineContent: React.FC = () => {
  const { data } = useBaselineQuery();
  const bestBaseline = Math.max(data.baseline_retry_everything.gross_expected_recovery, data.baseline_generic_reminder.gross_expected_recovery);
  const multiplier = bestBaseline > 0 ? data.vasooli_expected.gross_expected_recovery / bestBaseline : 0;

  return (
    <div className="space-y-10 pb-12">
      <header>
        <p className="text-xs font-bold uppercase tracking-[0.18em] text-[var(--color-accent)]">Strategy comparison</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-[var(--color-primary)]">Baseline Comparison</h1>
        <p className="mt-2 max-w-3xl text-sm text-[var(--color-text-muted)]">The same eligible events, scored with the same calibrated probabilities—only the recovery strategy changes.</p>
      </header>

      <section className="rounded-xl border border-gray-100 bg-white p-6 shadow-sm">
        <div className="flex flex-col gap-4 border-b border-gray-100 pb-5 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-[var(--color-status-risk)]" /><span className="text-xs font-bold uppercase tracking-[0.14em] text-[var(--color-text-muted)]">Simulated what-if projection</span></div>
            <h2 className="mt-2 text-xl font-semibold text-[var(--color-primary)]">Expected recovery by strategy</h2>
            <p className="mt-1 text-sm text-[var(--color-text-muted)]">Gross expected recovery; no money moved to produce these figures.</p>
          </div>
          <span className="w-fit rounded-full border border-[var(--color-status-risk)]/30 bg-[var(--color-status-risk)]/10 px-3 py-1 text-xs font-semibold text-[var(--color-primary)]">{data.event_basis.eligible_events.toLocaleString()} eligible events</span>
        </div>
        <div className="grid gap-6 pt-6 lg:grid-cols-[minmax(0,1fr)_220px] lg:items-center"><ComparisonChart data={data} /><MultiplierCallout multiplier={multiplier} /></div>
        <CoverageNote data={data} />
      </section>

      <ActualRecoveryPanel data={data.vasooli_actual} />

      <details className="rounded-xl border border-gray-100 bg-[var(--color-bg-surface)] px-5 py-4 text-sm text-[var(--color-text-muted)]">
        <summary className="cursor-pointer font-semibold text-[var(--color-primary)]">Methodology & event basis</summary>
        <p className="mt-3 leading-6">{data.methodology}</p>
        <p className="mt-3 text-xs">Computed {new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(data.computed_at))}{' · '}{data.event_basis.events_with_diagnosis.toLocaleString()} diagnosed of {data.event_basis.total_events.toLocaleString()} total events{' · '}{data.event_basis.excluded_non_recoverable.toLocaleString()} excluded as non-recoverable{' · '}{data.event_basis.excluded_undiagnosed.toLocaleString()} undiagnosed.</p>
      </details>
    </div>
  );
};

export const BaselineRoute: React.FC = () => <BaselineErrorBoundary><Suspense fallback={<BaselineSkeleton />}><BaselineContent /></Suspense></BaselineErrorBoundary>;
export default BaselineRoute;
