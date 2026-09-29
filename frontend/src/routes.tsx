import { Navigate, type RouteObject } from "react-router-dom";
import { Layout } from "./components/Layout";
import { AdminPage } from "./pages/AdminPage";
import { AnalysisPage } from "./pages/AnalysisPage";
import { EnrichmentPage } from "./pages/EnrichmentPage";
import { HistoryPage } from "./pages/HistoryPage";
import { MappingPage } from "./pages/MappingPage";
import { ResultPage } from "./pages/ResultPage";
import { SendPage } from "./pages/SendPage";
import { TimelinePage } from "./pages/TimelinePage";
import { UploadPage } from "./pages/UploadPage";

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to="/upload" replace /> },
      { path: "upload", element: <UploadPage /> },
      { path: "history", element: <HistoryPage /> },
      { path: "admin", element: <AdminPage /> },
      {
        path: "jobs/:jobId",
        children: [
          { index: true, element: <Navigate to="analysis" replace /> },
          { path: "mapping", element: <MappingPage /> },
          { path: "analysis", element: <AnalysisPage /> },
          { path: "enrichment", element: <EnrichmentPage /> },
          { path: "send", element: <SendPage /> },
          { path: "result", element: <ResultPage /> },
          { path: "timeline", element: <TimelinePage /> },
        ],
      },
      { path: "*", element: <Navigate to="/upload" replace /> },
    ],
  },
];
