import React from 'react';
import type { components } from '@/api/schema';
import { InterventionFunnelRow } from './InterventionFunnelRow';

type InterventionMetrics = components['schemas']['InterventionMetrics'];

interface InterventionFunnelSectionProps {
  data: InterventionMetrics[];
}

export const InterventionFunnelSection: React.FC<InterventionFunnelSectionProps> = ({ data }) => {
  if (!data || data.length === 0) return null;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between border-b border-gray-100 pb-2">
        <div>
          <h2 className="text-lg font-semibold text-[var(--color-primary)]">Intervention Funnel</h2>
          <p className="text-sm text-gray-500 mt-1">
            Tracking the drop-off from recommendation to final recovery.
          </p>
        </div>
      </div>
      
      <div className="flex flex-col gap-4">
        {data.map((interventionData, idx) => (
          <InterventionFunnelRow key={idx} data={interventionData} />
        ))}
      </div>
    </div>
  );
};
