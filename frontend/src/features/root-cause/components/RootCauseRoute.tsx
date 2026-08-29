import React from 'react';
import { useRootCauseQuery } from '../api/getRootCause';
import { useInterventionQuery } from '../api/getIntervention';

export const RootCauseRoute: React.FC = () => {
  const { data: rootCauseData } = useRootCauseQuery();
  const { data: interventionData } = useInterventionQuery();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight text-[var(--color-primary)]">Root Cause & Intervention</h1>
        <p className="text-sm text-[var(--color-text-muted)] mt-1">
          Raw responses from /metrics/by-root-cause and /metrics/by-intervention
        </p>
      </div>
      
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6 overflow-x-auto">
          <h2 className="text-sm font-semibold mb-4 text-[var(--color-primary)]">Root Cause Metrics</h2>
          <pre className="text-xs font-mono text-gray-800">
            {JSON.stringify(rootCauseData, null, 2)}
          </pre>
        </div>
        
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6 overflow-x-auto">
          <h2 className="text-sm font-semibold mb-4 text-[var(--color-primary)]">Intervention Metrics</h2>
          <pre className="text-xs font-mono text-gray-800">
            {JSON.stringify(interventionData, null, 2)}
          </pre>
        </div>
      </div>
    </div>
  );
};

export default RootCauseRoute;
