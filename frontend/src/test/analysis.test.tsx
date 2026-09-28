import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DEMO_JOB_ID, jobs } from "../mocks/fixtures";
import { routes } from "../routes";
import { server } from "./setup";

const path = `/jobs/${DEMO_JOB_ID}/analysis`;

function renderAnalysis() {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

function captureJson(method: "patch" | "post", url: string) {
  const bodies: unknown[] = [];
  server.use(
    http[method](url, async ({ request }) => {
      bodies.push(await request.clone().json());
      return undefined; // fall through to the default mock handler
    }),
  );
  return bodies;
}

const rowOf = (id: number) => document.querySelector<HTMLElement>(`tr[data-row-id="${id}"]`)!;
const issuesOf = (id: number) => document.querySelector<HTMLElement>(`tr[data-issues-for="${id}"]`)!;

describe("analysis review", () => {
  it("shows summary, grouped issues, campaigns and rows", async () => {
    renderAnalysis();
    expect(await screen.findByText("CAMPAIGN_NOT_FOUND")).toBeInTheDocument();
    const stats = screen.getByLabelText("Row summary");
    expect(within(stats).getByText("8")).toBeInTheDocument();
    const campaigns = screen.getByLabelText("Campaigns");
    expect(within(campaigns).getByText("Campaign not found").closest("article")).toHaveClass("invalid");
    expect(within(campaigns).getByText("Inactive")).toBeInTheDocument();
    // Source vs processed side by side.
    expect(within(await screen.findByRole("table")).getByText("was: Events")).toBeInTheDocument();
    expect(screen.getByText(/Enrichment will look up 3 contacts in ZoomInfo/)).toBeInTheDocument();
  });

  it("filters rows by issue", async () => {
    renderAnalysis();
    const group = (await screen.findByText("DUPLICATE_IN_FILE")).closest("li")!;
    await userEvent.setup().click(within(group).getByRole("button", { name: "Show rows" }));
    await screen.findByRole("button", { name: "DUPLICATE_IN_FILE ✕" });
    expect(screen.getAllByRole("button", { name: /^Edit row/ })).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Edit row 5" })).toBeInTheDocument();
  });

  it("edits a row and sends only the changed field", async () => {
    const bodies = captureJson("patch", "/api/jobs/:id/rows/:rowId");
    renderAnalysis();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit row 3" }));
    const input = screen.getByLabelText("Campaign ID for row 3");
    await user.clear(input);
    await user.type(input, "701000000000001AAA");
    await user.click(within(rowOf(3)).getByRole("button", { name: "Save" }));
    expect(bodies).toEqual([{ processed: { campaign_id: "701000000000001AAA" } }]);
    expect(await within(rowOf(3)).findByText("Ready")).toBeInTheDocument();
  });

  it("excludes a row", async () => {
    const bodies = captureJson("patch", "/api/jobs/:id/rows/:rowId");
    renderAnalysis();
    await userEvent.setup().click(await screen.findByLabelText("Exclude row 9"));
    expect(bodies).toEqual([{ excluded: true }]);
    expect(await within(rowOf(9)).findByText("Excluded")).toBeInTheDocument();
  });

  it("accepts a lead source suggestion and fixes a status for all matching rows", async () => {
    const patches = captureJson("patch", "/api/jobs/:id/rows/:rowId");
    const bulks = captureJson("post", "/api/jobs/:id/bulk-actions");
    renderAnalysis();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Accept “Marketing: Webinar”" }));
    expect(patches).toEqual([{ processed: { lead_source: "Marketing: Webinar" } }]);
    await user.click(await screen.findByRole("button", { name: "Use for all rows with “Atended”" }));
    expect(bulks).toEqual([
      { action: "set_status", params: { campaign_id: "701000000000002AAA", value: "Atended", status: "Attended" } },
    ]);
    expect(await within(rowOf(7)).findByText("Ready")).toBeInTheDocument();
  });

  it("runs a bulk action from the issue list", async () => {
    const bulks = captureJson("post", "/api/jobs/:id/bulk-actions");
    renderAnalysis();
    await userEvent.setup().click(await screen.findByRole("button", { name: "Exclude duplicate rows" }));
    expect(bulks).toEqual([{ action: "exclude_duplicates", params: {} }]);
    expect(await within(rowOf(5)).findByText("Excluded")).toBeInTheDocument();
  });

  it("clears a junk flag", async () => {
    const bodies = captureJson("patch", "/api/jobs/:id/rows/:rowId");
    renderAnalysis();
    await screen.findByRole("button", { name: "Edit row 6" });
    await userEvent.setup().click(within(issuesOf(6)).getAllByRole("button", { name: "Clear flag" })[0]!);
    expect(bodies).toEqual([{ dismiss: "VALUE_JUNK:company" }]);
  });

  it("shows server errors from an action", async () => {
    server.use(
      http.patch("/api/jobs/:id/rows/:rowId", () =>
        HttpResponse.json({ message: "Rows can only be changed while you're reviewing the analysis." }, { status: 409 }),
      ),
    );
    renderAnalysis();
    await userEvent.setup().click(await screen.findByLabelText("Exclude row 9"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Rows can only be changed");
  });
});

describe("analysis job states", () => {
  const demo = jobs[0]!;

  it("shows progress while analyzing, then the results", async () => {
    let polls = 0;
    server.use(
      http.get("/api/jobs/:id", () => {
        polls += 1;
        return HttpResponse.json({ ...demo, state: polls < 2 ? "ANALYZING" : "ANALYSIS_REVIEW" });
      }),
    );
    const router = createMemoryRouter(
      [{ path: "/jobs/:jobId/analysis", element: <AnalysisPageFast /> }],
      { initialEntries: [path] },
    );
    render(<RouterProvider router={router} />);
    expect(await screen.findByText("Analyzing your file…")).toBeInTheDocument();
    expect(await screen.findByText("CAMPAIGN_NOT_FOUND")).toBeInTheDocument();
  });

  it("offers a retry after a failure", async () => {
    let started = false;
    server.use(
      http.get("/api/jobs/:id", () =>
        HttpResponse.json({
          ...demo,
          state: started ? "ANALYSIS_REVIEW" : "FAILED",
          last_error: { stage: "analysis", message: "Something went wrong while analyzing this file." },
        }),
      ),
      http.post("/api/jobs/:id/analyze", () => {
        started = true;
        return HttpResponse.json({ state: "ANALYZING" }, { status: 202 });
      }),
    );
    renderAnalysis();
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong while analyzing");
    await userEvent.setup().click(screen.getByRole("button", { name: "Run analysis again" }));
    expect(await screen.findByText("CAMPAIGN_NOT_FOUND")).toBeInTheDocument();
  });

  it("sends you back to mapping if analysis hasn't run", async () => {
    server.use(http.get("/api/jobs/:id", () => HttpResponse.json({ ...demo, state: "MAPPING_REVIEW" })));
    renderAnalysis();
    expect(await screen.findByRole("link", { name: "Confirm the column mapping first" })).toBeInTheDocument();
  });
});

import { AnalysisPage } from "../pages/AnalysisPage";

function AnalysisPageFast() {
  return <AnalysisPage pollIntervalMs={10} />;
}
