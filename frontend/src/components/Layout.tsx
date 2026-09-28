import { NavLink, Outlet, useLocation, useParams } from "react-router-dom";
import { api } from "../api/client";
import { useApi } from "./useApi";

const STEPS = [
  { path: "mapping", label: "Map columns" },
  { path: "analysis", label: "Analyze & fix" },
  { path: "enrichment", label: "Enrich" },
  { path: "send", label: "Review & send" },
  { path: "result", label: "Result" },
];

function JobSteps({ jobId }: { jobId: string }) {
  const { pathname } = useLocation();
  const current = STEPS.findIndex((s) => pathname.endsWith(`/${s.path}`));
  return (
    <nav aria-label="Job steps" className="steps">
      <ol>
        {STEPS.map((s, i) => (
          <li key={s.path} className={i === current ? "current" : i < current ? "done" : ""}>
            <NavLink to={`/jobs/${jobId}/${s.path}`}>
              <span className="step-num">{i + 1}</span> {s.label}
            </NavLink>
          </li>
        ))}
      </ol>
      <NavLink className="timeline-link" to={`/jobs/${jobId}/timeline`}>
        Timeline
      </NavLink>
    </nav>
  );
}

export function Layout() {
  const { jobId } = useParams();
  const me = useApi(api.me, "me");
  const isAdmin = me.status === "ready" && me.data.is_admin;
  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">List Uploader</span>
        <nav aria-label="Main">
          <NavLink to="/upload">Upload</NavLink>
          <NavLink to="/history">History</NavLink>
          {isAdmin && <NavLink to="/admin">Admin</NavLink>}
        </nav>
        <span className="whoami">{me.status === "ready" ? me.data.email : ""}</span>
      </header>
      {import.meta.env.VITE_API_MODE !== "live" && (
        <div className="mock-banner" role="note">
          Demo mode: all data is synthetic and nothing is sent anywhere.
        </div>
      )}
      <main>
        {jobId && <JobSteps jobId={jobId} />}
        <Outlet />
      </main>
    </div>
  );
}
