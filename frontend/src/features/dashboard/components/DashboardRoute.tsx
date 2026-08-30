import React, { Suspense } from 'react';
import { motion } from 'framer-motion';
import { ShieldAlert, RefreshCw, BarChart3, AlertTriangle } from 'lucide-react';
import { useSummaryQuery } from '../api/getSummary';
import { AnimatedNumber } from './AnimatedNumber';
import { HeadlineMetric } from './HeadlineMetric';
import { StatusBreakdown } from './StatusBreakdown';
import { DashboardSkeleton } from './DashboardSkeleton';
import { DashboardErrorBoundary } from './DashboardErrorBoundary';

const formatCurrency = (value: number) => {
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
};

const formatPercent = (value: number | null) => {
  if (value === null) return 'N/A';
  return `${value.toFixed(1)}%`;
};

const DashboardContent: React.FC = () => {
  const { data } = useSummaryQuery();

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-3xl font-bold tracking-tight text-[var(--color-primary)]">Recovery Command Center</h1>
        <p className="text-sm text-[var(--color-text-muted)] mt-1">
          Real-time overview of system health and recovery metrics.
        </p>
      </div>

      <motion.div 
        className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6"
        initial="hidden"
        animate="show"
        variants={{
          hidden: {},
          show: {
            transition: { staggerChildren: 0.1 }
          }
        }}
      >
        <HeadlineMetric
          title="Revenue at Risk"
          icon={ShieldAlert}
          value={<AnimatedNumber value={data.total_revenue_at_risk} formatFn={formatCurrency} />}
        />

        <HeadlineMetric
          title="Revenue Recovered"
          icon={RefreshCw}
          value={<AnimatedNumber value={data.total_revenue_recovered} formatFn={formatCurrency} />}
        >
          <div className="flex flex-col gap-1 text-xs">
            <div className="flex justify-between items-center text-[var(--color-status-recovered)] font-medium">
              <span>Gateway Verified</span>
              <span>{formatCurrency(data.gateway_verified_recovered)}</span>
            </div>
            <div className="flex justify-between items-center text-[var(--color-text-muted)]">
              <span>Manually Asserted</span>
              <span>{formatCurrency(data.manually_asserted_recovered)}</span>
            </div>
          </div>
        </HeadlineMetric>

        <HeadlineMetric
          title="Recovery Rate"
          icon={BarChart3}
          value={<AnimatedNumber value={data.recovery_rate ?? 0} formatFn={() => formatPercent(data.recovery_rate)} />}
        >
           <div className="flex justify-between items-center text-xs">
              <span className="text-[var(--color-text-muted)]">Gateway Verified Rate</span>
              <span className="font-medium text-[var(--color-status-recovered)]">{formatPercent(data.recovery_rate_gateway_verified)}</span>
           </div>
        </HeadlineMetric>

        <HeadlineMetric
          title="Events by Status"
          icon={AlertTriangle}
          value={<AnimatedNumber value={data.total_events} formatFn={(v) => Math.round(v).toLocaleString()} />}
          subtitle="Total Events"
        >
          <StatusBreakdown data={data.events_by_status} total={data.total_events} />
        </HeadlineMetric>
      </motion.div>

      <motion.div 
        className="grid grid-cols-1 md:grid-cols-3 gap-6"
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.4, type: 'spring', stiffness: 300, damping: 24 }}
      >
        <div className="bg-[var(--color-bg-surface)] rounded-xl border border-gray-100 p-5 flex flex-col justify-between h-24">
          <span className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide">Events Processed</span>
          <span className="text-2xl font-semibold text-[var(--color-primary)]">
            <AnimatedNumber value={data.total_events_processed} formatFn={(v) => Math.round(v).toLocaleString()} />
          </span>
        </div>

        <div className="bg-[var(--color-bg-surface)] rounded-xl border border-gray-100 p-5 flex flex-col justify-between h-24">
          <span className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide flex justify-between">
            Non-Recoverable 
            <span className="normal-case text-[10px] text-gray-400 bg-gray-200/50 px-1.5 py-0.5 rounded">declined to chase</span>
          </span>
          <span className="text-2xl font-semibold text-[var(--color-primary)]">
            <AnimatedNumber value={data.non_recoverable_at_risk} formatFn={formatCurrency} />
          </span>
        </div>

        <div className="bg-[var(--color-bg-surface)] rounded-xl border border-gray-100 p-5 flex flex-col justify-between h-24">
          <span className="text-xs font-medium text-[var(--color-text-muted)] uppercase tracking-wide flex justify-between">
            Awaiting Decision
            <span className="normal-case text-[10px] text-gray-400 bg-gray-200/50 px-1.5 py-0.5 rounded">work not yet done</span>
          </span>
          <span className="text-2xl font-semibold text-[var(--color-primary)]">
            <AnimatedNumber value={data.events_without_decision} formatFn={(v) => Math.round(v).toLocaleString()} />
          </span>
        </div>
      </motion.div>
    </div>
  );
};

export const DashboardRoute: React.FC = () => {
  return (
    <DashboardErrorBoundary>
      <Suspense fallback={<DashboardSkeleton />}>
        <DashboardContent />
      </Suspense>
    </DashboardErrorBoundary>
  );
};

export default DashboardRoute;
