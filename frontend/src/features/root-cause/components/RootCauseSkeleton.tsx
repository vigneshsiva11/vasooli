import React from 'react';

export const RootCauseSkeleton: React.FC = () => {
  return (
    <div className="space-y-6 animate-pulse">
      <div>
        <div className="h-9 w-64 bg-gray-200 rounded-md"></div>
        <div className="h-4 w-96 bg-gray-100 rounded-md mt-2"></div>
      </div>
      
      <div className="space-y-6">
        {/* Section 1 Skeleton */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6">
          <div className="h-6 w-48 bg-gray-200 rounded-md mb-6"></div>
          <div className="space-y-4">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="flex flex-col gap-2 border-b border-gray-50 pb-4 last:border-0 last:pb-0">
                <div className="flex justify-between">
                  <div className="h-4 w-32 bg-gray-100 rounded-md"></div>
                  <div className="h-4 w-24 bg-gray-100 rounded-md"></div>
                </div>
                <div className="h-2 w-full bg-gray-50 rounded-full mt-1 overflow-hidden">
                  <div className={`h-full bg-gray-200 w-${i * 20}`}></div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Section 2 Skeleton */}
        <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6">
          <div className="h-6 w-48 bg-gray-200 rounded-md mb-6"></div>
          <div className="space-y-8">
            {[1, 2].map((i) => (
              <div key={i} className="flex gap-4 items-center">
                <div className="h-4 w-32 bg-gray-100 rounded-md"></div>
                <div className="flex-1 h-12 bg-gray-50 rounded-lg"></div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};
