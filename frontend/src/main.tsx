import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { initColors } from "./lib/colors";
import { initialize } from "./i18n";
import { featureLocales } from "./feature-locales";
import "./index.css";

const ready = initialize(featureLocales);
// The colors cached from the last visit, before anything is painted.
initColors();

const client = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 5_000, refetchOnWindowFocus: true },
  },
});
const root = ReactDOM.createRoot(document.getElementById("root")!);
// Initial translations load before the application renders. Keep a neutral
// skeleton visible while the selected resource chunk travels over the network.
root.render(<div className="m-6 space-y-4" aria-busy="true">
  <div className="h-10 max-w-md animate-pulse rounded-lg bg-muted" />
  <div className="h-72 animate-pulse rounded-lg bg-muted" />
</div>);
void ready.then(() => root.render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
));
