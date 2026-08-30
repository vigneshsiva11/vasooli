import React from 'react';
import { motion } from 'framer-motion';

interface MultiplierCalloutProps { multiplier: number; }

export const MultiplierCallout: React.FC<MultiplierCalloutProps> = ({ multiplier }) => (
  <motion.aside initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} transition={{ duration: 0.4, delay: 0.32 }} className="rounded-xl border border-[var(--color-accent)]/25 bg-[var(--color-accent)]/10 p-5">
    <p className="text-xs font-bold uppercase tracking-[0.14em] text-[var(--color-accent)]">Best baseline beaten</p>
    <p className="mt-2 text-4xl font-bold tracking-tight text-[var(--color-primary)]">{multiplier.toFixed(2)}×</p>
    <p className="mt-2 text-sm leading-5 text-[var(--color-text-muted)]">higher simulated gross expected recovery than Generic Reminder, the strongest naive baseline.</p>
  </motion.aside>
);
