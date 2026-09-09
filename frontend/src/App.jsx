import { useEffect, useState } from "react";
import DeployForm from "./DeployForm.jsx";
import { BACKEND_URL } from "./config";
import "./index.css";

export default function App() {
  const [status, setStatus] = useState("checking...");

  useEffect(() => {
    fetch(`${BACKEND_URL}/health`)
      .then((res) => res.json())
      .then((data) => setStatus(data.status))
      .catch(() => setStatus("unreachable"));
  }, []);

  const statusVariant =
    status === "ok" ? "ok" : status === "checking..." ? "pending" : "error";

  return (
    <main className="page">
      <div className="art-bg" aria-hidden="true">
        <span className="blob blob--a" />
        <span className="blob blob--b" />
        <span className="blob blob--c" />
        <span className="blob blob--d" />
      </div>
      <div className="grain" aria-hidden="true" />
      <div className="shell">
        <header className="shell-header">
          <div className="logo-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <h1>Agent Feedback Deploy</h1>
          <p className="subtitle">
            Point us at your agent's repo and we'll deploy it to your own AWS account,
            with a shareable link and a feedback dashboard included.
          </p>
          <span className={`status-pill status-pill--${statusVariant}`}>
            <span className="status-dot" />
            backend: {status}
          </span>
        </header>
        <DeployForm />
      </div>
    </main>
  );
}
