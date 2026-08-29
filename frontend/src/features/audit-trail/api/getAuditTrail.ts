import { useSuspenseQuery } from '@tanstack/react-query';
import { apiClient } from '@/api/client';
import type { components } from '@/api/schema';

type AuditTrail = components['schemas']['AuditTrail'];
type RevenueEventRecord = components['schemas']['RevenueEventRecord'];

export const getEvents = async ({ signal }: { signal?: AbortSignal }) => {
  return apiClient<RevenueEventRecord[]>('/events', { signal });
};

export const getAuditTrail = async (eventId: string, { signal }: { signal?: AbortSignal }) => {
  return apiClient<AuditTrail>(`/audit-trail/${eventId}`, { signal });
};

export const useEventsQuery = () => {
  return useSuspenseQuery({
    queryKey: ['events'],
    queryFn: ({ signal }) => getEvents({ signal }),
  });
};

export const useAuditTrailQuery = (eventId: string | null) => {
  return useSuspenseQuery({
    queryKey: ['audit-trail', eventId],
    queryFn: ({ signal }) => getAuditTrail(eventId!, { signal }),
  });
};
