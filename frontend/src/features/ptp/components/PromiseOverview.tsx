import React from 'react';
import type { components } from '@/api/schema';

type PromiseMetrics = components['schemas']['PromiseMetrics'];
interface Props { metrics: PromiseMetrics; }

export const PromiseOverview: React.FC<Props> = ({ metrics }) => {
  const segments = [
    { label: 'Honored', value: metrics.honored, className: 'bg-[var(--color-status-recovered)]' },
    { label: 'Broken', value: metrics.broken, className: 'bg-[var(--color-status-blocked)]' },
    { label: 'Re-evaluating', value: metrics.reevaluating, className: 'bg-[var(--color-status-risk)]' },
    { label: 'Promised', value: metrics.promised, className: 'bg-[var(--color-primary)]/45' },
  ];
  return <section className="rounded-xl border border-gray-100 bg-white p-6 shadow-sm"><div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between"><div><p className="text-xs font-bold uppercase tracking-[0.14em] text-[var(--color-text-muted)]">Real promise outcomes</p><h2 className="mt-2 text-xl font-semibold text-[var(--color-primary)]">{metrics.total_promises} recorded commitments</h2><p className="mt-1 max-w-2xl text-sm text-[var(--color-text-muted)]">{metrics.methodology}</p></div><div className="rounded-lg bg-[var(--color-status-recovered)]/10 px-5 py-3 text-left md:text-right"><p className="text-xs font-bold uppercase tracking-wide text-[var(--color-status-recovered)]">Honor rate</p><p className="mt-1 text-4xl font-bold text-[var(--color-primary)]">{(metrics.honor_rate ?? 0).toFixed(1)}%</p><p className="text-xs text-[var(--color-text-muted)]">{metrics.honored} honored · {metrics.broken} broken</p></div></div><div className="mt-6 h-3 overflow-hidden rounded-full bg-[var(--color-bg-surface)]">{segments.map((segment) => <div key={segment.label} className={`inline-block h-full ${segment.className}`} style={{ width: `${metrics.total_promises ? (segment.value / metrics.total_promises) * 100 : 0}%` }} />)}</div><div className="mt-3 grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">{segments.map((segment) => <div key={segment.label} className="flex items-center gap-2"><span className={`h-2 w-2 rounded-full ${segment.className}`} /><span className="text-[var(--color-text-muted)]">{segment.label}</span><span className="font-bold text-[var(--color-primary)]">{segment.value}</span></div>)}</div></section>;
};
