import { useState } from "react";
import { fmtGB, Job } from "../api";
import type { Ctx } from "../App";
import { useServerEvents } from "../events";
import { I } from "./icons";

// Live progress for a background job (download/install), fed by "job" events over the WebSocket.
export function useJob(ctx: Ctx, initial: Job | null): [Job | null, (j: Job | null) => void] {
  const [job, setJob] = useState<Job | null>(initial);
  useServerEvents(ctx, (e) => {
    if (e.type === "job") {
      const j = e.job as Job;
      setJob((cur) => (cur && cur.id === j.id ? j : cur));
    }
  });
  return [job, setJob];
}

export default function JobProgress({ job }: { job: Job | null }) {
  if (!job) return null;
  const pct = Math.round((job.fraction || 0) * 100);
  return (
    <div style={{ marginTop: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5 }} className="muted">
        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>{job.status === "failed" ? <I.fail /> : job.status === "done" ? <I.ok /> : <I.spin className="spin-anim" />}{job.step || job.title}</span>
        <span>{job.total_bytes ? `${fmtGB(job.done_bytes)} / ${fmtGB(job.total_bytes)} · ${pct}%` : ""}</span>
      </div>
      {job.status === "running" && <div className="progress"><i style={{ width: `${job.total_bytes ? pct : 5}%` }} /></div>}
      {job.status === "failed" && <div className="banner err" style={{ marginTop: 6 }}>{job.error}</div>}
    </div>
  );
}
