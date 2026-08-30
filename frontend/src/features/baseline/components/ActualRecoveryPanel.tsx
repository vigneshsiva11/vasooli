import React from 'react';
import { motion } from 'framer-motion';
import type { components } from '@/api/schema';
import { formatCurrency } from '@/utils/formatters';

type VasooliActual = components['schemas']['VasooliActual'];
interface Props { data: VasooliActual; }

export const ActualRecoveryPanel: React.FC<Props> = ({ data }) => (
  <motion.section initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, delay: 0.2 }} className="rounded-xl border border-[var(--color-status-recovered)]/25 bg-white p-6 shadow-sm">
    <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between"><div><div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-[var(--color-status-recovered)]" /><span className="text-xs font-bold uppercase tracking-[0.14em] text-[var(--color-status-recovered)]">Real verified outcome</span></div><h2 className="mt-2 text-xl font-semibold text-[var(--color-primary)]">What has actually come back</h2><p className="mt-1 max-w-2xl text-sm leading-6 text-[var(--color-text-muted)]">The projection above scores every eligible event. This is the money actually executed and confirmed during development—shown separately so it is never presented as comparable to a what-if estimate.</p></div><div className="rounded-lg bg-[var(--color-status-recovered)]/10 px-4 py-3 text-left md:text-right"><p className="text-xs font-semibold uppercase tracking-wide text-[var(--color-text-muted)]">Total recovered</p><p className="mt-1 text-3xl font-bold text-[var(--color-status-recovered)]">{formatCurrency(data.revenue_recovered)}</p></div></div>
    <div className="mt-6 grid gap-3 sm:grid-cols-2"><div className="rounded-lg border border-[var(--color-status-recovered)]/20 bg-[var(--color-status-recovered)]/10 p-4"><p className="text-xs font-bold uppercase tracking-wide text-[var(--color-status-recovered)]">Gateway verified</p><p className="mt-1 text-2xl font-bold text-[var(--color-primary)]">{formatCurrency(data.revenue_recovered_gateway_verified)}</p><p className="mt-1 text-xs text-[var(--color-text-muted)]">Razorpay webhook confirmed</p></div><div className="rounded-lg border border-gray-200 bg-[var(--color-bg-surface)] p-4"><p className="text-xs font-bold uppercase tracking-wide text-[var(--color-text-muted)]">Manually asserted</p><p className="mt-1 text-2xl font-bold text-[var(--color-primary)]">{formatCurrency(data.revenue_recovered_manually_asserted)}</p><p className="mt-1 text-xs text-[var(--color-text-muted)]">Merchant-confirmed; no gateway proof</p></div></div>
  </motion.section>
);
