import { useSuspenseQuery } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type MetricsSummary = components['schemas']['MetricsSummary'];

export const getSummary = async ({ signal }: { signal?: AbortSignal }) => {
  return apiClient<MetricsSummary>('/metrics/summary', { signal });
};

export const useSummaryQuery = () => {
  return useSuspenseQuery({
    queryKey: ['metrics', 'summary'],
    queryFn: ({ signal }) => getSummary({ signal }),
  });
};
