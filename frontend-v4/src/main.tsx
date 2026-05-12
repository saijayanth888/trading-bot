import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "@/App";
import { queryClient } from "@/lib/query";
import "@/styles/globals.css";

// Apply persisted theme synchronously to avoid a flash of wrong palette.
const persistedTheme = (() => {
  try {
    return window.localStorage.getItem("quanta_v4_theme") || "dark";
  } catch {
    return "dark";
  }
})();
document.documentElement.dataset.theme = persistedTheme;

const root = document.getElementById("root");
if (!root) throw new Error("V4 shell: #root not found");

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename="/v4">
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
