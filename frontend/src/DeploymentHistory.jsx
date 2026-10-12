import { useEffect, useState } from "react";
import { BACKEND_URL } from "./config";
import { apiFetch } from "./apiClient";
import AllowedIpsField, { parseAllowedIps } from "./AllowedIpsField.jsx";
import TerminateControl from "./TerminateControl.jsx";

const DECISION_LABELS = {
  repo_url: "Repo",
  private_repo: "Private repo",
  api_key_name: "API key env var",
  base_url_name: "Base URL env var",
  instance_type: "Instance type",
  ebs_size_gb: "EBS size",
  commit_sha: "Commit",
  commit_message: "Commit message",
  agent_repo: "Collects feedback for",
};

const shortRepo = (url) => url.replace(/^https?:\/\/(www\.)?github\.com\//, "");

// The commit a linked feedback stack is stamping responses with: the newest
// running deployment of its agent repo (same rule the backend uses when it
// notifies the stack — see link_to_live_agent / announce_agent_version).
function liveAgentFor(records, agentRepo) {
  const normalize = (url) => url.trim().replace(/\/+$/, "").replace(/\.git$/, "").toLowerCase();
  return records.find(
    (r) =>
      r.kind === "agent" &&
      r.status === "running" &&
      r.decisions?.commit_sha &&
      r.decisions?.repo_url &&
      normalize(r.decisions.repo_url) === normalize(agentRepo)
  );
}

function CommitLine({ sha, message }) {
  return (
    <>
      <code>{sha.slice(0, 7)}</code>
      {message ? ` — ${message}` : ""}
    </>
  );
}

const EXPORT_LABELS = { xlsx: "Download Excel", json: "Download JSON", csv: "Download CSV" };

// API calls are signed (see apiClient), so a plain <a href> can't download —
// fetch the file and hand it to the browser as a blob instead.
async function saveDownload(url, filename) {
  const res = await apiFetch(url);
  if (!res.ok) throw new Error();
  const objectUrl = URL.createObjectURL(await res.blob());
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(objectUrl);
}

async function apiJson(url, options) {
  const res = await apiFetch(url, options);
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(
      (Array.isArray(data?.detail)
        ? data.detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ")
        : data?.detail) || "Request failed"
    );
  }
  return data;
}

