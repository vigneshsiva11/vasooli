import { useMutation, useQueryClient } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type PromiseFromTextRequest = components['schemas']['PromiseFromTextRequest'];
type PromiseFromTextResponse = components['schemas']['PromiseFromTextResponse'];

export const extractPromise = (body: PromiseFromTextRequest) => apiClient<PromiseFromTextResponse>('/promises/from-text', {
  method: 'POST', body: JSON.stringify(body),
});

export const useExtractPromiseMutation = () => {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: extractPromise,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['promises'] });
      queryClient.invalidateQueries({ queryKey: ['metrics', 'ptp'] });
      queryClient.invalidateQueries({ queryKey: ['promise-extractions'] });
    },
  });
};
