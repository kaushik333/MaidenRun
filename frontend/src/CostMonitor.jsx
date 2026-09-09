import { useEffect, useState } from "react";
import { BACKEND_URL } from "./config";

// Illustrative on-demand hourly rates, matching IDEA.md's EC2 Instance Options
// table. Mocked for the deploying user to watch spend accrue — real AWS cost
// tracking wires in once provisioning (Phase 4-6) is built.
const INSTANCE_HOURLY_RATES = {
  "t3.small": 0.02,
  "t3.medium": 0.04,
  "m5.xlarge": 0.19,
  "g4dn.xlarge": 0.53,
  "g5.xlarge": 1.01,
};

const EBS_HOURLY_RATE_PER_GB = 0.08 / 730; // ~$0.08/GB-month (gp3), converted to hourly

function formatElapsed(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
}

export default function CostMonitor({ instanceType, ebsSizeGb, instanceId, instanceName, region }) {
  const [now, setNow] = useState(() => Date.now());
  const [launchedAt] = useState(() => Date.now());
  const [terminated, setTerminated] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [terminating, setTerminating] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (terminated) return undefined;
    const interval = setInterval(() => setNow(Date.now()), 3000);
    return () => clearInterval(interval);
  }, [terminated]);

  const elapsedSeconds = Math.max(0, (now - launchedAt) / 1000);
  const elapsedHours = elapsedSeconds / 3600;
  const hourlyRate = INSTANCE_HOURLY_RATES[instanceType] ?? 0;
  const totalCost = elapsedHours * hourlyRate + ebsSizeGb * EBS_HOURLY_RATE_PER_GB * elapsedHours;
  const displayName = instanceName || instanceId;

  async function confirmTerminate() {
    setTerminating(true);
    setError(null);

    let res;
    try {
      res = await fetch(`${BACKEND_URL}/terminate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: instanceId, region }),
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
  }

  return (
    <div className="cost-monitor" data-status={terminated ? "terminated" : "running"}>
      <div className="cost-monitor-header">
        <span className={`cost-status-dot ${terminated ? "" : "cost-status-dot--live"}`} aria-hidden="true" />
        <span className="cost-monitor-title">{terminated ? "Deployment terminated" : "Deployment running"}</span>
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
          <span className="cost-stat-label">Estimated cost</span>
          <span className="cost-stat-value cost-stat-value--highlight">${totalCost.toFixed(4)}</span>
        </div>
      </div>

      <p className="cost-monitor-note">
        Cost is an estimate only (${hourlyRate.toFixed(2)}/hr instance + {ebsSizeGb}GB storage) — real AWS cost
        tracking wires in later. Terminate below is real: it calls AWS and deletes the instance.
      </p>

      {!terminated && !confirming && (
        <button type="button" className="terminate-btn" onClick={() => setConfirming(true)}>
          Terminate instance
        </button>
      )}

      {!terminated && confirming && (
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
