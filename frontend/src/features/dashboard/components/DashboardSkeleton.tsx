import React from 'react';


export const DashboardSkeleton: React.FC = () => {
  return (
    <div className="space-y-8 animate-pulse">
      {/* Header Skeleton */}
      <div>
        <div className="h-8 w-64 bg-gray-200 rounded-md mb-2"></div>
        <div className="h-4 w-96 bg-gray-100 rounded-md"></div>
      </div>

      {/* Headline Metrics Grid Skeleton */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
        {[...Array(4)].map((_, i) => (
          <div key={i} className="bg-white rounded-xl border border-gray-100 p-6 flex flex-col h-36">
            <div className="flex justify-between items-center mb-4">
              <div className="h-3 w-24 bg-gray-100 rounded-md"></div>
              <div className="h-4 w-4 bg-gray-100 rounded-full"></div>
            </div>
            <div className="h-8 w-32 bg-gray-200 rounded-md mt-auto"></div>
          </div>
        ))}
      </div>

      {/* Secondary Metrics Row Skeleton */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {[...Array(3)].map((_, i) => (
          <div key={i} className="bg-[var(--color-bg-surface)] rounded-xl border border-gray-100 p-5 flex flex-col h-24">
            <div className="h-3 w-32 bg-gray-200 rounded-md mb-3"></div>
            <div className="h-6 w-20 bg-gray-300 rounded-md"></div>
          </div>
        ))}
      </div>
    </div>
  );
};
