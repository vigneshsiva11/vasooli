import React, { Suspense, useState, useEffect } from 'react';
import { useEventsQuery, useAuditTrailQuery } from '../api/getAuditTrail';

const AuditTrailDetails: React.FC<{ eventId: string }> = ({ eventId }) => {
  const { data } = useAuditTrailQuery(eventId);
  
  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6 overflow-x-auto mt-4">
      <h2 className="text-sm font-semibold mb-4 text-[var(--color-primary)]">Audit Trail for {eventId}</h2>
      <pre className="text-xs font-mono text-gray-800">
        {JSON.stringify(data, null, 2)}
      </pre>
    </div>
  );
};

export const AuditTrailRoute: React.FC = () => {
  const { data: events } = useEventsQuery();
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);

  useEffect(() => {
    if (events && events.length > 0 && !selectedEventId) {
      setSelectedEventId(events[0].event_id);
    }
  }, [events, selectedEventId]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight text-[var(--color-primary)]">Audit Trail Explorer</h1>
        <p className="text-sm text-[var(--color-text-muted)] mt-1">
          Raw responses from /events and /audit-trail/{"{event_id}"}
        </p>
      </div>

      <div className="flex gap-4 mb-6">
        <label className="text-sm font-medium text-[var(--color-primary)] self-center">Select Event:</label>
        <select 
          className="border border-gray-300 rounded px-3 py-1.5 text-sm outline-none focus:border-[var(--color-accent)]"
          value={selectedEventId || ''} 
          onChange={(e) => setSelectedEventId(e.target.value)}
        >
          {events.map((evt) => (
            <option key={evt.event_id} value={evt.event_id}>{evt.event_id}</option>
          ))}
        </select>
      </div>
      
      {selectedEventId ? (
        <Suspense fallback={<div className="p-4 text-sm text-[var(--color-text-muted)]">Loading trail...</div>}>
          <AuditTrailDetails eventId={selectedEventId} />
        </Suspense>
      ) : (
        <div className="p-4 text-sm text-[var(--color-text-muted)]">No events found.</div>
      )}
    </div>
  );
};

export default AuditTrailRoute;
