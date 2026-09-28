import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { allRows, mockEdit } from "../mocks/analysisMock";
import { mockDecide } from "../mocks/enrichmentMock";
import { DEMO_JOB_ID } from "../mocks/fixtures";
import { ResultPage } from "../pages/ResultPage";
import { SendPage } from "../pages/SendPage";
import { server } from "./setup";

function renderAt(page: "send" | "result") {
  const router = createMemoryRouter(
    [
      { path: "/jobs/:jobId/send", element: <SendPage /> },
      { path: "/jobs/:jobId/result", element: <ResultPage pollIntervalMs={10} /> },
      { path: "/jobs/:jobId/analysis", element: <h1>Analysis page</h1> },
    ],
    { initialEntries: [`/jobs/${DEMO_JOB_ID}/${page}`] },
  );
  render(<RouterProvider router={router} />);
  return router;
}

/** Exclude every blocked row and decide the enrichment match, so the gate passes. */
function makeSendable() {
  for (const r of allRows()) if (r.issues.some((i) => i.severity === "blocking")) mockEdit(r.row_id, { excluded: true });
  mockDecide({ skip_all: true });
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

describe("review and send", () => {
  it("lists the gate's reasons and offers no Send button while it fails", async () => {
    renderAt("send");
    const blocked = await screen.findByRole("region", { name: "Blocking issues" });
    expect(within(blocked).getByText(/still have an issue to fix/)).toBeInTheDocument();
    expect(within(blocked).getByText(/waiting for Apply or Skip/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
  });

  it("shows what is and isn't sent (OQ-1 interim rule)", async () => {
    renderAt("send");
    await screen.findByText("What will be sent");
    expect(screen.getByText(/Not sent \(kept in the processed file\): .*Company/)).toBeInTheDocument();
    expect(screen.getByText(/Test environment/)).toBeInTheDocument();
  });

  it("confirms the summary and sends once, even on a double click", async () => {
    makeSendable();
    const bodies = capture("/api/jobs/:id/send");
    const router = renderAt("send");
    const confirm = await screen.findByRole("region", { name: "Confirm send" });
    expect(within(confirm).getByText("Send 2 leads to Eloqua for 2 campaigns.")).toBeInTheDocument();
    await userEvent.setup().dblClick(within(confirm).getByRole("button", { name: "Send" }));
    expect(bodies).toHaveLength(1);
    expect(bodies[0]).toMatchObject({ confirmation: { rows_to_send: 2 }, ui_gate_passed: true });
    await screen.findByRole("heading", { level: 1, name: "Result" });
    expect(router.state.location.pathname).toBe(`/jobs/${DEMO_JOB_ID}/result`);
  });

  it("shows the server's message when the send is refused", async () => {
    makeSendable();
    server.use(
      http.post("/api/jobs/:id/send", () =>
        HttpResponse.json({ message: "The rows to send changed since you loaded this page." }, { status: 409 }),
      ),
    );
    renderAt("send");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Send" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The rows to send changed");
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it("offers to re-check stale campaigns", async () => {
    let calls = 0;
    server.use(
      http.post("/api/jobs/:id/revalidate-campaigns", () => {
        calls += 1;
        return undefined;
      }),
    );
    server.use(
      http.get("/api/jobs/:id/gate", () =>
        HttpResponse.json({
          passed: false,
          reasons: ["Campaigns were last checked more than 24 hours ago."],
          reason_codes: ["CAMPAIGNS_STALE"],
          by_campaign: [],
          rows_to_send: 1,
          campaign_count: 1,
          excluded: 0,
          not_sent_fields: [],
          sent_fields: ["Email Address"],
          campaigns_stale: true,
          state: "ANALYSIS_REVIEW",
          can_send: true,
          send_to_prod: false,
        }),
      ),
    );
    renderAt("send");
    await userEvent.setup().click(await screen.findByRole("button", { name: "Re-check campaigns" }));
    expect(calls).toBe(1);
  });
});

describe("result", () => {
  it("polls while sending, then shows the failed row and retries only it", async () => {
    makeSendable();
    await fetch(`/api/jobs/${DEMO_JOB_ID}/gate`)
      .then((r) => r.json())
      .then((gate: { rows_to_send: number; by_campaign: { campaign_id: string; status: string; rows: number }[] }) =>
        fetch(`/api/jobs/${DEMO_JOB_ID}/send`, {
          method: "POST",
          body: JSON.stringify({
            confirmation: {
              rows_to_send: gate.rows_to_send,
              by_campaign: gate.by_campaign.map(({ campaign_id, status, rows }) => ({ campaign_id, status, rows })),
            },
          }),
        }),
      );
    renderAt("result");
    const failed = await screen.findByRole("region", { name: "Failed rows" });
    expect(within(failed).getByText(/HTTP 500 \(mock\)/)).toBeInTheDocument();
    const stats = screen.getByLabelText("Send summary");
    expect(within(stats).getByText("Submitted").parentElement).toHaveTextContent("1");

    await userEvent.setup().click(within(failed).getByRole("button", { name: "Retry failed rows" }));
    expect(await screen.findByText(/^2 leads submitted to Eloqua/)).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Failed rows" })).not.toBeInTheDocument();
  });

  it("downloads the processed file", async () => {
    server.use(
      http.get("/api/jobs/:id/download", () =>
        HttpResponse.json({ url: "data:text/csv,a", filename: "demo-processed.csv", expires_in: 300 }),
      ),
    );
    const clicked: string[] = [];
    const original = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function (this: HTMLAnchorElement) {
      clicked.push(this.download);
    };
    try {
      const router = createMemoryRouter([{ path: "/jobs/:jobId/result", element: <ResultPage /> }], {
        initialEntries: ["/jobs/j_01JDEMO0000000000000000001/result"],
      });
      render(<RouterProvider router={router} />);
      await userEvent.setup().click(await screen.findByRole("button", { name: "Download processed file" }));
      expect(clicked).toEqual(["demo-processed.csv"]);
    } finally {
      HTMLAnchorElement.prototype.click = original;
    }
  });
});
