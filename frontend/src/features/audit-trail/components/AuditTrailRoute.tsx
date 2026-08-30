import { Suspense, useMemo, useState, type ReactNode } from 'react';
import { motion } from 'framer-motion';
import type { components } from '@/api/schema';
import { formatCurrency, formatLabel } from '@/utils/formatters';
import { useAuditTrailQuery, useEventsQuery } from '../api/getAuditTrail';
import { AuditTrailErrorBoundary } from './AuditTrailErrorBoundary';
import { AuditTrailSkeleton } from './AuditTrailSkeleton';

type Diagnosis = components['schemas']['DiagnosisRecord'];
type Decision = components['schemas']['DecisionRecord'];
type PolicyVerdict = components['schemas']['PolicyVerdictRecord'];

const EXAMPLES = [
  { id: 'pol_S4_MULTI', label: 'Opt-out block · version history' },
  { id: 'exe_S5ADV_20260825T045458_HONEST', label: 'Completed recovery' },
  { id: 'demo_191_rcv', label: 'Free-text promise' },
  { id: 'exe_S5ADV_20260825T045458_FAILKEY', label: 'Genuine execution failure' },
];

function VersionTabs({ stage, versions, selected, onChange }: { stage: string; versions: number[]; selected: number; onChange: (version: number) => void }) {
  return <div aria-label={`${stage} version navigation`} className="mb-3 flex flex-wrap items-center gap-1.5">
    <span className="mr-1 text-xs font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">Versions</span>
    {versions.map((version) => <button aria-pressed={selected === version} className={`rounded-md border px-2.5 py-1 text-xs font-bold transition-colors ${selected === version ? 'border-[var(--color-primary)] bg-[var(--color-primary)] text-white' : 'border-gray-200 bg-white text-[var(--color-text-muted)] hover:border-[var(--color-accent)]'}`} data-testid={`${stage.toLowerCase()}-version-${version}`} key={version} onClick={() => onChange(version)} type="button">v{version}</button>)}
  </div>;
}

function VersionedPanel<T extends { version: number }>({ stage, records, render }: { stage: string; records: T[]; render: (record: T) => ReactNode }) {
  const [selectedVersion, setSelectedVersion] = useState(() => records.at(-1)?.version ?? 1);
  const active = records.find((record) => record.version === selectedVersion) ?? records.at(-1);
  if (!active) return null;
  return <div>
    <VersionTabs onChange={setSelectedVersion} selected={active.version} stage={stage} versions={records.map((record) => record.version)} />
    <motion.div animate={{ opacity: 1, y: 0 }} initial={{ opacity: 0, y: 4 }} key={active.version} transition={{ duration: 0.16 }}>{render(active)}</motion.div>
  </div>;
}

function Stage({ title, color, children, empty }: { title: string; color: string; children?: ReactNode; empty?: string }) {
  return <motion.section animate={{ opacity: 1, x: 0 }} className="relative border-l-2 border-gray-200 pl-6" initial={{ opacity: 0, x: -10 }} transition={{ duration: 0.22 }}>
    <span className={`absolute -left-[7px] top-1 h-3 w-3 rounded-full ${color}`} />
    <h2 className="font-semibold text-[var(--color-primary)]">{title}</h2>
    {children ? <div className="mt-2 text-sm text-[var(--color-text-muted)]">{children}</div> : <p className="mt-2 text-sm italic text-[var(--color-text-muted)]">{empty}</p>}
  </motion.section>;
}

function DiagnosisCard({ item }: { item: Diagnosis }) {
  return <div className="rounded-lg bg-[var(--color-bg-surface)] p-3">
    <strong>v{item.version} · {formatLabel(item.root_cause)}</strong> · confidence {item.confidence} · {item.recoverable ? 'recoverable' : 'not recoverable'}
    <p className="mt-1">Method: {item.method}{item.llm_model ? ` · ${item.llm_model}` : ''} · Evidence: {(item.evidence ?? []).join('; ') || 'None recorded'}</p>
  </div>;
}

