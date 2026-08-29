import React, { Suspense } from 'react';
import { NavLink, Outlet } from 'react-router-dom';
import { LayoutDashboard, TrendingDown, Target, ScrollText, GitPullRequest } from 'lucide-react';
import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import { motion } from 'framer-motion';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const NAV_ITEMS = [
  { path: '/', label: 'Overview', icon: LayoutDashboard },
  { path: '/root-cause', label: 'Root Cause', icon: TrendingDown },
  { path: '/baseline', label: 'Baseline', icon: Target },
  { path: '/ptp', label: 'Promises', icon: GitPullRequest },
  { path: '/audit-trail', label: 'Audit Trail', icon: ScrollText },
];

const SuspenseLoader = () => (
  <div className="flex h-full w-full items-center justify-center">
    <div className="h-6 w-6 animate-spin rounded-full border-2 border-[var(--color-accent)] border-t-transparent" />
  </div>
);

export const AppShell: React.FC = () => {
  return (
    <div className="flex h-screen w-full bg-[var(--color-bg-base)] text-[var(--color-text-primary)] antialiased">
      {/* Sidebar Navigation */}
      <nav className="flex w-64 flex-col border-r border-gray-200 bg-[var(--color-bg-surface)] px-4 py-8 shadow-sm z-10">
        <div className="mb-10 px-2 flex items-center gap-3">
          <div className="h-4 w-4 bg-[var(--color-primary)] rounded-sm transform rotate-45"></div>
          <h1 className="text-lg font-semibold tracking-tight text-[var(--color-primary)]">Vasooli</h1>
        </div>
        
        <div className="flex flex-1 flex-col gap-1.5">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                cn(
                  "relative flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                  isActive
                    ? "text-[var(--color-primary)]"
                    : "text-[var(--color-text-muted)] hover:bg-gray-100/50 hover:text-[var(--color-primary)]"
                )
              }
            >
              {({ isActive }) => (
                <>
                  <item.icon className="h-4 w-4 z-10" />
                  <span className="z-10">{item.label}</span>
                  {isActive && (
                    <motion.div
                      layoutId="activeNavTab"
                      className="absolute inset-0 rounded-lg bg-white shadow-sm border border-gray-100"
                      transition={{ type: "spring", stiffness: 350, damping: 30 }}
                    />
                  )}
                </>
              )}
            </NavLink>
          ))}
        </div>
      </nav>

      {/* Main Content Area */}
      <main className="flex-1 overflow-y-auto overflow-x-hidden relative">
        <div className="mx-auto max-w-6xl p-8">
          <Suspense fallback={<SuspenseLoader />}>
            <Outlet />
          </Suspense>
        </div>
      </main>
    </div>
  );
};
