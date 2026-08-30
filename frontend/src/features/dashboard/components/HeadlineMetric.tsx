import React from 'react';
import { motion } from 'framer-motion';
import { cn } from '@/components/AppShell';

interface HeadlineMetricProps {
  title: string;
  value: React.ReactNode;
  subtitle?: React.ReactNode;
  icon?: React.ElementType;
  className?: string;
  children?: React.ReactNode;
}

export const HeadlineMetric: React.FC<HeadlineMetricProps> = ({
  title,
  value,
  subtitle,
  icon: Icon,
  className,
  children
}) => {
  return (
    <motion.div
      variants={{
        hidden: { opacity: 0, y: 20 },
        show: { opacity: 1, y: 0, transition: { type: 'spring', stiffness: 300, damping: 24 } }
      }}
      className={cn(
        "bg-white rounded-xl shadow-sm border border-gray-100 p-6 flex flex-col",
        className
      )}
    >
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-[var(--color-text-muted)] tracking-wide uppercase">
          {title}
        </h3>
        {Icon && <Icon className="h-4 w-4 text-gray-400" />}
      </div>
      
      <div className="flex items-baseline gap-2">
        <div className="text-3xl font-semibold tracking-tight text-[var(--color-primary)]">
          {value}
        </div>
        {subtitle && (
          <div className="text-sm font-medium text-[var(--color-text-muted)]">
            {subtitle}
          </div>
        )}
      </div>
      
      {children && (
        <div className="mt-4 pt-4 border-t border-gray-100">
          {children}
        </div>
      )}
    </motion.div>
  );
};
