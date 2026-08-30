import React from 'react';
import type { components } from '@/api/schema';
import { formatCurrency, formatPercentage, formatLabel } from '@/utils/formatters';

type InterventionMetrics = components['schemas']['InterventionMetrics'];

interface FunnelRowProps {
  data: InterventionMetrics;
}

export const InterventionFunnelRow: React.FC<FunnelRowProps> = ({ data }) => {
  const recommended = data.times_recommended;
  const percentOfRecommended = (value: number) => (
    recommended > 0 ? (value / recommended) * 100 : 0
  );

  // Every funnel stage uses the same starting denominator: Recommended.
  const recommendedPct = recommended > 0 ? 100 : 0;
  const authPct = percentOfRecommended(data.times_authorized);
  const execPct = percentOfRecommended(data.times_executed);
  const recPct = percentOfRecommended(
    data.recoveries_gateway_verified + data.recoveries_manually_asserted,
  );
  const executedFillHeight = `${execPct}%`;

  console.log(
    `[InterventionFunnelRow] Executed fill diagnostic intervention=${data.intervention} timesExecuted=${data.times_executed} timesExecutedType=${typeof data.times_executed} timesRecommended=${data.times_recommended} execPct=${execPct} executedFillHeight=${executedFillHeight}`,
  );

  // Drop-off percentages
  const recToAuthDrop = data.times_recommended > 0 
    ? ((data.times_recommended - data.times_authorized) / data.times_recommended) * 100 
    : 0;
    
  const authToExecDrop = data.times_authorized > 0 
    ? ((data.times_authorized - data.times_executed) / data.times_authorized) * 100 
    : 0;

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6 flex flex-col gap-6">
      {/* Header: Intervention Name and Summary */}
      <div className="flex justify-between items-start">
        <div>
          <h3 className="text-lg font-bold text-[var(--color-primary)]">
            {formatLabel(data.intervention)}
          </h3>
          <div className="flex items-center gap-2 mt-1">
            <span className="text-sm font-medium text-gray-600">
              Recovery Rate: <span className="text-[var(--color-primary)] font-bold">{formatPercentage(data.recovery_rate)}</span>
            </span>
            {!data.verifiable && (
              <span className="px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide rounded bg-gray-100 text-gray-500">
                Unverifiable by design
              </span>
            )}
          </div>
        </div>
        
        <div className="text-right">
          <div className="text-sm text-gray-500">Total Recovered</div>
          <div className="text-xl font-bold text-[var(--color-status-recovered)]">
            {formatCurrency(data.revenue_recovered)}
          </div>
        </div>
      </div>

      {/* Funnel Flow */}
      <div className="flex flex-col md:flex-row items-stretch gap-2 md:gap-0 mt-2">
        {/* Step 1: Recommended */}
        <div className="flex-1 border border-gray-100 rounded-lg p-4 relative overflow-hidden flex flex-col justify-between h-[120px]">
          <div className="absolute bottom-0 left-0 right-0 bg-[#E0F2FE] transition-all duration-1000" style={{ height: `${recommendedPct}%` }} />
          <div className="relative z-10">
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Recommended</div>
            <div className="text-2xl font-bold text-gray-800">{data.times_recommended.toLocaleString()}</div>
          </div>
        </div>

        {/* Connector 1 */}
        <div className="hidden md:flex flex-col items-center justify-center px-2 z-10 -mx-3">
          <div className="bg-white border border-gray-100 rounded-full text-[10px] font-bold text-red-400 px-2 py-1 shadow-sm">
            -{recToAuthDrop.toFixed(0)}%
          </div>
        </div>

        {/* Step 2: Authorized */}
        <div className="flex-1 border border-gray-100 rounded-lg p-4 relative overflow-hidden flex flex-col justify-between h-[120px]">
          <div className="absolute bottom-0 left-0 right-0 bg-[#E0F2FE] transition-all duration-1000" style={{ height: `${authPct}%` }} />
          <div className="relative z-10">
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-1">Authorized</div>
            <div className="text-2xl font-bold text-gray-800">{data.times_authorized.toLocaleString()}</div>
          </div>
        </div>

        {/* Connector 2 */}
        <div className="hidden md:flex flex-col items-center justify-center px-2 z-10 -mx-3">
          <div className="bg-white border border-gray-100 rounded-full text-[10px] font-bold text-red-400 px-2 py-1 shadow-sm">
            -{authToExecDrop.toFixed(0)}%
          </div>
        </div>

        {/* Step 3: Executed */}
        <div className="flex-1 border border-gray-100 rounded-lg p-4 relative overflow-hidden flex flex-col justify-between h-[120px]">
          <div className="absolute bottom-0 left-0 right-0 bg-[#7DD3FC] transition-all duration-1000" style={{ height: executedFillHeight }} />
          <div className="relative z-10">
            <div className="flex justify-between items-start mb-1">
              <div className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Executed</div>
              {data.times_execution_failed > 0 && (
                <div className="text-[10px] font-bold bg-[var(--color-status-blocked)] text-white px-1.5 py-0.5 rounded-sm">
                  {data.times_execution_failed} Failed
                </div>
              )}
            </div>
            <div className="text-2xl font-bold text-gray-800">{data.times_executed.toLocaleString()}</div>
          </div>
        </div>

        {/* Connector 3 */}
        <div className="hidden md:flex flex-col items-center justify-center px-2 z-10 -mx-3">
          <div className="text-gray-300">
            <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6"/></svg>
          </div>
        </div>

        {/* Step 4: Recovered (Split) */}
        <div className="flex-[1.5] border border-gray-100 rounded-lg p-4 relative overflow-hidden flex flex-col justify-between bg-gray-50/30 h-[120px]">
          <div className="absolute bottom-0 left-0 right-0 bg-[#6B8E23] opacity-10 transition-all duration-1000" style={{ height: `${recPct}%` }} />
          <div className="relative z-10">
            <div className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Recoveries</div>
            
            <div className="flex flex-col gap-2">
              {/* Gateway Verified */}
              <div className="flex justify-between items-end border-b border-gray-200/60 pb-2">
                <div>
                  <div className="text-[10px] font-bold text-[var(--color-status-recovered)] uppercase">Gateway Verified</div>
                  <div className="text-lg font-bold text-gray-800">{data.recoveries_gateway_verified}</div>
                </div>
                <div className="text-sm font-semibold text-[var(--color-status-recovered)]">
                  {formatCurrency(data.revenue_recovered_gateway_verified)}
                </div>
              </div>
              
              {/* Manually Asserted */}
              <div className="flex justify-between items-end pt-1">
                <div>
                  <div className="text-[10px] font-bold text-gray-500 uppercase">Manually Asserted</div>
                  <div className="text-lg font-bold text-gray-700">{data.recoveries_manually_asserted}</div>
                </div>
                <div className="text-sm font-semibold text-gray-600">
                  {formatCurrency(data.revenue_recovered_manually_asserted)}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
