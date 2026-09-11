import { useEffect, useState } from "react";
import { BACKEND_URL } from "./config";

const DECISION_LABELS = {
  repo_url: "Repo",
  llm_provider: "LLM provider",
  instance_type: "Instance type",
  ebs_size_gb: "EBS size",
  self_hosted_llm: "Self-hosted LLM",
};

function formatRelative(iso) {
  if (!iso) return "";
  const diffMs = Date.now() - new Date(iso).getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function DecisionsList({ decisions }) {
  const entries = Object.entries(decisions || {}).filter(([key]) => DECISION_LABELS[key]);
  const envKeys = decisions?.env_var_keys;

  if (entries.length === 0 && !envKeys?.length) return null;

  return (
    <details className="history-decisions">
      <summary>Deploy settings</summary>
      <ul>
        {entries.map(([key, value]) => (
          <li key={key}>
            {DECISION_LABELS[key]}: {String(value)}
          </li>
        ))}
        {envKeys?.length > 0 && <li>Env vars: {envKeys.join(", ")}</li>}
      </ul>
    </details>
  );
}

function DeploymentRow({ record, onChanged }) {
  const [confirming, setConfirming] = useState(false);
  const [terminating, setTerminating] = useState(false);
  const [error, setError] = useState(null);

  async function confirmTerminate() {
    setTerminating(true);
    setError(null);

    let res;
    try {
      res = await fetch(`${BACKEND_URL}/terminate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          instance_id: record.instance_id,
          region: record.region,
          deployment_id: record.deployment_id,
        }),
      });
    } catch {
      setError("Could not reach the backend");
      setTerminating(false);
      return;
    }

    let data;
    try {
      data = await res.json();
    } catch {
      data = null;
    }

    if (!res.ok) {
      setError(data?.detail || "Terminate failed");
      setTerminating(false);
      return;
    }

    setTerminating(false);
    setConfirming(false);
    onChanged();
  }

  return (
    <div className="history-row">
      <div className="history-row-main">
        <span className="history-kind-badge" data-kind={record.kind}>
          {record.kind === "agent" ? "Agent" : "Feedback"}
        </span>
        <div className="history-row-info">
          <span className="history-row-name">{record.instance_name || record.instance_id}</span>
          <span className="history-row-meta">
            {record.region} · deployed {formatRelative(record.deployed_at)}
            {record.status === "terminated" && ` · terminated ${formatRelative(record.terminated_at)}`}
          </span>
        </div>
      </div>

      <div className="history-row-links">
        {record.links.map((link) => (
          <a key={link.url} className="link-chip" href={link.url} target="_blank" rel="noreferrer">
            {link.label}
          </a>
        ))}
      </div>

      {record.cost_estimate_usd != null && (
        <div className="history-row-cost">
          <span className="cost-stat-label">{record.status === "running" ? "Cost so far" : "Total cost"}</span>
          <span className="cost-stat-value cost-stat-value--highlight">
            ${record.cost_estimate_usd.toFixed(4)}
          </span>
        </div>
      )}

      <DecisionsList decisions={record.decisions} />

      {record.status === "running" && !confirming && (
        <button type="button" className="terminate-btn" onClick={() => setConfirming(true)}>
          Terminate
        </button>
      )}

      {record.status === "running" && confirming && (
        <div className="terminate-confirm-row">
          <span className="terminate-confirm-text">
            Terminate <strong>{record.instance_name || record.instance_id}</strong>? This deletes the EC2
            instance — it can't be undone.
          </span>
          <div className="terminate-confirm-actions">
            <button
              type="button"
              className="terminate-btn terminate-btn--danger"
              disabled={terminating}
              onClick={confirmTerminate}
            >
              {terminating ? "Terminating…" : "Confirm terminate"}
            </button>
            <button type="button" className="link-btn" disabled={terminating} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
          {error && <p className="terminate-error">{error}</p>}
        </div>
      )}

      {record.status === "terminated" && <span className="history-terminated-tag">Terminated</span>}
    </div>
  );
}

export default function DeploymentHistory({ refreshKey }) {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  function load() {
    setLoading(true);
    fetch(`${BACKEND_URL}/deployments`)
      .then((res) => res.json())
      .then((data) => {
        setRecords(data);
        setError(null);
      })
      .catch(() => setError("Could not load deployment history"))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [refreshKey]);

  const running = records.filter((r) => r.status === "running");
  const terminated = records.filter((r) => r.status === "terminated");

  return (
    <div className="card-wrap">
      <div className="card">
        <div className="form">
          <section className="form-section">
            <div className="section-heading">
              <h2 className="section-title">Deployments</h2>
              <button type="button" className="link-btn" onClick={load}>
                Refresh
              </button>
            </div>
            {loading && records.length === 0 && <p className="section-hint">Loading…</p>}
            {error && <p className="field-status field-status--error">{error}</p>}
            {!loading && !error && records.length === 0 && (
              <p className="section-hint">Nothing deployed yet from this backend.</p>
            )}
          </section>

          {running.length > 0 && (
            <section className="form-section">
              <h2 className="section-title">Running</h2>
              <div className="history-list">
                {running.map((r) => (
                  <DeploymentRow key={r.deployment_id} record={r} onChanged={load} />
                ))}
              </div>
            </section>
          )}

          {terminated.length > 0 && (
            <section className="form-section">
              <h2 className="section-title">Terminated</h2>
              <div className="history-list">
                {terminated.map((r) => (
                  <DeploymentRow key={r.deployment_id} record={r} onChanged={load} />
                ))}
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
