import { useEffect, useState } from "react";
import { fmtGB, Job } from "../api";
import type { Ctx } from "../App";

// Live progress for a background job (download/install), fed by "job" events over the WebSocket.
export function useJob(ctx: Ctx, initial: Job | null): [Job | null, (j: Job | null) => void] {
  const [job, setJob] = useState<Job | null>(initial);
  useEffect(() => {
    const e = ctx.lastEvent;
    if (e?.type === "job") {
      const j = e.job as Job;
      setJob((cur) => (cur && cur.id === j.id ? j : cur));
    }
  }, [ctx.lastEvent]);
  return [job, setJob];
}

export default function JobProgress({ job }: { job: Job | null }) {
  if (!job) return null;
  const pct = Math.round((job.fraction || 0) * 100);
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5 }} className="muted">
        <span>{job.status === "failed" ? "✕ " : job.status === "done" ? "✓ " : ""}{job.step || job.title}</span>
        <span>{job.total_bytes ? `${fmtGB(job.done_bytes)} / ${fmtGB(job.total_bytes)} · ${pct}%` : ""}</span>
      </div>
      {job.status === "running" && <div className="progress"><i style={{ width: `${job.total_bytes ? pct : 5}%` }} /></div>}
      {job.status === "failed" && <div className="banner err" style={{ marginTop: 6 }}>{job.error}</div>}
    </div>
  );
}
