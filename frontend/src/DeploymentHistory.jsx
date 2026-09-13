import { useEffect, useState } from "react";
import { BACKEND_URL } from "./config";

// Mirrors backend/app/api/deploy.py's AWS_REGIONS — same curated list,
// duplicated the same way DeployForm.jsx and DeployFeedbackStack.jsx do.
const AWS_REGIONS = [
  "us-east-1",
  "us-east-2",
  "us-west-2",
  "eu-west-1",
  "eu-central-1",
  "ap-southeast-1",
  "ap-southeast-2",
  "ap-northeast-1",
  "me-central-1",
];

const ACTIVE_POLL_INTERVAL_MS = 3000;

const DECISION_LABELS = {
  repo_url: "Repo",
  private_repo: "Private repo",
  api_key_name: "API key env var",
  base_url_name: "Base URL env var",
  instance_type: "Instance type",
  ebs_size_gb: "EBS size",
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

function formatTimestamp(iso) {
  if (!iso) return "";
  const absolute = new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  return `${absolute} (${formatRelative(iso)})`;
}

function DecisionsList({ decisions }) {
  const entries = Object.entries(decisions || {}).filter(
    ([key, value]) => DECISION_LABELS[key] && value != null && value !== false
  );
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

function DeploymentRow({ record, onChanged, onTerminated }) {
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
    // Tells App.jsx so a CostMonitor showing this same deployment elsewhere
    // (the Deploy or Feedback stack tab) flips to "terminated" too, instead
    // of continuing to poll cost-estimate as if it were still running.
    onTerminated?.(record.deployment_id);
  }

  return (
    <div className="history-row">
      <div className="history-row-main">
        <span className="history-kind-badge" data-kind={record.kind}>
          {record.kind === "agent" ? "Agent" : "Feedback"}
        </span>
        <div className="history-row-info">
          <span className="history-row-name">{record.instance_name || record.instance_id}</span>
          <span className="history-row-meta">{record.region}</span>
        </div>
      </div>

      <div className="history-row-times">
        <div className="cost-stat">
          <span className="cost-stat-label">Created at</span>
          <span className="cost-stat-value">{formatTimestamp(record.deployed_at)}</span>
        </div>
        {record.status === "terminated" && (
          <div className="cost-stat">
            <span className="cost-stat-label">Terminated at</span>
            <span className="cost-stat-value">{formatTimestamp(record.terminated_at)}</span>
          </div>
        )}
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
    </div>
  );
}

function BucketCleanup({ defaultRegion }) {
  const [region, setRegion] = useState(defaultRegion);
  const [regionTouched, setRegionTouched] = useState(false);
  const [active, setActive] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  // Keep the region synced to the most recent feedback deploy once
  // deployment history actually loads (this mounts before that first fetch
  // resolves) — but stop overriding it the moment the user picks one.
  useEffect(() => {
    if (!regionTouched) setRegion(defaultRegion);
  }, [defaultRegion, regionTouched]);

  // Polls whether a feedback-stack deploy is currently mid-flight so the
  // button stays disabled for the whole window it could race one — not
  // just at the moment it's clicked.
  useEffect(() => {
    let cancelled = false;
    function poll() {
      fetch(`${BACKEND_URL}/deploy-feedback/active`)
        .then((res) => res.json())
        .then((data) => {
          if (!cancelled) setActive(Boolean(data.active));
        })
        .catch(() => {});
    }
    poll();
    const interval = setInterval(poll, ACTIVE_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  async function confirmCleanup() {
    setCleaning(true);
    setError(null);
    setResult(null);

    let res;
    try {
      res = await fetch(`${BACKEND_URL}/deploy-feedback/cleanup-bucket`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ region }),
      });
    } catch {
      setError("Could not reach the backend");
      setCleaning(false);
      return;
    }

    let data;
    try {
      data = await res.json();
    } catch {
      data = null;
    }

    if (!res.ok) {
      setError(data?.detail || "Cleanup failed");
      setCleaning(false);
      return;
    }

    setResult(data);
    setCleaning(false);
    setConfirming(false);
  }

  return (
    <section className="form-section">
      <div className="section-heading">
        <h2 className="section-title">S3 cleanup</h2>
        <span className="section-hint">
          Feedback-stack bundle uploads relay through one S3 bucket per AWS account, normally emptied and left
          in place after every deploy. Use this if you suspect one was left behind — it deletes the bucket
          entirely, not just its contents, so nothing keeps quietly costing storage.
        </span>
      </div>

      <div className="field-row">
        <div className="field">
          <label htmlFor="cleanup_region">Region</label>
          <select
            id="cleanup_region"
            className="input"
            value={region}
            onChange={(e) => {
              setRegion(e.target.value);
              setRegionTouched(true);
            }}
          >
            {AWS_REGIONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </div>
      </div>

      {!confirming && (
        <button
          type="button"
          className="terminate-btn"
          disabled={active}
          title={active ? "A feedback-stack deployment is in progress — try again once it finishes." : undefined}
          onClick={() => setConfirming(true)}
        >
          Delete S3 bucket
        </button>
      )}

      {confirming && (
        <div className="terminate-confirm-row">
          <span className="terminate-confirm-text">
            Delete the feedback-stack relay bucket in <strong>{region}</strong>, if one exists? This can't be
            undone — it's recreated automatically the next time you deploy the feedback stack.
          </span>
          <div className="terminate-confirm-actions">
            <button
              type="button"
              className="terminate-btn terminate-btn--danger"
              disabled={cleaning}
              onClick={confirmCleanup}
            >
              {cleaning ? "Deleting…" : "Confirm delete"}
            </button>
            <button type="button" className="link-btn" disabled={cleaning} onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
          {error && <p className="terminate-error">{error}</p>}
        </div>
      )}

      {result && !confirming && (
        <p className="field-status field-status--ok">
          {result.existed
            ? `✓ Deleted bucket ${result.bucket} (${result.objects_deleted} leftover object${result.objects_deleted === 1 ? "" : "s"} removed first).`
            : `✓ No bucket found for this account/region — nothing to clean up.`}
        </p>
      )}
    </section>
  );
}

export default function DeploymentHistory({ refreshKey, onTerminated }) {
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
  const defaultCleanupRegion = records.find((r) => r.kind === "feedback")?.region || AWS_REGIONS[0];

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
                  <DeploymentRow key={r.deployment_id} record={r} onChanged={load} onTerminated={onTerminated} />
                ))}
              </div>
            </section>
          )}

          {terminated.length > 0 && (
            <section className="form-section">
              <h2 className="section-title">Terminated</h2>
              <div className="history-list">
                {terminated.map((r) => (
                  <DeploymentRow key={r.deployment_id} record={r} onChanged={load} onTerminated={onTerminated} />
                ))}
              </div>
            </section>
          )}

          <BucketCleanup defaultRegion={defaultCleanupRegion} />
        </div>
      </div>
    </div>
  );
}
