import { useState } from "react";
import { BACKEND_URL } from "./config";
import { apiFetch } from "./apiClient";

// Terminate button + confirm step, shared by the cost monitor and the
// Deployments tab. A feedback stack's data is exported first; if that fails
// the backend refuses (409) and the user can retry or terminate anyway.
export default function TerminateControl({ instanceId, region, deploymentId, kind, displayName, label, onTerminated }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [exportFailed, setExportFailed] = useState(false);
  const isFeedback = kind === "feedback";

  async function terminate(force) {
    setBusy(true);
    setError(null);
    let res;
    try {
      res = await apiFetch(`${BACKEND_URL}/terminate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: instanceId, region, deployment_id: deploymentId, force }),
      });
    } catch {
      setError("Could not reach the backend");
      setBusy(false);
      return;
    }
    const data = await res.json().catch(() => null);
    setBusy(false);

    if (res.status === 409) {
      setExportFailed(true);
      setError(data?.detail);
      return;
    }
    if (!res.ok) {
      setError(
        (Array.isArray(data?.detail)
          ? data.detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ")
          : data?.detail) || "Terminate failed"
      );
      return;
    }
    setConfirming(false);
    setExportFailed(false);
    onTerminated?.(data);
  }

  if (!confirming) {
    return (
      <button type="button" className="terminate-btn" onClick={() => setConfirming(true)}>
        {label}
      </button>
    );
  }

  return (
    <div className="terminate-confirm-row">
      <span className="terminate-confirm-text">
        Terminate <strong>{displayName}</strong>?{" "}
        {isFeedback
          ? "All feedback is first exported (CSV + JSON) to data/exports/ on this machine, then the EC2 instance, its disk, its secrets and its security group are deleted."
          : "This deletes the EC2 instance and its disk — including anything your agent stored on it — plus its secrets and security group."}{" "}
        It can't be undone.
      </span>
      <div className="terminate-confirm-actions">
        {exportFailed ? (
          <>
            <button type="button" className="check-btn" disabled={busy} onClick={() => terminate(false)}>
              Retry export
            </button>
            <button
              type="button"
              className="terminate-btn terminate-btn--danger"
              disabled={busy}
              onClick={() => terminate(true)}
            >
              {busy ? "Terminating…" : "Terminate anyway (feedback will be lost)"}
            </button>
          </>
        ) : (
          <button
            type="button"
            className="terminate-btn terminate-btn--danger"
            disabled={busy}
            onClick={() => terminate(false)}
          >
            {busy ? (isFeedback ? "Exporting feedback…" : "Terminating…") : isFeedback ? "Export & terminate" : "Confirm terminate"}
          </button>
        )}
        <button type="button" className="link-btn" disabled={busy} onClick={() => setConfirming(false)}>
          Cancel
        </button>
      </div>
      {error && <p className="terminate-error">{error}</p>}
    </div>
  );
}
