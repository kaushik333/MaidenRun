import { useEffect, useLayoutEffect, useState } from "react";
import DeployFeedbackStack from "./DeployFeedbackStack.jsx";
import DeployForm from "./DeployForm.jsx";
import DeploymentHistory from "./DeploymentHistory.jsx";
import { BACKEND_URL } from "./config";
import "./index.css";

const THEME_STORAGE_KEY = "agentdock-theme";

function getInitialTheme() {
  try {
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    // localStorage can be unavailable (private mode, disabled storage) —
    // fall through to the system preference for this load.
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function App() {
  const [status, setStatus] = useState("checking...");
  const [activeTab, setActiveTab] = useState("deploy");
  const [lastDeploy, setLastDeploy] = useState(null);
  const [feedbackDeploy, setFeedbackDeploy] = useState(null);
  const [theme, setTheme] = useState(getInitialTheme);
  const [historyVersion, setHistoryVersion] = useState(0);
  // Deployment ids terminated from *any* view — a CostMonitor card only
  // knows about a termination it triggered itself, so this is how one
  // terminated elsewhere (the Deployments tab, or the other deploy form)
  // reaches it too.
  const [terminatedIds, setTerminatedIds] = useState(() => new Set());

  function handleAgentDeploySuccess(result) {
    setLastDeploy(result);
    setHistoryVersion((v) => v + 1);
  }

  function handleFeedbackDeploySuccess(result) {
    setFeedbackDeploy(result);
    setHistoryVersion((v) => v + 1);
  }

  function handleTerminated(deploymentId) {
    setTerminatedIds((prev) => new Set(prev).add(deploymentId));
    setHistoryVersion((v) => v + 1);
  }

  // useLayoutEffect (not useEffect) so the attribute is set before the
  // browser paints — avoids a flash of the wrong theme on load.
  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  function toggleTheme() {
    const next = theme === "dark" ? "light" : "dark";
    setTheme(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      // Persistence is a convenience; the toggle still works for this tab.
    }
  }

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
      <button
        type="button"
        className="theme-toggle"
        onClick={toggleTheme}
        aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
      >
        {theme === "dark" ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M21 12.8A9 9 0 1 1 11.2 3a7.2 7.2 0 0 0 9.8 9.8Z"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinejoin="round"
            />
          </svg>
        ) : (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="12" cy="12" r="4.5" stroke="currentColor" strokeWidth="2" />
            <path
              d="M12 2.5v2.5M12 19v2.5M4.2 4.2l1.8 1.8M18 18l1.8 1.8M2.5 12H5M19 12h2.5M4.2 19.8 6 18M18 6l1.8-1.8"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
            />
          </svg>
        )}
      </button>
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
            <svg viewBox="0 0 100 100" width="46" height="46" fill="none">
              <path
                d="M40 22H24C18.477 22 14 26.477 14 32V68C14 73.523 18.477 78 24 78H40"
                stroke="var(--art-violet)"
                strokeWidth="9"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <path d="M49 50H33" stroke="var(--art-coral)" strokeWidth="9" strokeLinecap="round" />
              <circle cx="66" cy="50" r="17" fill="var(--art-coral)" />
            </svg>
          </div>
          <h1>AgentDock</h1>
          <p className="subtitle">
            Point us at your agent's repo and we'll deploy it to your own AWS account,
            with a shareable link and a feedback dashboard included.
          </p>
          <span className={`status-pill status-pill--${statusVariant}`}>
            <span className="status-dot" />
            backend: {status}
          </span>
        </header>

        <div className="segmented tab-bar" role="tablist" aria-label="Sections">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "deploy"}
            className={`segmented-option ${activeTab === "deploy" ? "is-active" : ""}`}
            onClick={() => setActiveTab("deploy")}
          >
            Deploy
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "feedback"}
            className={`segmented-option ${activeTab === "feedback" ? "is-active" : ""}`}
            onClick={() => setActiveTab("feedback")}
          >
            Feedback stack
            {feedbackDeploy && <span className="tab-dot" aria-hidden="true" />}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "history"}
            className={`segmented-option ${activeTab === "history" ? "is-active" : ""}`}
            onClick={() => setActiveTab("history")}
          >
            Deployments
          </button>
        </div>

        <div hidden={activeTab !== "deploy"}>
          <DeployForm
            onDeploySuccess={handleAgentDeploySuccess}
            onGoToFeedback={() => setActiveTab("feedback")}
            feedbackDeployed={Boolean(feedbackDeploy)}
            onTerminated={handleTerminated}
            terminatedIds={terminatedIds}
          />
        </div>
        <div hidden={activeTab !== "feedback"}>
          <DeployFeedbackStack
            onDeploySuccess={handleFeedbackDeploySuccess}
            lastAwsRegion={lastDeploy?.region ?? null}
            onTerminated={handleTerminated}
            terminatedIds={terminatedIds}
          />
        </div>
        <div hidden={activeTab !== "history"}>
          <DeploymentHistory refreshKey={historyVersion} onTerminated={handleTerminated} />
        </div>
      </div>
    </main>
  );
}
