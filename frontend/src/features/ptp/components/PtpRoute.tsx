import React, { Suspense } from 'react';
import { motion } from 'framer-motion';
import { usePromiseExtractionsQuery, usePtpMetricsQuery, usePromisesQuery } from '../api/getPtpMetrics';
import { ExtractionDemo } from './ExtractionDemo';
import { PromiseErrorBoundary } from './PromiseErrorBoundary';
import { PromiseList } from './PromiseList';
import { PromiseOverview } from './PromiseOverview';
import { PromiseSkeleton } from './PromiseSkeleton';

const PromiseContent: React.FC = () => {
  const { data: metrics } = usePtpMetricsQuery();
  const { data: promises } = usePromisesQuery();
  const { data: extractions } = usePromiseExtractionsQuery();
  return <motion.div animate={{ opacity: 1, y: 0 }} className="space-y-10 pb-12" initial={{ opacity: 0, y: 8 }} transition={{ duration: 0.22 }}><header><p className="text-xs font-bold uppercase tracking-[0.18em] text-[var(--color-accent)]">Commitment intelligence</p><h1 className="mt-2 text-3xl font-bold tracking-tight text-[var(--color-primary)]">Promise to Pay</h1><p className="mt-2 max-w-3xl text-sm text-[var(--color-text-muted)]">Track customer commitments and turn defensible free-text promises into structured, auditable records.</p></header><PromiseOverview metrics={metrics} /><ExtractionDemo extractions={extractions} /><PromiseList promises={promises} /></motion.div>;
};

export const PtpRoute: React.FC = () => <PromiseErrorBoundary><Suspense fallback={<PromiseSkeleton />}><PromiseContent /></Suspense></PromiseErrorBoundary>;
export default PtpRoute;