// Questionnaire versions on a running feedback stack: publishing or
// activating one changes the form at the same URL — no redeploy. Loaded
// only when opened, so an unreachable stack never slows the whole tab down.
function QuestionnairePanel({ record }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [file, setFile] = useState(null);
  const [fileInputKey, setFileInputKey] = useState(0); // bumped to clear the native file picker
  const [activate, setActivate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(null);
  const [error, setError] = useState(null);
  const base = `${BACKEND_URL}/deployments/${record.deployment_id}/questionnaires`;

  async function load() {
    setLoading(true);
    try {
      setData(await apiJson(base));
      setError(null);
    } catch (e) {
      setError(e.message);
    }
    setLoading(false);
  }

  async function act(run, successMessage) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const result = await run();
      setMessage(successMessage(result));
      await load();
    } catch (e) {
      setError(e.message);
    }
    setBusy(false);
  }

  async function publish() {
    const questionnaire_json = await file.text();
    await act(
      () =>
        apiJson(base, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ questionnaire_json, activate }),
        }),
      (result) => `✓ Published v${result.version}${activate ? " — the form shows it from the next page load" : ""}.`
    );
    setFile(null);
    setFileInputKey((k) => k + 1);
  }

  const active = data?.questionnaires.find((q) => q.is_active);

  return (
    <details className="history-decisions questionnaire-panel" onToggle={(e) => e.target.open && !data && load()}>
      <summary>Questionnaire{active ? ` — v${active.version} active` : ""}</summary>
      <div className="questionnaire-body">
        {loading && !data && <p className="section-hint">Loading versions from the feedback stack…</p>}

        {data && (
          <>
            <table className="version-table">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Questions</th>
                  <th>Created</th>
                  <th>Responses</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.questionnaires
                  .slice()
                  .reverse()
                  .map((q) => (
                    <tr key={q.version}>
                      <td className="version-name">
                        v{q.version}
                        {q.is_active && <span className="version-active">active</span>}
                      </td>
                      <td>{q.questions.length}</td>
                      <td>{formatTimestamp(q.created_at)}</td>
                      <td>{q.responses}</td>
                      <td>
                        <div className="version-actions">
                          {!q.is_active && (
                            <button
                              type="button"
                              className="link-btn"
                              disabled={busy}
                              onClick={() =>
                                act(
                                  () => apiJson(`${base}/${q.version}/activate`, { method: "POST" }),
                                  () => `✓ v${q.version} is now active — the form shows it from the next page load.`
                                )
                              }
                            >
                              Activate
                            </button>
                          )}
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() =>
                              saveDownload(`${base}/${q.version}/download`, `questionnaire-v${q.version}.json`).catch(
                                () => setError("Could not download that version.")
                              )
                            }
                          >
                            Download JSON
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>

            <div className="questionnaire-upload">
              <span className="questionnaire-upload-title">Publish a new version</span>
              <span className="section-hint">
                Same JSON format as the original upload. To add questions, download the current version, edit it,
                and upload it here.
              </span>
              <input
                key={fileInputKey}
                type="file"
                accept=".json,application/json"
                onChange={(e) => setFile(e.target.files[0] || null)}
              />
              <label className="questionnaire-activate">
                <input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} />
                Make it the active version now
              </label>
              <button type="button" className="check-btn" disabled={!file || busy} onClick={publish}>
                {busy ? "Working…" : "Publish version"}
              </button>
            </div>
          </>
        )}

        {message && <p className="field-status field-status--ok">{message}</p>}
        {error && <p className="terminate-error">{error}</p>}
      </div>
    </details>
  );
}

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

function AccessControl({ record, onChanged }) {
  const allowed = record.decisions?.allowed_ips || [];
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  function startEditing() {
    setText(allowed.join("\n"));
    setError(null);
    setEditing(true);
  }

  async function save() {
    setSaving(true);
    setError(null);
    let res;
    try {
      res = await apiFetch(`${BACKEND_URL}/deployments/${record.deployment_id}/allowed-ips`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ allowed_ips: parseAllowedIps(text) }),
      });
    } catch {
      setError("Could not reach the backend");
      setSaving(false);
      return;
    }
    const data = await res.json().catch(() => null);
    setSaving(false);
    if (!res.ok) {
      setError(
        Array.isArray(data?.detail) ? data.detail.map((d) => d.msg.replace(/^Value error, /, "")).join("; ") : data?.detail || "Update failed"
      );
      return;
    }
    setEditing(false);
    onChanged();
  }

  // Deployments made before per-deployment security groups share one, so
  // their access can't be changed without a redeploy.
  if (!record.security_group_id) {
    return (
      <div className="history-access">
        <span>Access: open to everyone (redeploy to restrict by IP)</span>
      </div>
    );
  }

  if (!editing) {
    return (
      <div className="history-access">
        <div className="history-access-summary">
          <span>Access: {allowed.length ? `${allowed.join(", ")} + your IP` : "open to everyone"}</span>
          <button type="button" className="link-btn" onClick={startEditing}>
            Edit allowed IPs
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="history-access">
      <AllowedIpsField id={`allowed_ips_${record.deployment_id}`} value={text} onChange={setText} disabled={saving} />
      <div className="history-access-actions">
        <button type="button" className="check-btn" disabled={saving} onClick={save}>
          {saving ? "Saving…" : "Save"}
        </button>
        <button type="button" className="link-btn" disabled={saving} onClick={() => setEditing(false)}>
          Cancel
        </button>
      </div>
      {error && <p className="terminate-error">{error}</p>}
    </div>
  );
}

