import React from 'react';
import type { components } from '@/api/schema';

type BaselineComparison = components['schemas']['BaselineComparison'];
interface Props { data: BaselineComparison; }

export const CoverageNote: React.FC<Props> = ({ data }) => {
  const baselines = [{ label: 'Retry everything', data: data.baseline_retry_everything }, { label: 'Generic reminder', data: data.baseline_generic_reminder }];
  return <div className="mt-5 border-t border-gray-100 pt-5"><p className="text-xs font-bold uppercase tracking-[0.14em] text-[var(--color-text-muted)]">Coverage context</p><p className="mt-1 text-sm text-[var(--color-text-muted)]">Each naive strategy was scored over all eligible events, but receives zero where its intervention family has no defined probability for that root-cause/surface pairing.</p><div className="mt-3 grid gap-3 md:grid-cols-2">{baselines.map(({ label, data: baseline }) => <div key={label} className="rounded-lg bg-[var(--color-bg-surface)] px-4 py-3 text-sm"><p className="font-semibold text-[var(--color-primary)]">{label}</p><p className="mt-1 text-[var(--color-text-muted)]">Defined strategy for <span className="font-semibold text-[var(--color-primary)]">{baseline.events_with_defined_probability}</span> of {baseline.events_scored} events; {baseline.events_scored_zero_no_defined_pairing} score zero with no defined pairing.</p></div>)}</div></div>;
};
