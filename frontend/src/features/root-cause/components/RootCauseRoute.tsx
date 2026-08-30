import React, { Suspense } from 'react';
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
    <div className="space-y-10 pb-12">
      <div>
        <h1 className="text-3xl font-bold tracking-tight text-[var(--color-primary)]">Root Cause & Intervention</h1>
        <p className="text-sm text-[var(--color-text-muted)] mt-2 max-w-3xl">
          Understanding why revenue leaks occur, and tracking the complete funnel of interventions from recommendation through to final recovery.
        </p>
      </div>
      
      <div className="space-y-12">
        <RootCauseSection data={rootCauseData} />
        <InterventionFunnelSection data={interventionData} />
      </div>
    </div>
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
