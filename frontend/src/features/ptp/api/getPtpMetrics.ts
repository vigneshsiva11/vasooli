import { useSuspenseQuery } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type PromiseMetrics = components['schemas']['PromiseMetrics'];
type PromiseToPay = components['schemas']['PromiseToPayDocument'];
type PromiseExtraction = components['schemas']['PromiseExtractionDocument'];

export const getPtpMetrics = async ({ signal }: { signal?: AbortSignal }) => {
  return apiClient<PromiseMetrics>('/metrics/promise-to-pay', { signal });
};

export const usePtpMetricsQuery = () => {
  return useSuspenseQuery({
    queryKey: ['metrics', 'ptp'],
    queryFn: ({ signal }) => getPtpMetrics({ signal }),
  });
};

export const getPromises = async ({ signal }: { signal?: AbortSignal }) => apiClient<PromiseToPay[]>('/promises', { signal });

export const usePromisesQuery = () => useSuspenseQuery({
  queryKey: ['promises'],
  queryFn: ({ signal }) => getPromises({ signal }),
});

export const getPromiseExtractions = async ({ signal }: { signal?: AbortSignal }) => apiClient<PromiseExtraction[]>('/promise-extractions', { signal });
export const usePromiseExtractionsQuery = () => useSuspenseQuery({ queryKey: ['promise-extractions'], queryFn: ({ signal }) => getPromiseExtractions({ signal }) });
