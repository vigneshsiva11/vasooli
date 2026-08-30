import React from 'react';
import { motion } from 'framer-motion';
import { Bar, BarChart, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { components } from '@/api/schema';
import { formatCurrency } from '@/utils/formatters';

type BaselineComparison = components['schemas']['BaselineComparison'];
interface Props { data: BaselineComparison; }
interface ChartDatum { label: string; value: number; patternId: string; }

export const ComparisonChart: React.FC<Props> = ({ data }) => {
  const chartData: ChartDatum[] = [
    { label: 'Retry everything', value: data.baseline_retry_everything.gross_expected_recovery, patternId: 'baseline-hatch' },
    { label: 'Generic reminder', value: data.baseline_generic_reminder.gross_expected_recovery, patternId: 'baseline-hatch' },
    { label: 'Vasooli', value: data.vasooli_expected.gross_expected_recovery, patternId: 'vasooli-hatch' },
  ];

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35 }}>
      <div className="mb-3 flex items-center gap-4 text-xs text-[var(--color-text-muted)]"><span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-[var(--color-status-risk)]" /> Naive baseline</span><span className="flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm bg-[var(--color-accent)]" /> Root-cause-aware Vasooli</span></div>
      <div className="h-[260px]" aria-label="Simulated gross expected recovery comparison">
        <ResponsiveContainer width="100%" height="100%"><BarChart data={chartData} layout="vertical" margin={{ top: 4, right: 82, left: 8, bottom: 0 }} barCategoryGap="24%">
          <defs>
            <pattern id="baseline-hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="8" height="8" fill="var(--color-status-risk)" /><line x1="0" y1="0" x2="0" y2="8" stroke="var(--color-bg-base)" strokeWidth="2" opacity="0.38" /></pattern>
            <pattern id="vasooli-hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><rect width="8" height="8" fill="var(--color-accent)" /><line x1="0" y1="0" x2="0" y2="8" stroke="var(--color-bg-base)" strokeWidth="2" opacity="0.32" /></pattern>
          </defs>
          <XAxis type="number" hide /><YAxis type="category" dataKey="label" width={122} tickLine={false} axisLine={false} tick={{ fill: 'var(--color-text-muted)', fontSize: 12, fontWeight: 600 }} />
          <Tooltip cursor={{ fill: 'var(--color-bg-surface)' }} contentStyle={{ borderRadius: 8, border: '1px solid #e5e7eb', boxShadow: '0 8px 20px rgba(0,0,0,0.08)' }} formatter={(value) => [formatCurrency(Number(value ?? 0)), 'Simulated gross expected recovery']} />
          <Bar dataKey="value" isAnimationActive animationBegin={80} animationDuration={700} animationEasing="ease-out" radius={[6, 6, 6, 6]}>{chartData.map((entry) => <Cell key={entry.label} fill={`url(#${entry.patternId})`} />)}<LabelList dataKey="value" position="right" formatter={(value) => formatCurrency(Number(value ?? 0))} fill="var(--color-primary)" fontSize={12} fontWeight={700} /></Bar>
        </BarChart></ResponsiveContainer>
      </div>
    </motion.div>
  );
};
