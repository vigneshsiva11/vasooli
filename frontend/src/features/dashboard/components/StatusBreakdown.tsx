import React from 'react';
import { motion } from 'framer-motion';

interface StatusBreakdownProps {
  data: Record<string, number>;
  total: number;
}

const STATUS_CONFIG: Record<string, { label: string; colorClass: string; order: number }> = {
  recovered: { label: 'Recovered', colorClass: 'bg-[var(--color-status-recovered)]', order: 1 },
  at_risk: { label: 'At Risk', colorClass: 'bg-[var(--color-status-risk)]', order: 2 },
  blocked: { label: 'Blocked', colorClass: 'bg-[var(--color-status-blocked)]', order: 3 },
  // Default for other statuses
};

export const StatusBreakdown: React.FC<StatusBreakdownProps> = ({ data, total }) => {
  if (total === 0) {
    return (
      <div className="h-4 w-full bg-gray-100 rounded-full flex items-center justify-center">
        <span className="text-[10px] text-gray-400">No events</span>
      </div>
    );
  }

  // Sort and filter data to ensure stable rendering order
  const segments = Object.entries(data)
    .filter(([_, value]) => value > 0)
    .map(([key, value]) => ({
      key,
      value,
      percentage: (value / total) * 100,
      config: STATUS_CONFIG[key] || { label: key.replace(/_/g, ' '), colorClass: 'bg-gray-300', order: 99 }
    }))
    .sort((a, b) => a.config.order - b.config.order);

  return (
    <div className="flex flex-col gap-3">
      {/* Segmented Bar */}
      <div className="h-4 w-full flex rounded-full overflow-hidden bg-gray-100 gap-[1px]">
        {segments.map((segment) => (
          <motion.div
            key={segment.key}
            initial={{ width: 0 }}
            animate={{ width: `${segment.percentage}%` }}
            transition={{ duration: 1, ease: "easeOut" }}
            className={`h-full ${segment.config.colorClass}`}
            title={`${segment.config.label}: ${segment.value}`}
          />
        ))}
      </div>
      
      {/* Legend */}
      <div className="flex flex-wrap items-center gap-4 text-xs">
        {segments.map((segment) => (
          <div key={segment.key} className="flex items-center gap-1.5">
            <span className={`h-2.5 w-2.5 rounded-full ${segment.config.colorClass}`} />
            <span className="text-[var(--color-text-muted)] capitalize">{segment.config.label}</span>
            <span className="font-medium text-[var(--color-text-primary)]">{segment.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
};
