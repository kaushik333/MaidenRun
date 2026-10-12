import { useEffect, useState } from "react";
import TerminateControl from "./TerminateControl.jsx";
import { BACKEND_URL } from "./config";
import { apiFetch } from "./apiClient";

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
  kind,
  onTerminated,
  externallyTerminated,
}) {
  const [estimate, setEstimate] = useState(null);
  const [now, setNow] = useState(() => Date.now());
  const [terminated, setTerminated] = useState(false);
  const [exports, setExports] = useState(null);

  // externallyTerminated covers the case where this same deployment was
  // terminated from a different view (the Deployments tab) — this card has
  // no way to know that happened on its own, since it only ever checks its
  // own local state, so App.jsx tells it.
  const isTerminated = terminated || externallyTerminated;

  useEffect(() => {
    if (isTerminated) return undefined;
    let cancelled = false;
    const fetchEstimate = () =>
      apiFetch(`${BACKEND_URL}${costEstimateUrl}`)
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

  function handleTerminated(data) {
    setTerminated(true);
    setExports(data?.exports ?? null);
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

      {!isTerminated && (
        <TerminateControl
          instanceId={instanceId}
          region={region}
          deploymentId={deploymentId}
          kind={kind}
          displayName={displayName}
          label="Terminate instance"
          onTerminated={handleTerminated}
        />
      )}

      {exports && <p className="field-status field-status--ok">✓ Feedback saved to {exports.csv} (and .json)</p>}
    </div>
  );
}