function DecisionCard({ item }: { item: Decision }) {
  return <div className="rounded-lg bg-[var(--color-bg-surface)] p-3">
    <strong>v{item.version} · {formatLabel(item.recommended_intervention)}</strong>
    <p className="mt-1">ERV: {formatCurrency(item.revenue_at_risk)} × {item.recovery_probability} − {formatCurrency(item.estimated_cost)} = <strong>{formatCurrency(item.expected_recovery_value)}</strong></p>
    <p className="mt-2 leading-6">{item.reasoning}</p>
  </div>;
}

function PolicyCard({ item }: { item: PolicyVerdict }) {
  return <div className="rounded-lg border border-gray-100 bg-white p-3">
    <strong className={item.verdict === 'blocked' ? 'text-[var(--color-status-blocked)]' : 'text-[var(--color-status-recovered)]'}>v{item.version} · {item.verdict.toUpperCase()} · {formatLabel(item.reason)}</strong>
    <p className="mt-1 font-mono text-xs" data-testid="rulebook-fingerprint">rulebook_fingerprint: {item.rulebook_fingerprint} · {item.rulebook_fingerprint_source}</p>
    <ul className="mt-2 list-disc space-y-1 pl-4 text-xs leading-5">{item.checks_performed.map((check) => <li className={check.includes('FAIL') ? 'text-[var(--color-status-blocked)]' : ''} key={check}>{check}</li>)}</ul>
  </div>;
}

function Trail({ eventId }: { eventId: string }) {
  const { data } = useAuditTrailQuery(eventId);
  const blocked = data.policy_verdicts.some((verdict) => verdict.verdict === 'blocked');
  const recovered = data.distinct_recoveries > 0;
  const blockedReason = data.policy_verdicts.find((item) => item.verdict === 'blocked')?.reason;
  const summary = recovered ? `Recovered ${formatCurrency(data.revenue_recovered)} via ${formatLabel(data.executions[0]?.intervention ?? 'execution')}; gateway verification is recorded below.` : blocked ? `Blocked — ${formatLabel(blockedReason ?? 'policy')}. No action was authorized.` : data.promises.length ? `Awaiting promise — customer committed to pay by ${data.promises[0].promised_date}.` : 'Still at risk — no completed recovery path recorded.';
  return <div className="mt-6 space-y-7">
    <section className="rounded-xl border border-gray-100 bg-white p-6 shadow-sm"><div className="flex flex-col gap-3 md:flex-row md:justify-between"><div><p className="font-mono text-sm font-semibold text-[var(--color-primary)]">{data.event_id}</p><p className="mt-1 text-sm text-[var(--color-text-muted)]">{summary}</p></div><div className="text-left md:text-right"><p className="text-2xl font-bold text-[var(--color-primary)]">{formatCurrency(data.event.amount)}</p><span className="text-xs font-bold uppercase tracking-wide text-[var(--color-text-muted)]">{data.event.status}</span></div></div></section>
    <div className="space-y-7">
      <Stage color="bg-[var(--color-status-risk)]" title="Ingestion"><p>{data.event.surface} · raw reason <strong>{data.event.raw_failure_reason}</strong> · customer {data.event.customer_ref} · {data.event.created_at}</p></Stage>
      <Stage color="bg-[var(--color-accent)]" empty="No diagnosis recorded." title="Diagnosis">{data.diagnoses.length ? <VersionedPanel records={data.diagnoses} render={(item) => <DiagnosisCard item={item} />} stage="Diagnosis" /> : null}</Stage>
      <Stage color="bg-[var(--color-status-risk)]" empty="No decision recorded." title="Decision">{data.decisions.length ? <VersionedPanel records={data.decisions} render={(item) => <DecisionCard item={item} />} stage="Decision" /> : null}</Stage>
      <Stage color={blocked ? 'bg-[var(--color-status-blocked)]' : 'bg-[var(--color-status-recovered)]'} empty="No policy verdict recorded." title="Policy verdict">{data.policy_verdicts.length ? <VersionedPanel records={data.policy_verdicts} render={(item) => <PolicyCard item={item} />} stage="Policy" /> : null}</Stage>
      <Stage color={data.executions.some((item) => item.status === 'failed') ? 'bg-[var(--color-status-blocked)]' : 'bg-[var(--color-status-recovered)]'} empty={blocked ? 'Not reached — blocked by policy.' : 'Not reached — no authorized execution recorded.'} title="Execution">{data.executions.length ? <div className="space-y-2">{data.executions.map((item) => <div key={item.id}><strong>{formatLabel(item.intervention)} · {item.status}</strong> · {item.action_type}{item.razorpay_payment_link_url && <> · <a className="text-[var(--color-accent)] underline" href={item.razorpay_payment_link_url} rel="noreferrer" target="_blank">Razorpay link</a></>}{item.failure_reason && <p className="mt-1 text-[var(--color-status-blocked)]">Failure: {item.failure_reason}</p>}</div>)}</div> : null}</Stage>
      <Stage color="bg-[var(--color-status-recovered)]" empty={data.executions.length ? 'Not reached — no verification recorded.' : 'Not reached — no execution to verify.'} title="Verification">{data.verifications.length ? <div className="space-y-2">{data.verifications.map((item) => <div key={item.id}><strong>{item.outcome}</strong> · {formatCurrency(item.amount_recovered)} · {item.source}{item.amount_mismatch ? ' · amount mismatch' : ''}</div>)}</div> : null}</Stage>
      <Stage color="bg-[var(--color-status-risk)]" empty="No promise recorded for this event." title="Promise to pay">{data.promises.length ? <div>{data.promises.map((item) => <p key={item.id}><strong>{item.state}</strong> · {formatCurrency(item.promised_amount)} by {item.promised_date}</p>)}</div> : null}</Stage>
    </div>
  </div>;
}

