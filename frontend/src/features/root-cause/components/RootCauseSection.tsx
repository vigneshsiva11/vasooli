import React from 'react';
import type { components } from '@/api/schema';
import { formatCurrency, formatPercentage, formatLabel } from '@/utils/formatters';

type RootCauseMetrics = components['schemas']['RootCauseMetrics'];

interface RootCauseSectionProps {
  data: RootCauseMetrics[];
}

export const RootCauseSection: React.FC<RootCauseSectionProps> = ({ data }) => {
  if (!data || data.length === 0) return null;

  // Sort by revenue_at_risk descending
  const sortedData = [...data].sort((a, b) => b.revenue_at_risk - a.revenue_at_risk);
  
  // The comparison baseline is the largest risk value in the complete dataset,
  // not the current table cell or a CSS minimum.
  const maxRisk = Math.max(...data.map((row) => row.revenue_at_risk), 0);
  
  // Find top and bottom performers (excluding those with 0 risk or null rates)
  const validRates = sortedData
    .filter(d => d.recovery_rate !== null && d.revenue_at_risk > 0)
    .map(d => d.recovery_rate as number);
  
  const highestRate = validRates.length > 0 ? Math.max(...validRates) : -1;

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <div className="px-6 py-5 border-b border-gray-100 flex items-center justify-between">
        <h2 className="text-lg font-semibold text-[var(--color-primary)]">Root Cause Breakdown</h2>
      </div>
      
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm whitespace-nowrap">
          <thead className="bg-gray-50/50 text-[var(--color-text-muted)] text-xs uppercase tracking-wider">
            <tr>
              <th className="px-6 py-4 font-medium">Root Cause</th>
              <th className="px-6 py-4 font-medium text-right">Events</th>
              <th className="px-6 py-4 font-medium w-2/5">Revenue Impact & Recovery</th>
              <th className="px-6 py-4 font-medium text-right">Recovery Rate</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-50">
            {sortedData.map((row, idx) => {
              // The grey bar scales against the largest at-risk amount. The green
              // segment then scales within that grey bar, rather than against maxRisk.
              const riskPercent = maxRisk > 0
                ? (row.revenue_at_risk / maxRisk) * 100
                : 0;
              const recoveredPercent = row.revenue_at_risk > 0
                ? (row.revenue_recovered / row.revenue_at_risk) * 100
                : 0;
                
              const isTopPerformer = row.recovery_rate === highestRate && highestRate > 0;
              
              return (
                <tr key={idx} className={`border-b border-gray-50 last:border-0 hover:bg-gray-50/50 transition-colors ${row.superseded_only ? 'opacity-60 grayscale' : ''}`}>
                  <td className="px-6 py-4">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-[var(--color-primary)]">
                        {formatLabel(row.root_cause)}
                      </span>
                      {row.superseded_only && (
                        <span 
                          className="px-2 py-0.5 text-[10px] uppercase font-bold tracking-wide rounded bg-gray-100 text-gray-500"
                          title="Historical only (Replaced by newer diagnosis)"
                        >
                          Superseded
                        </span>
                      )}
                    </div>
                  </td>
                  
                  <td className="px-6 py-4 text-right text-gray-600 font-medium">
                    {row.events.toLocaleString()}
                  </td>
                  
                  <td className="px-6 py-4">
                    <div className="flex flex-col gap-1.5 min-w-[200px]">
                      <div className="flex justify-between text-xs">
                        <span className="font-medium text-gray-700">{formatCurrency(row.revenue_at_risk)} at risk</span>
                        <span className="text-[var(--color-status-recovered)] font-medium">{formatCurrency(row.revenue_recovered)} recovered</span>
                      </div>
                      
                      {/* The Mini-Bar */}
                      <div className="h-2 w-full bg-gray-100 rounded-full overflow-hidden relative mt-1">
                        {/* Risk Bar (Relative to max risk) */}
                        <div 
                          className="absolute left-0 top-0 bottom-0 bg-gray-400 rounded-full overflow-hidden"
                          style={{ width: `${riskPercent}%` }}
                        >
                          {/* Recovered Bar (Relative to the risk bar) */}
                          <div 
                            className="absolute left-0 top-0 bottom-0 bg-[var(--color-status-recovered)] transition-all duration-1000 ease-out"
                            style={{ width: `${recoveredPercent}%` }}
                          />
                        </div>
                      </div>
                    </div>
                  </td>
                  
                  <td className="px-6 py-4 text-right">
                    <div className="flex items-center justify-end gap-2">
                      {isTopPerformer && (
                        <span className="px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide rounded bg-[#6B8E23]/10 text-[var(--color-status-recovered)]">
                          Top
                        </span>
                      )}
                      <span className="font-bold text-[var(--color-primary)]">
                        {formatPercentage(row.recovery_rate)}
                      </span>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};
