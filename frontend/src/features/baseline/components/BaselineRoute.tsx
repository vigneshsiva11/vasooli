import React from 'react';
import { useBaselineQuery } from '../api/getBaseline';

export const BaselineRoute: React.FC = () => {
  const { data } = useBaselineQuery();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight text-[var(--color-primary)]">Baseline Comparison</h1>
        <p className="text-sm text-[var(--color-text-muted)] mt-1">
          Raw response from /metrics/baseline-comparison
        </p>
      </div>
      
      <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6 overflow-x-auto">
        <pre className="text-xs font-mono text-gray-800">
          {JSON.stringify(data, null, 2)}
        </pre>
      </div>
    </div>
  );
};

export default BaselineRoute;
