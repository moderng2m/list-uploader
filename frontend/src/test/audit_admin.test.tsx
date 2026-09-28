import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DEMO_JOB_ID } from "../mocks/fixtures";
import { routes } from "../routes";
import { server } from "./setup";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

async function openTab(name: string) {
  renderAt("/admin");
  await userEvent.setup().click(await screen.findByRole("tab", { name }));
}

describe("timeline", () => {
  it("lists job events in plain English and can include row events", async () => {
    renderAt(`/jobs/${DEMO_JOB_ID}/timeline`);
    expect(await screen.findByText("Upload started with enrichment on")).toBeInTheDocument();
    expect(screen.getByText("Moved from analyzing to analysis review")).toBeInTheDocument();
    expect(screen.queryByText(/^Row 3: issue found/)).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByLabelText(/Include per-row events/));
    expect(await screen.findByText(/^Row 3: issue found: CAMPAIGN_ID_FORMAT/)).toBeInTheDocument();
  });

  it("is linked from every job page and from history", async () => {
    renderAt(`/jobs/${DEMO_JOB_ID}/analysis`);
    expect(await screen.findByRole("link", { name: "Timeline" })).toHaveAttribute(
      "href",
      `/jobs/${DEMO_JOB_ID}/timeline`,
    );
  });
});

describe("row history drawer", () => {
  it("shows where each value came from", async () => {
    renderAt(`/jobs/${DEMO_JOB_ID}/analysis`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "History of row 2" }));
    const drawer = await screen.findByRole("dialog", { name: "History of row 2" });
    const lineage = await within(drawer).findByRole("list", { name: "SFDC Last Campaign ID lineage" });
    expect(within(lineage).getByText("'701000000000001'")).toBeInTheDocument();
    expect(within(lineage).getByText("'701000000000001AAA'")).toBeInTheDocument();
    const source = within(drawer).getByRole("list", { name: "Lead Source - Most Recent lineage" });
    expect(within(source).getByText(/corrected by rule/)).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("history", () => {
  it("lets admins switch to everyone's uploads", async () => {
    let all = false;
    server.use(
      http.get("/api/jobs", ({ request }) => {
        all = new URL(request.url).searchParams.get("all") === "true";
        return undefined;
      }),
    );
    renderAt("/history");
    await screen.findByText("demo_event_list.xlsx");
    await userEvent.setup().click(screen.getByRole("tab", { name: "Everyone's" }));
    await screen.findByText("Every upload.");
    expect(all).toBe(true);
  });
});

describe("admin", () => {
  it("adds, renames, reorders and deactivates lead sources (never deletes)", async () => {
    await openTab("Lead sources");
    const user = userEvent.setup();
    const panel = await screen.findByRole("region", { name: "Lead sources" });
    await user.type(within(panel).getByLabelText("New lead source"), "Marketing: Podcast");
    await user.click(within(panel).getByRole("button", { name: "Add" }));
    const item = await within(panel).findByRole("listitem", { name: "Marketing: Podcast" });

    await user.click(within(item).getByRole("button", { name: "Deactivate" }));
    expect(await within(panel).findByText("(deactivated)")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();

    await user.click(within(panel).getByRole("button", { name: "Move Marketing: Podcast up" }));
    const values = within(panel)
      .getAllByRole("listitem")
      .map((li) => li.getAttribute("aria-label"));
    expect(values.indexOf("Marketing: Podcast")).toBe(values.length - 2);

    await user.type(within(panel).getByLabelText("New lead source"), "marketing: EVENTS");
    await user.click(within(panel).getByRole("button", { name: "Add" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("already in the list");
  });

  it("shows a reload prompt when someone else changed the setting", async () => {
    server.use(
      http.post("/api/admin/lead-sources", () =>
        HttpResponse.json({ message: "Someone else changed this setting while you were editing." }, { status: 409 }),
      ),
    );
    await openTab("Lead sources");
    const user = userEvent.setup();
    const panel = await screen.findByRole("region", { name: "Lead sources" });
    await user.type(within(panel).getByLabelText("New lead source"), "X");
    await user.click(within(panel).getByRole("button", { name: "Add" }));
    const alert = await within(panel).findByRole("alert");
    expect(alert).toHaveTextContent("Someone else changed");
    expect(within(alert).getByRole("button", { name: "Reload" })).toBeInTheDocument();
  });

  it("edits thresholds and rejects a block below the flag", async () => {
    await openTab("Thresholds");
    const user = userEvent.setup();
    const block = await screen.findByLabelText("Block a value as junk at or above");
    await user.clear(block);
    await user.type(block, "0.5");
    await user.click(screen.getByRole("button", { name: "Save thresholds" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least the junk flag threshold");
    await user.clear(block);
    await user.type(block, "0.95");
    await user.click(screen.getByRole("button", { name: "Save thresholds" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved.");
  });

  it("promotes an AI match to an alias and edits aliases", async () => {
    await openTab("Field aliases");
    const user = userEvent.setup();
    const matches = await screen.findByRole("region", { name: "AI matches to keep" });
    await within(matches).findByText("Job Position");
    await user.click(within(matches).getByRole("button", { name: "Save as alias" }));
    const title = await screen.findByRole("list", { name: "Aliases for Title" });
    expect(await within(title).findByText("Job Position")).toBeInTheDocument();
    expect(await within(matches).findByText("Nothing to review.")).toBeInTheDocument();

    await user.click(within(title).getByRole("button", { name: "Remove position from Title" }));
    expect(within(screen.getByRole("list", { name: "Aliases for Title" })).queryByText("position")).toBeNull();

    await user.type(screen.getByLabelText("New alias for Company"), "work email");
    const companyRow = screen.getByLabelText("New alias for Company").closest("form")!;
    await user.click(within(companyRow).getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already maps to Email");
  });

  it("finds every upload that included a person and exports", async () => {
    await openTab("Audit search");
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Lead email"), "ADA@acme.example");
    await user.click(screen.getByRole("button", { name: "Search" }));
    const uploads = await screen.findByRole("region", { name: "Uploads with this person" });
    expect(within(uploads).getByText("demo_event_list.xlsx")).toBeInTheDocument();
    expect(screen.getAllByText(/^Row 2: /).length).toBeGreaterThan(0);

    const exports: unknown[] = [];
    server.use(
      http.post("/api/admin/audit/export", async ({ request }) => {
        exports.push(await request.json());
        return HttpResponse.json({ url: "data:text/csv,a", filename: "audit.csv", events: 2, truncated: false });
      }),
    );
    const original = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = () => undefined;
    try {
      await user.click(screen.getByRole("button", { name: "Export CSV" }));
      expect(await screen.findByText("Exported 2 events.")).toBeInTheDocument();
    } finally {
      HTMLAnchorElement.prototype.click = original;
    }
    expect(exports).toEqual([{ email: "ADA@acme.example" }]);
  });

  it("asks for a search term", async () => {
    await openTab("Audit search");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Search" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter at least one search term");
  });
});
