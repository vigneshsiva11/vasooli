import { useSuspenseQuery } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type InterventionMetrics = components['schemas']['InterventionMetrics'];

export const getIntervention = async ({ signal }: { signal?: AbortSignal }) => {
  return apiClient<InterventionMetrics[]>('/metrics/by-intervention', { signal });
};

export const useInterventionQuery = () => {
  return useSuspenseQuery({
    queryKey: ['metrics', 'intervention'],
    queryFn: ({ signal }) => getIntervention({ signal }),
  });
};
