import { useEffect, useState } from "react";
import { BACKEND_URL } from "./config";

const POLL_INTERVAL_MS = 5000;

function formatElapsed(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

// Polls a real cost-estimate endpoint (GET /deploy/{id}/cost-estimate or
// GET /deploy-feedback/{id}/cost-estimate) rather than guessing from a
// client-side clock — figures are computed server-side from the real AWS
// launch time. Shared by both the app deploy and feedback-stack deploy
// cards; costEstimateUrl's response shape (s3_estimated_usd present or not)
// decides whether the S3 line renders.
export default function CostMonitor({
  costEstimateUrl,
  instanceId,
  instanceName,
  region,
  deploymentId,
  onTerminated,
  externallyTerminated,
}) {
  const [estimate, setEstimate] = useState(null);
  const [now, setNow] = useState(() => Date.now());
  const [terminated, setTerminated] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [terminating, setTerminating] = useState(false);
  const [error, setError] = useState(null);

  // externallyTerminated covers the case where this same deployment was
  // terminated from a different view (the Deployments tab) — this card has
  // no way to know that happened on its own, since it only ever checks its
  // own local state, so App.jsx tells it.
  const isTerminated = terminated || externallyTerminated;

  useEffect(() => {
    if (isTerminated) return undefined;
    let cancelled = false;
    const fetchEstimate = () =>
      fetch(`${BACKEND_URL}${costEstimateUrl}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          if (!cancelled && data) setEstimate(data);
        })
        .catch(() => {});
    fetchEstimate();
    const interval = setInterval(fetchEstimate, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [costEstimateUrl, isTerminated]);

  useEffect(() => {
    if (isTerminated) return undefined;
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [isTerminated]);

  async function confirmTerminate() {
    setTerminating(true);
    setError(null);

    let res;
    try {
      res = await fetch(`${BACKEND_URL}/terminate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: instanceId, region, deployment_id: deploymentId }),
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
      setError(`Backend returned an unexpected response (HTTP ${res.status}).`);
      setTerminating(false);
      return;
    }

    if (!res.ok) {
      const detail = Array.isArray(data?.detail) ? data.detail.map((d) => d.msg).join("; ") : data?.detail;
      setError(detail || "Terminate failed");
      setTerminating(false);
      return;
    }

    setTerminated(true);
    setConfirming(false);
    setTerminating(false);
    onTerminated?.(deploymentId);
  }

  if (!estimate) return null;

  const elapsedSeconds = Math.max(0, (now - new Date(estimate.launched_at).getTime()) / 1000);
  const hasS3 = estimate.s3_estimated_usd !== undefined;
  const ec2Cost = estimate.ec2_estimated_usd ?? estimate.estimated_usd;
  const displayName = instanceName || instanceId;

  return (
    <div className="cost-monitor" data-status={isTerminated ? "terminated" : "running"}>
      <div className="cost-monitor-header">
        <span className={`cost-status-dot ${isTerminated ? "" : "cost-status-dot--live"}`} aria-hidden="true" />
        <span className="cost-monitor-title">{isTerminated ? "Deployment terminated" : "Deployment running"}</span>
      </div>

      <div className="cost-monitor-stats">
        <div className="cost-stat">
          <span className="cost-stat-label">Elapsed</span>
          <span className="cost-stat-value">{formatElapsed(elapsedSeconds)}</span>
        </div>
        <div className="cost-stat">
          <span className="cost-stat-label">Instance</span>
          <span className="cost-stat-value" title={instanceId}>
            {displayName}
          </span>
        </div>
        <div className="cost-stat">
          <span className="cost-stat-label">{hasS3 ? "EC2 + EBS" : "Estimated cost"}</span>
          <span className="cost-stat-value cost-stat-value--highlight">${ec2Cost.toFixed(4)}</span>
        </div>
        {hasS3 && (
          <div className="cost-stat">
            <span className="cost-stat-label">S3 (bundle relay)</span>
            <span className="cost-stat-value cost-stat-value--highlight">${estimate.s3_estimated_usd.toFixed(6)}</span>
          </div>
        )}
      </div>

      <p className="cost-monitor-note">
        Estimated from the real AWS launch time ({estimate.instance_type} @ ${estimate.hourly_rate.toFixed(4)}/hr
        + EBS storage){hasS3 ? " — the S3 figure stops growing once the relay bundle is deleted" : ""}. Terminate
        below is real: it calls AWS and deletes the instance.
      </p>

      {!isTerminated && !confirming && (
        <button type="button" className="terminate-btn" onClick={() => setConfirming(true)}>
          Terminate instance
        </button>
      )}

      {!isTerminated && confirming && (
        <div className="terminate-confirm-row">
          <span className="terminate-confirm-text">
            Terminate <strong>{displayName}</strong>? This deletes the EC2 instance and its EBS volume —
            it can't be undone.
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
