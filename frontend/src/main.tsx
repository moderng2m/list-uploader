import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { completeSignIn, idToken, loadConfig, signIn } from "./auth";
import { routes } from "./routes";
import "./styles.css";

function render() {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <RouterProvider router={createBrowserRouter(routes)} />
    </StrictMode>,
  );
}

async function startLive() {
  await loadConfig();
  if (location.pathname === "/auth/callback") {
    const back = await completeSignIn(location.search);
    history.replaceState(null, "", back);
  }
  if (!(await idToken())) await signIn();
  render();
}

async function start() {
  // The build-time constant (not isLive()) lets the bundler drop the mock API from
  // live builds.
  if (import.meta.env.VITE_API_MODE === "live") {
    try {
      await startLive();
    } catch (err) {
      const root = document.getElementById("root")!;
      root.textContent = err instanceof Error ? err.message : String(err);
      const again = document.createElement("a");
      again.href = "/";
      again.textContent = " Start again";
      root.append(again);
    }
    return;
  }
  // Mock mode is the default: MSW answers the API in the browser.
  const { worker } = await import("./mocks/browser");
  await worker.start({ onUnhandledRequest: "bypass" });
  render();
}

void start();
