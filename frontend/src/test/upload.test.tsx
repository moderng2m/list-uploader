import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { MOCK_UPLOAD_URL } from "../mocks/handlers";
import { routes } from "../routes";
import { server } from "./setup";

function renderUpload() {
  const router = createMemoryRouter(routes, { initialEntries: ["/upload"] });
  render(<RouterProvider router={router} />);
  return router;
}

async function chooseAndSubmit(file: File) {
  const user = userEvent.setup({ applyAccept: false });
  await screen.findByRole("heading", { name: "Upload a lead list" });
  await user.upload(screen.getByLabelText("File"), file);
  await user.click(screen.getByRole("button", { name: "Upload and continue" }));
  return user;
}

describe("upload flow", () => {
  it("uploads, waits for parsing, and lands on mapping with file details", async () => {
    const router = renderUpload();
    await chooseAndSubmit(new File(["Company\nAcme\n"], "leads.xlsx"));
    expect(await screen.findByRole("heading", { name: "Map your columns" })).toBeInTheDocument();
    expect(router.state.location.pathname).toMatch(/^\/jobs\/j_MOCK\d+\/mapping$/);
    const details = await screen.findByLabelText("File details");
    expect(details).toHaveTextContent("leads.xlsx: 6 rows and 7 columns read from sheet “Sheet1”");
    expect(details).toHaveTextContent("Cell E2 contained the Excel error #VALUE!");
  });

  it("shows the parser's message when the file can't be read", async () => {
    const router = renderUpload();
    await chooseAndSubmit(new File(["Company\n"], "broken.csv"));
    expect(await screen.findByRole("alert")).toHaveTextContent("header row but no data rows");
    expect(router.state.location.pathname).toBe("/upload");
    expect(screen.getByRole("button", { name: "Upload and continue" })).toBeEnabled();
  });

  it("rejects a wrong file type before contacting the server", async () => {
    let created = false;
    server.use(
      http.post("/api/jobs", () => {
        created = true;
        return HttpResponse.json({}, { status: 500 });
      }),
    );
    renderUpload();
    await chooseAndSubmit(new File(["%PDF"], "leads.pdf"));
    expect(await screen.findByRole("alert")).toHaveTextContent("This file is a .pdf");
    expect(created).toBe(false);
  });

  const s3Error = (code: string) =>
    new HttpResponse(`<?xml version="1.0"?><Error><Code>${code}</Code><Message>m</Message></Error>`, {
      status: 400,
      headers: { "content-type": "application/xml" },
    });

  it("says when the file is too large", async () => {
    server.use(http.post(MOCK_UPLOAD_URL, () => s3Error("EntityTooLarge")));
    renderUpload();
    await chooseAndSubmit(new File(["Company\nAcme\n"], "leads.csv"));
    expect(await screen.findByRole("alert")).toHaveTextContent("over 10 MB");
  });

  it("shows storage's reason for any other refusal", async () => {
    server.use(http.post(MOCK_UPLOAD_URL, () => s3Error("InvalidArgument")));
    renderUpload();
    await chooseAndSubmit(new File(["Company\nAcme\n"], "leads.csv"));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Storage refused the upload (InvalidArgument)");
    expect(alert).not.toHaveTextContent("10 MB");
  });

  it("new uploads and parse failures appear in history", async () => {
    renderUpload();
    await chooseAndSubmit(new File(["Company\nAcme\n"], "fresh_list.csv"));
    await screen.findByRole("heading", { name: "Map your columns" });
    const router = createMemoryRouter(routes, { initialEntries: ["/history"] });
    render(<RouterProvider router={router} />);
    expect(await screen.findByText("fresh_list.csv")).toBeInTheDocument();
    expect(screen.getByText("empty_export.csv")).toBeInTheDocument();
    expect(screen.getAllByText(/header row but no data rows/).length).toBeGreaterThan(0);
  });
});
