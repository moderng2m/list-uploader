import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { DEMO_JOB_ID, mapping } from "../mocks/fixtures";
import { routes } from "../routes";
import { server } from "./setup";

const path = `/jobs/${DEMO_JOB_ID}/mapping`;

function renderMapping() {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

const confirmButton = () => screen.findByRole("button", { name: "Confirm mapping and continue" });

describe("mapping screen", () => {
  it("shows method badges, samples and the AI reason", async () => {
    renderMapping();
    const ai = await screen.findByText("AI suggested (87%)");
    expect(ai.closest("td")).toHaveAttribute("title", "Header and values look like job titles.");
    expect(screen.getAllByText("Exact").length).toBe(4);
    expect(screen.getByText("Alias")).toBeInTheDocument();
    expect(screen.getByText("ada@example.com · grace@example.org · alan@example.net")).toBeInTheDocument();
  });

  it("lists fields that will be filled automatically", async () => {
    renderMapping();
    await confirmButton();
    expect(screen.getByText(/SFDC List Name: generated from campaign name/)).toBeInTheDocument();
    expect(screen.getByText(/Lead Source - Most Recent: derived from the Salesforce/)).toBeInTheDocument();
  });

  it("confirms and moves on to analysis", async () => {
    const router = renderMapping();
    let sent: unknown;
    server.use(
      http.put("/api/jobs/:id/mapping", async ({ request }) => {
        sent = await request.json();
        return HttpResponse.json({ ...mapping, confirmed: true });
      }),
    );
    await userEvent.setup().click(await confirmButton());
    await screen.findByRole("heading", { name: "Analyze and fix" });
    expect(router.state.location.pathname).toBe(`/jobs/${DEMO_JOB_ID}/analysis`);
    expect(sent).toEqual({
      columns: mapping.columns.map((c) => ({ source_header: c.source_header, field_key: c.field_key })),
    });
  });

  it("disables confirm while a must-map field is unmapped", async () => {
    renderMapping();
    const user = userEvent.setup();
    await confirmButton();
    await user.selectOptions(screen.getByLabelText("Target for Company"), "");
    const missing = screen.getByRole("list", { name: "Required fields not yet mapped" });
    expect(within(missing).getByText("Company")).toBeInTheDocument();
    expect(await confirmButton()).toBeDisabled();
  });

  it("marks your own choice, restores the suggestion badge, and keeps one-to-one", async () => {
    renderMapping();
    const user = userEvent.setup();
    await confirmButton();
    const badge = screen.getByLabelText("Target for Badge Color");
    await user.selectOptions(badge, "notes");
    expect(within(badge.closest("tr")!).getByText("Chosen by you")).toBeInTheDocument();

    // Taking a field from another column clears it there.
    await user.selectOptions(badge, "email");
    expect(screen.getByLabelText<HTMLSelectElement>("Target for E-mail").value).toBe("");

    // Putting the suggestion back restores its original badge.
    await user.selectOptions(screen.getByLabelText("Target for E-mail"), "email");
    expect(within(screen.getByLabelText("Target for E-mail").closest("tr")!).getByText("Alias")).toBeInTheDocument();
  });

  it("shows the server's validation message", async () => {
    server.use(
      http.put("/api/jobs/:id/mapping", () =>
        HttpResponse.json({ message: "Map these required fields before you continue: Company." }, { status: 400 }),
      ),
    );
    renderMapping();
    await userEvent.setup().click(await confirmButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("Map these required fields");
  });

  it("shows the AI note when suggestions weren't available", async () => {
    server.use(
      http.get("/api/jobs/:id/mapping", () =>
        HttpResponse.json({ ...mapping, ai_note: "Automatic suggestions weren't available for this file." }),
      ),
    );
    renderMapping();
    expect(await screen.findByText(/Automatic suggestions weren't available/)).toHaveAttribute("role", "note");
  });

  it("is read-only once analysis has started", async () => {
    server.use(
      http.get("/api/jobs/:id/mapping", () =>
        HttpResponse.json({ ...mapping, editable: false, confirmed: true }),
      ),
    );
    renderMapping();
    expect(await screen.findByText("This mapping is confirmed and can no longer be changed.")).toBeInTheDocument();
    expect(screen.getByLabelText("Target for Company")).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Confirm mapping/ })).toBeNull();
  });
});
