import { useSuspenseQuery } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type BaselineComparison = components['schemas']['BaselineComparison'];

export const getBaseline = async ({ signal }: { signal?: AbortSignal }) => {
  return apiClient<BaselineComparison>('/metrics/baseline-comparison', { signal });
};

export const useBaselineQuery = () => {
  return useSuspenseQuery({
    queryKey: ['metrics', 'baseline'],
    queryFn: ({ signal }) => getBaseline({ signal }),
  });
};
