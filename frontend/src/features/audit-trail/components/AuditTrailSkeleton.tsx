export function AuditTrailSkeleton() {
  return (
    <div className="space-y-7 animate-pulse">
      <div><div className="h-9 w-72 rounded bg-gray-200" /><div className="mt-2 h-4 w-[30rem] max-w-full rounded bg-gray-100" /></div>
      <div className="rounded-xl border border-gray-100 bg-white p-6"><div className="flex flex-wrap gap-2"><div className="h-8 w-44 rounded-full bg-gray-100" /><div className="h-8 w-36 rounded-full bg-gray-100" /></div><div className="mt-4 h-10 w-full rounded-lg bg-gray-100" /></div>
      <div className="rounded-xl border border-gray-100 bg-white p-6"><div className="h-4 w-40 rounded bg-gray-200" /><div className="mt-3 h-4 w-3/5 rounded bg-gray-100" /><div className="mt-6 space-y-6 border-l-2 border-gray-100 pl-6">{[1, 2, 3, 4].map((item) => <div key={item}><div className="h-4 w-28 rounded bg-gray-200" /><div className="mt-3 h-16 rounded-lg bg-gray-100" /></div>)}</div></div>
    </div>
  );
}