function DeploymentRow({ record, records, onChanged, onTerminated }) {
  const [downloadError, setDownloadError] = useState(null);

  async function downloadExport(fmt) {
    setDownloadError(null);
    try {
      await saveDownload(
        `${BACKEND_URL}/deployments/${record.deployment_id}/export/${fmt}`,
        `feedback-${record.deployment_id}.${fmt}`
      );
    } catch {
      setDownloadError("Could not download the export — it may have been moved or deleted from data/exports/.");
    }
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

      {record.kind === "agent" && record.decisions?.commit_sha && (
        <p className="history-row-commit">
          Commit <CommitLine sha={record.decisions.commit_sha} message={record.decisions.commit_message} />
        </p>
      )}
      {record.kind === "feedback" && record.decisions?.agent_repo && (
        <p className="history-row-commit">
          Collects feedback for <strong>{shortRepo(record.decisions.agent_repo)}</strong>
          {record.status === "running" &&
            (() => {
              const live = liveAgentFor(records, record.decisions.agent_repo);
              return live ? (
                <>
                  {" "}
                  · live agent <CommitLine sha={live.decisions.commit_sha} message={live.decisions.commit_message} />
                </>
              ) : (
                " · no running agent with a recorded commit"
              );
            })()}
        </p>
      )}

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

      {record.status === "running" && record.kind === "feedback" && <QuestionnairePanel record={record} />}

      {record.status === "running" && <AccessControl record={record} onChanged={onChanged} />}

      {record.exports && (
        <div className="history-access">
          <div className="history-access-summary">
            <span>Feedback exported before teardown:</span>
            {Object.keys(record.exports).map((fmt) => (
              <button key={fmt} type="button" className="link-btn" onClick={() => downloadExport(fmt)}>
                {EXPORT_LABELS[fmt] || `Download ${fmt}`}
              </button>
            ))}
          </div>
          {downloadError && <p className="terminate-error">{downloadError}</p>}
        </div>
      )}

      {record.status === "running" && (
        <TerminateControl
          instanceId={record.instance_id}
          region={record.region}
          deploymentId={record.deployment_id}
          kind={record.kind}
          displayName={record.instance_name || record.instance_id}
          label="Terminate"
          onTerminated={() => {
            onChanged();
            // Tells App.jsx so a CostMonitor showing this same deployment elsewhere
            // (the Deploy or Feedback stack tab) flips to "terminated" too.
            onTerminated?.(record.deployment_id);
          }}
        />
      )}
    </div>
  );
}

const LEFTOVER_KIND_LABELS = {
  instance: "EC2 instance",
  security_group: "Security group",
  secrets: "Secrets",
  bucket: "S3 bucket",
};

const leftoverKey = (item) => `${item.kind}:${item.region}:${item.id}`;

