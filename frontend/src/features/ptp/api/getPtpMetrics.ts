import { useSuspenseQuery } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type PromiseMetrics = components['schemas']['PromiseMetrics'];

export const getPtpMetrics = async ({ signal }: { signal?: AbortSignal }) => {
  return apiClient<PromiseMetrics>('/metrics/promise-to-pay', { signal });
};

export const usePtpMetricsQuery = () => {
  return useSuspenseQuery({
    queryKey: ['metrics', 'ptp'],
    queryFn: ({ signal }) => getPtpMetrics({ signal }),
  });
};
