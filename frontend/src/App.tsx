import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { AppShell } from './components/AppShell';

// Lazy load feature routes for performance (Suspense-first approach)
const DashboardRoute = React.lazy(() => import('./features/dashboard/components/DashboardRoute'));
const RootCauseRoute = React.lazy(() => import('./features/root-cause/components/RootCauseRoute'));
const BaselineRoute = React.lazy(() => import('./features/baseline/components/BaselineRoute'));
const PtpRoute = React.lazy(() => import('./features/ptp/components/PtpRoute'));
const AuditTrailRoute = React.lazy(() => import('./features/audit-trail/components/AuditTrailRoute'));

// Global query client with retry backoff for transient failures
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (failureCount, error: any) => {
        // Retry only for 5xx errors or network errors
        const isHttpError = typeof error?.status === 'number';
        const isTransient = !isHttpError || error.status >= 500;
        return failureCount < 3 && isTransient;
      },
      retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000),
      staleTime: 1000 * 60, // 1 minute
      refetchOnWindowFocus: false,
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<DashboardRoute />} />
            <Route path="/root-cause" element={<RootCauseRoute />} />
            <Route path="/baseline" element={<BaselineRoute />} />
            <Route path="/ptp" element={<PtpRoute />} />
            <Route path="/audit-trail" element={<AuditTrailRoute />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

export default App;