function Leftovers({ onChanged }) {
  const [items, setItems] = useState(null);
  const [warnings, setWarnings] = useState([]);
  const [selected, setSelected] = useState(new Set());
  const [scanning, setScanning] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [failures, setFailures] = useState([]);
  const [error, setError] = useState(null);

  async function scan() {
    setScanning(true);
    setError(null);
    try {
      const res = await apiFetch(`${BACKEND_URL}/leftovers`);
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || "Scan failed");
      setItems(data.items);
      setWarnings(data.warnings || []);
      setSelected(new Set());
    } catch (e) {
      setError(e.message || "Could not reach the backend");
    }
    setScanning(false);
  }

  function toggle(item) {
    setSelected((current) => {
      const next = new Set(current);
      next.has(leftoverKey(item)) ? next.delete(leftoverKey(item)) : next.add(leftoverKey(item));
      return next;
    });
  }

  async function deleteSelected() {
    setDeleting(true);
    setError(null);
    try {
      const res = await apiFetch(`${BACKEND_URL}/leftovers/delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: items.filter((item) => selected.has(leftoverKey(item))) }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || "Delete failed");
      setFailures(data.filter((r) => !r.ok));
    } catch (e) {
      setError(e.message || "Could not reach the backend");
    }
    setDeleting(false);
    setConfirming(false);
    onChanged();
    await scan();
  }

  const billing = (items || []).filter((item) => item.hourly_usd);

  return (
    <section className="form-section">
      <div className="section-heading">
        <h2 className="section-title">Leftover resources</h2>
        <span className="section-hint">
          Scans every region for anything MaidenRun created that no running deployment uses — instances still
          billing, unattached security groups, old secrets and the S3 relay bucket — so nothing quietly costs money
          or keeps credentials around.
        </span>
      </div>

      <button type="button" className="check-btn leftovers-scan-btn" disabled={scanning || deleting} onClick={scan}>
        {scanning ? "Scanning all regions…" : items ? "Scan again" : "Scan all regions"}
      </button>

      {error && <p className="terminate-error">{error}</p>}
      {warnings.map((warning) => (
        <p key={warning} className="section-hint leftover-warning">
          ⚠ {warning}
        </p>
      ))}
      {failures.map((f) => (
        <p key={leftoverKey(f.item)} className="terminate-error">
          Couldn't delete {f.item.label}: {f.error}
        </p>
      ))}

      {items && items.length === 0 && (
        <p className="field-status field-status--ok">✓ Nothing left over in any region.</p>
      )}

      {items && items.length > 0 && (
        <>
          {billing.length > 0 && (
            <p className="terminate-error">
              {billing.length} instance{billing.length === 1 ? " is" : "s are"} still billing — about $
              {billing.reduce((sum, item) => sum + item.hourly_usd, 0).toFixed(4)}/hour.
            </p>
          )}
          <ul className="leftover-list">
            {items.map((item) => (
              <li key={leftoverKey(item)}>
                <label className="leftover-row">
                  <input type="checkbox" checked={selected.has(leftoverKey(item))} onChange={() => toggle(item)} />
                  <span className="leftover-kind">{LEFTOVER_KIND_LABELS[item.kind]}</span>
                  <span className="leftover-label">{item.label}</span>
                  <span className="leftover-detail">
                    {item.region} · {item.detail}
                    {item.hourly_usd ? ` · $${item.hourly_usd.toFixed(4)}/hr` : ""}
                  </span>
                </label>
              </li>
            ))}
          </ul>

          {!confirming && (
            <button
              type="button"
              className="terminate-btn"
              disabled={selected.size === 0}
              onClick={() => setConfirming(true)}
            >
              Delete selected ({selected.size})
            </button>
          )}
          {confirming && (
            <div className="terminate-confirm-row">
              <span className="terminate-confirm-text">
                Delete {selected.size} selected resource{selected.size === 1 ? "" : "s"}? Instances are terminated
                along with their disks. This can't be undone.
              </span>
              <div className="terminate-confirm-actions">
                <button
                  type="button"
                  className="terminate-btn terminate-btn--danger"
                  disabled={deleting}
                  onClick={deleteSelected}
                >
                  {deleting ? "Deleting…" : "Confirm delete"}
                </button>
                <button type="button" className="link-btn" disabled={deleting} onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}

export default function DeploymentHistory({ refreshKey, isActive, onTerminated }) {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  function load() {
    setLoading(true);
    apiFetch(`${BACKEND_URL}/deployments`)
      .then((res) => res.json())
      .then((data) => {
        setRecords(data);
        setError(null);
      })
      .catch(() => setError("Could not load deployment history"))
      .finally(() => setLoading(false));
  }

  // Reloaded whenever the tab is shown, not just after deploys made from this
  // window — a deploy from another browser tab still shows up.
  useEffect(() => {
    if (isActive) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey, isActive]);

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
                  <DeploymentRow
                    key={r.deployment_id}
                    record={r}
                    records={records}
                    onChanged={load}
                    onTerminated={onTerminated}
                  />
                ))}
              </div>
            </section>
          )}

          {terminated.length > 0 && (
            <section className="form-section">
              <h2 className="section-title">Terminated</h2>
              <div className="history-list">
                {terminated.map((r) => (
                  <DeploymentRow
                    key={r.deployment_id}
                    record={r}
                    records={records}
                    onChanged={load}
                    onTerminated={onTerminated}
                  />
                ))}
              </div>
            </section>
          )}

          <Leftovers onChanged={load} />
        </div>
      </div>
    </div>
  );
}
