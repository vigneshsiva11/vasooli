import React, { useEffect, useState } from 'react';
import { motion, useSpring, useTransform } from 'framer-motion';

interface AnimatedNumberProps {
  value: number;
  formatFn?: (val: number) => string;
  className?: string;
  duration?: number; // duration is implicitly handled by spring physics here, but kept for interface completeness
}

export const AnimatedNumber: React.FC<AnimatedNumberProps> = ({ 
  value, 
  formatFn = (val) => val.toString(),
  className = ""
}) => {
  const [mounted, setMounted] = useState(false);
  
  // Spring configuration for a smooth tween.
  // Using moderate stiffness and damping for a quick, confident settling (no bouncy feel).
  const springValue = useSpring(0, {
    stiffness: 70,
    damping: 25,
    mass: 1,
    bounce: 0,
  });

  // Transform the numerical spring value into a formatted string
  const displayValue = useTransform(springValue, (current) => formatFn(current));

  useEffect(() => {
    setMounted(true);
    // When the value changes (or on mount), animate the spring to the new value
    springValue.set(value);
  }, [value, springValue]);

  // Fallback to static value during SSR or before hydration
  if (!mounted) {
    return <span className={className}>{formatFn(value)}</span>;
  }

  return <motion.span className={className}>{displayValue}</motion.span>;
};