export function AuditTrailRoute() {
  const { data: events } = useEventsQuery();
  const [selected, setSelected] = useState('pol_S4_MULTI');
  const [term, setTerm] = useState('');
  const matches = useMemo(() => events.filter((event) => event.event_id.toLowerCase().includes(term.toLowerCase())).slice(0, 8), [events, term]);
  return <div className="space-y-6 pb-12">
    <header><h1 className="mt-2 text-3xl font-bold tracking-tight text-[var(--color-primary)]">Audit Trail Explorer</h1><p className="mt-2 max-w-3xl text-sm text-[var(--color-text-muted)]">One event’s explainable path from ingestion through policy and recovery.</p></header>
    <section className="rounded-xl border border-gray-100 bg-white p-6 shadow-sm"><div className="flex flex-wrap gap-2">{EXAMPLES.map((item) => <button className={`rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors ${selected === item.id ? 'border-[var(--color-primary)] bg-[var(--color-primary)] text-white' : 'border-gray-200 hover:border-[var(--color-accent)]'}`} key={item.id} onClick={() => setSelected(item.id)} type="button">{item.label}</button>)}</div><input className="mt-4 w-full rounded-lg border border-gray-200 px-3 py-2 text-sm outline-none focus:border-[var(--color-accent)]" onChange={(event) => setTerm(event.target.value)} placeholder="Search 305 event IDs…" value={term} />{term && <div className="mt-2 flex flex-wrap gap-2">{matches.map((item) => <button className="rounded bg-[var(--color-bg-surface)] px-2 py-1 font-mono text-xs" key={item.event_id} onClick={() => { setSelected(item.event_id); setTerm(''); }} type="button">{item.event_id}</button>)}</div>}</section>
    <Suspense fallback={<AuditTrailSkeleton />}><Trail eventId={selected} /></Suspense>
  </div>;
}

export default function AuditTrailRouteWithBoundary() { return <AuditTrailErrorBoundary><AuditTrailRoute /></AuditTrailErrorBoundary>; }
