import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Job, JobState } from "../api/types";

const RUNNING: JobState[] = ["UPLOADED", "ANALYZING", "ENRICHING", "SENDING"];

/** Load a job and keep polling while a workflow is running on it. */
export function useJobPoll(jobId: string, intervalMs: number) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = () =>
      api.getJob(jobId).then(
        (j) => {
          if (cancelled) return;
          setJob(j);
          if (RUNNING.includes(j.state)) timer = setTimeout(load, intervalMs);
        },
        (e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)),
      );
    load();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [jobId, intervalMs, tick]);
  return { job, error, reload: () => setTick((t) => t + 1) };
}
