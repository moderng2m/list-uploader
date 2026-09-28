import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { PageHeader } from "../components/ui";

const ALLOWED = [".csv", ".xlsx"];
const MAX_BYTES = 10 * 1024 * 1024;

export function validateFile(file: File): string | null {
  const ext = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")).toLowerCase() : "";
  if (!ALLOWED.includes(ext))
    return `This file is a ${ext || "file with no extension"}. Upload a .csv or .xlsx file — you can download the template below.`;
  if (file.size === 0) return "This file is empty. Check you saved it, then upload again.";
  if (file.size > MAX_BYTES) return "This file is over 10 MB. Split it into smaller files and upload each one.";
  return null;
}

export function UploadPage() {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [enrich, setEnrich] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!file) return;
    const problem = validateFile(file);
    if (problem) return setError(problem);
    setBusy(true);
    try {
      const { job_id } = await api.createJob(file.name, enrich);
      navigate(`/jobs/${job_id}/mapping`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Upload a lead list">
        Upload a .csv or .xlsx file (first sheet only, up to 10 MB and 5,000 rows).
      </PageHeader>
      <form className="card form" onSubmit={onSubmit}>
        <label className="field">
          <span>File</span>
          <input
            type="file"
            accept=".csv,.xlsx"
            onChange={(e) => {
              setError(null);
              setFile(e.target.files?.[0] ?? null);
            }}
          />
        </label>
        <label className="check">
          <input type="checkbox" checked={enrich} onChange={(e) => setEnrich(e.target.checked)} />
          Enrich leads with ZoomInfo
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="submit" className="primary" disabled={!file || busy}>
            {busy ? "Uploading…" : "Upload and continue"}
          </button>
          <a href="/List_Upload_Template.xlsx" download>
            Download the template
          </a>
        </div>
      </form>
    </>
  );
}
