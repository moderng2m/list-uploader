import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DEMO_JOB_ID, jobs } from "../mocks/fixtures";
import { AnalysisPage } from "../pages/AnalysisPage";
import { EnrichmentPage } from "../pages/EnrichmentPage";
import { server } from "./setup";

const demo = jobs[0]!;

function renderAt(page: "enrichment" | "analysis", jobState: string) {
  server.use(http.get("/api/jobs/:id", () => HttpResponse.json({ ...demo, state: jobState })));
  const element = page === "enrichment" ? <EnrichmentPage pollIntervalMs={10} /> : <AnalysisPage pollIntervalMs={10} />;
  const router = createMemoryRouter(
    [
      { path: "/jobs/:jobId/enrichment", element: <EnrichmentPage pollIntervalMs={10} /> },
      { path: "/jobs/:jobId/analysis", element },
      { path: "/jobs/:jobId/send", element: <h1>Send page</h1> },
    ],
    { initialEntries: [`/jobs/${DEMO_JOB_ID}/${page}`] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

function capture(url: string) {
  const bodies: unknown[] = [];
  server.use(
    http.post(url, async ({ request }) => {
      bodies.push(await request.clone().json());
      return undefined;
    }),
  );
  return bodies;
}

describe("enrichment review", () => {
  it("shows the summary, a side-by-side comparison, conflicts and filled values", async () => {
    renderAt("enrichment", "ENRICHMENT_REVIEW");
    const stats = await screen.findByLabelText("Enrichment summary");
    expect(within(stats).getByText("Needs review")).toBeInTheDocument();
    const item = screen.getByRole("article", { name: "Row 4" });
    expect(within(item).getByText("Hooli XYZ Demo")).toBeInTheDocument();
    expect(within(item).getByText("ZoomInfo shows a different current employer.")).toBeInTheDocument();
    expect(within(item).getByText(/Apply would fill: LinkedIn/)).toBeInTheDocument();
    expect(screen.getByText("Pied Piper Demo")).toBeInTheDocument();
  });

  it("defaults to Skip and saves the chosen decisions", async () => {
    const bodies = capture("/api/jobs/:id/enrichment-decisions");
    renderAt("enrichment", "ENRICHMENT_REVIEW");
    const user = userEvent.setup();
    const group = await screen.findByRole("radiogroup", { name: "Decision for row 4" });
    expect(within(group).getByLabelText("Skip")).toBeChecked();
    await user.click(within(group).getByLabelText("Apply"));
    await user.click(screen.getByRole("button", { name: "Save decisions" }));
    expect(bodies).toEqual([{ decisions: [{ row_id: 4, decision: "apply" }] }]);
    expect(await screen.findByText("decided: apply")).toBeInTheDocument();
  });

  it("skips all undecided matches", async () => {
    const bodies = capture("/api/jobs/:id/enrichment-decisions");
    renderAt("enrichment", "ENRICHMENT_REVIEW");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Skip all" }));
    expect(bodies).toEqual([{ skip_all: true }]);
  });

  it("shows service errors", async () => {
    server.use(
      http.get("/api/jobs/:id/enrichment", async () => {
        const { enrichment } = await import("../mocks/fixtures");
        return HttpResponse.json({ ...enrichment, errors: 4 });
      }),
    );
    renderAt("enrichment", "ENRICHMENT_REVIEW");
    expect(await screen.findByText(/4 contacts couldn't be enriched \(service error\)/)).toBeInTheDocument();
  });
});

describe("enrichment job states", () => {
  it("shows progress while enriching", async () => {
    renderAt("enrichment", "ENRICHING");
    expect(await screen.findByText("Looking up contacts in ZoomInfo…")).toBeInTheDocument();
  });

  it("offers a retry after an enrichment failure", async () => {
    let started = false;
    server.use(
      http.get("/api/jobs/:id", () =>
        HttpResponse.json({
          ...demo,
          state: started ? "ENRICHMENT_REVIEW" : "FAILED",
          last_error: { stage: "enrichment", message: "Something went wrong while enriching this file." },
        }),
      ),
      http.post("/api/jobs/:id/enrich", () => {
        started = true;
        return HttpResponse.json({ state: "ENRICHING" }, { status: 202 });
      }),
    );
    const router = createMemoryRouter([{ path: "/jobs/:jobId/enrichment", element: <EnrichmentPage pollIntervalMs={10} /> }], {
      initialEntries: [`/jobs/${DEMO_JOB_ID}/enrichment`],
    });
    render(<RouterProvider router={router} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong while enriching");
    await userEvent.setup().click(screen.getByRole("button", { name: "Try enriching again" }));
    expect(await screen.findByRole("article", { name: "Row 4" })).toBeInTheDocument();
  });

  it("starts enrichment from the analysis screen", async () => {
    let started = false;
    server.use(
      http.post("/api/jobs/:id/enrich", () => {
        started = true;
        return HttpResponse.json({ state: "ENRICHING" }, { status: 202 });
      }),
    );
    const router = renderAt("analysis", "ANALYSIS_REVIEW");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Next: Enrich" }));
    expect(started).toBe(true);
    await screen.findByText(/Enrichment results|Looking up contacts/);
    expect(router.state.location.pathname).toBe(`/jobs/${DEMO_JOB_ID}/enrichment`);
  });

  it("goes straight to Review & Send when enrichment is off", async () => {
    server.use(http.get("/api/jobs/:id/analysis", async () => {
      const { analysis } = await import("../mocks/fixtures");
      return HttpResponse.json({ ...analysis, enrich: false });
    }));
    renderAt("analysis", "ANALYSIS_REVIEW");
    expect(await screen.findByRole("link", { name: "Next: Review & Send" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Next: Enrich" })).toBeNull();
  });
});
