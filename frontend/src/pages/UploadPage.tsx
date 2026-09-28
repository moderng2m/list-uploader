import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api, uploadFile, waitForParse } from "../api/client";
import { PageHeader } from "../components/ui";

const ALLOWED = [".csv", ".xlsx"];
const MAX_BYTES = 10 * 1024 * 1024;

export function validateFile(file: File): string | null {
  const ext = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")).toLowerCase() : "";
  if (!ALLOWED.includes(ext))
    return `This file is a ${ext || "file with no extension"}. Upload a .csv or .xlsx file — you can download the template below.`;
  if (file.size === 0) return "This file is empty. Check that you saved your data in it, then upload it again.";
  if (file.size > MAX_BYTES) return "This file is over 10 MB. Split it into smaller files and upload each one.";
  return null;
}

type Phase = "idle" | "uploading" | "reading";

export function UploadPage({ pollIntervalMs = 1000 }: { pollIntervalMs?: number }) {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [enrich, setEnrich] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    const problem = validateFile(file);
    if (problem) return setError(problem);
    setError(null);
    try {
      setPhase("uploading");
      const created = await api.createJob(file.name, enrich);
      await uploadFile(created.upload, file);
      await api.markUploaded(created.job.job_id);
      setPhase("reading");
      const job = await waitForParse(created.job.job_id, { intervalMs: pollIntervalMs });
      if (job.state === "MAPPING_REVIEW") {
        navigate(`/jobs/${job.job_id}/mapping`);
        return;
      }
      setError(job.parse_error?.message ?? "We couldn't read this file. Try uploading it again.");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
    setPhase("idle");
  }

  const busy = phase !== "idle";
  return (
    <>
      <PageHeader title="Upload a lead list">
        Upload a .csv or .xlsx file (first sheet only, up to 10 MB and 5,000 rows).
      </PageHeader>
      <form className="card form" onSubmit={onSubmit} aria-busy={busy}>
        <label className="field">
          <span>File</span>
          <input
            type="file"
            accept=".csv,.xlsx"
            disabled={busy}
            onChange={(e) => {
              setError(null);
              setFile(e.target.files?.[0] ?? null);
            }}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={enrich}
            disabled={busy}
            onChange={(e) => setEnrich(e.target.checked)}
          />
          Enrich leads with ZoomInfo
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="submit" className="primary" disabled={!file || busy}>
            {phase === "uploading" ? "Uploading…" : phase === "reading" ? "Reading your file…" : "Upload and continue"}
          </button>
          <a href="/List_Upload_Template.xlsx" download>
            Download the template
          </a>
        </div>
        {busy && (
          <p className="muted" role="status">
            {phase === "uploading" ? "Uploading your file…" : "Checking columns and rows. This usually takes a few seconds."}
          </p>
        )}
      </form>
    </>
  );
}
