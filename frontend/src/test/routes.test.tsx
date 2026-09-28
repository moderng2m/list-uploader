import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DEMO_JOB_ID } from "../mocks/fixtures";
import { validateFile } from "../pages/UploadPage";
import { routes } from "../routes";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

const job = (step: string) => `/jobs/${DEMO_JOB_ID}/${step}`;

describe("every route renders with mock data", () => {
  it.each([
    ["/upload", "Upload a lead list", "Enrich leads with ZoomInfo"],
    [job("mapping"), "Map your columns", "Job Position"],
    [job("analysis"), "Analyze and fix", "Demo Conference 2026"],
    [job("enrichment"), "Enrichment results", "ZoomInfo shows a different current employer"],
    [job("send"), "Review and send", "You can't send yet"],
    [job("result"), "Result", "3 leads submitted to Eloqua"],
    ["/history", "Upload history", "demo_event_list.xlsx"],
    ["/admin", "Admin", "Marketing: Webinar"],
  ])("%s", async (path, heading, content) => {
    renderAt(path);
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(await screen.findByText(content, { exact: false })).toBeInTheDocument();
  });

  it("redirects / to upload", async () => {
    const router = renderAt("/");
    expect(await screen.findByRole("heading", { name: "Upload a lead list" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/upload");
  });
});

describe("mapping", () => {
  it("shows method badges including AI confidence", async () => {
    renderAt(job("mapping"));
    expect(await screen.findByText("AI suggested (87%)")).toBeInTheDocument();
    expect(screen.getAllByText("Exact").length).toBeGreaterThan(0);
    expect(screen.getByText("Alias")).toBeInTheDocument();
  });

  it("blocks Continue while a required field is unmapped, and swaps on reuse", async () => {
    renderAt(job("mapping"));
    // SFDC List Name is required and unmapped in the fixture.
    const continueLink = await screen.findByRole("link", { name: "Continue" });
    expect(continueLink).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByText("SFDC List Name")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("Target for Badge Color"), "email");
    expect(screen.getByLabelText<HTMLSelectElement>("Target for E-mail").value).toBe("");
  });
});

describe("upload validation", () => {
  it("rejects wrong extensions with a plain-English message", () => {
    const msg = validateFile(new File(["x"], "leads.pdf"));
    expect(msg).toMatch(/This file is a \.pdf\. Upload a \.csv or \.xlsx file/);
  });

  it("rejects empty files", () => {
    expect(validateFile(new File([], "leads.csv"))).toMatch(/empty/);
  });

  it("accepts xlsx", () => {
    expect(validateFile(new File(["x"], "Leads.XLSX"))).toBeNull();
  });
});
