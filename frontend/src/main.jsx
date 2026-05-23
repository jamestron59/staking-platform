import React from "react";
import ReactDOM from "react-dom/client";
import { Toaster } from "react-hot-toast";
import App from "./App";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
    <Toaster position="top-right" toastOptions={{
      style: { background: "#1e2035", color: "#fff", border: "1px solid #2d3055" }
    }} />
  </React.StrictMode>
);
