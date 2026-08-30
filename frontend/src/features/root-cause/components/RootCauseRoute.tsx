import React, { Suspense } from 'react';
import { motion } from 'framer-motion';
import { useRootCauseQuery } from '../api/getRootCause';
import { useInterventionQuery } from '../api/getIntervention';
import { RootCauseSection } from './RootCauseSection';
import { InterventionFunnelSection } from './InterventionFunnelSection';
import { RootCauseErrorBoundary } from './RootCauseErrorBoundary';
import { RootCauseSkeleton } from './RootCauseSkeleton';

const RootCauseContent: React.FC = () => {
  const { data: rootCauseData } = useRootCauseQuery();
  const { data: interventionData } = useInterventionQuery();

  return (
    <motion.div animate={{ opacity: 1, y: 0 }} className="space-y-10 pb-12" initial={{ opacity: 0, y: 8 }} transition={{ duration: 0.22 }}>
      <div>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-[var(--color-primary)]">Root Cause & Intervention</h1>
        <p className="mt-2 max-w-3xl text-sm text-[var(--color-text-muted)]">
          Understanding why revenue leaks occur, and tracking the complete funnel of interventions from recommendation through to final recovery.
        </p>
      </div>
      
      <div className="space-y-12">
        <RootCauseSection data={rootCauseData} />
        <InterventionFunnelSection data={interventionData} />
      </div>
    </motion.div>
  );
};

export const RootCauseRoute: React.FC = () => {
  return (
    <RootCauseErrorBoundary>
      <Suspense fallback={<RootCauseSkeleton />}>
        <RootCauseContent />
      </Suspense>
    </RootCauseErrorBoundary>
  );
};

export default RootCauseRoute;
