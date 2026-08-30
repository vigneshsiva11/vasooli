import { useSuspenseQuery } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type RootCauseMetrics = components['schemas']['RootCauseMetrics'];

export const getRootCause = async ({ signal }: { signal?: AbortSignal }) => {
  return apiClient<RootCauseMetrics[]>('/metrics/by-root-cause', { signal });
};

export const useRootCauseQuery = () => {
  return useSuspenseQuery({
    queryKey: ['metrics', 'root-cause'],
    queryFn: ({ signal }) => getRootCause({ signal }),
  });
};
