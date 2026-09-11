import { useEffect, useRef, useState } from "react";
import { BACKEND_URL } from "./config";
import CostMonitor from "./CostMonitor.jsx";
import DeploySteps from "./DeploySteps.jsx";

// Mirrors backend/app/api/deploy.py's AWS_REGIONS — same curated list,
// duplicated rather than shared across the two independent form components.
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

// Named identically to GET /deploy-feedback/{id}/status's `steps` keys —
// same no-translation-layer pattern as DeployForm.jsx's pipeline.
const PIPELINE_STEPS = [
  { id: "feedback_bundle", label: "Package & upload feedback stack" },
  { id: "feedback_compute", label: "Provision feedback stack compute" },
  { id: "feedback_health", label: "Check feedback :8002/:8003" },
];
const POLL_INTERVAL_MS = 2000;

function freshSteps() {
  return PIPELINE_STEPS.map((step) => ({ ...step, status: "pending", errorMessage: null }));
}

const initialState = {
  access_key_id: "",
  secret_access_key: "",
  region: AWS_REGIONS[0],
  instance_name: "",
};

export default function DeployFeedbackStack({ onDeploySuccess, lastAwsRegion }) {
  const [form, setForm] = useState(initialState);
  const [questionnaireFile, setQuestionnaireFile] = useState(null);
  const [fileError, setFileError] = useState(null);
  const [result, setResult] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [steps, setSteps] = useState(freshSteps());
  const [hasStarted, setHasStarted] = useState(false);
  const [deployCount, setDeployCount] = useState(0);
  const [reuseCredentials, setReuseCredentials] = useState(Boolean(lastAwsRegion));
  const pollGenerationRef = useRef(0);

  // A deploy elsewhere in the app becomes known after this component has
  // already mounted — pick up its region and switch to the reuse affordance
  // once it lands, rather than only checking on first render.
  useEffect(() => {
    if (lastAwsRegion) {
      setForm((f) => ({ ...f, region: lastAwsRegion }));
      setReuseCredentials(true);
    }
  }, [lastAwsRegion]);

  function setStepStatus(id, status, errorMessage = null) {
    setSteps((prev) => prev.map((s) => (s.id === id ? { ...s, status, errorMessage } : s)));
  }

  function updateField(field, value) {
    setForm((f) => ({ ...f, [field]: value }));
  }

  function handleFileChange(e) {
    const file = e.target.files?.[0] || null;
    setQuestionnaireFile(file);
    setFileError(null);
  }

  function pollStatus(deploymentId, generation, region, instanceName) {
    if (pollGenerationRef.current !== generation) return;

    fetch(`${BACKEND_URL}/deploy-feedback/${deploymentId}/status`)
      .then((res) => res.json())
      .then((data) => {
        if (pollGenerationRef.current !== generation) return;

        for (const step of PIPELINE_STEPS) {
          if (data.steps?.[step.id]) setStepStatus(step.id, data.steps[step.id]);
        }

        if (data.result) {
          // Snapshot the region/name/deployment id actually used for *this*
          // deploy — same reasoning as DeployForm.jsx's cost widget wiring.
          const finalResult = { ...data.result, region, instanceName, deploymentId };
          setResult(finalResult);
          setDeployCount((c) => c + 1);
          setSubmitting(false);
          onDeploySuccess?.(finalResult);
          return;
        }

        if (data.error) {
          const failedStep = PIPELINE_STEPS.find((s) => data.steps?.[s.id] === "error");
          setStepStatus(failedStep?.id || "feedback_bundle", "error", data.error);
          setSubmitting(false);
          return;
        }

        setTimeout(() => pollStatus(deploymentId, generation, region, instanceName), POLL_INTERVAL_MS);
      })
      .catch(() => {
        // A blip reaching our own backend for a poll — not a deployment
        // failure. The background task on the server side is unaffected.
        setTimeout(() => pollStatus(deploymentId, generation, region, instanceName), POLL_INTERVAL_MS);
      });
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (!questionnaireFile || submitting) return;

    let questionnaireText;
    try {
      questionnaireText = await questionnaireFile.text();
    } catch {
      setFileError("Could not read that file.");
      return;
    }

    setResult(null);
    setFileError(null);
    setSubmitting(true);
    setHasStarted(true);
    setSteps(freshSteps());
    const generation = ++pollGenerationRef.current;

    const payload = {
      aws: {
        access_key_id: form.access_key_id.trim() || null,
        secret_access_key: form.secret_access_key.trim() || null,
        region: form.region,
      },
      instance_name: form.instance_name.trim() || null,
      questionnaire_json: questionnaireText,
    };

    setStepStatus("feedback_bundle", "active");

    let res;
    try {
      res = await fetch(`${BACKEND_URL}/deploy-feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      setStepStatus("feedback_bundle", "error", "Could not reach the backend");
      setSubmitting(false);
      return;
    }

    let data;
    try {
      data = await res.json();
    } catch {
      setStepStatus("feedback_bundle", "error", `Backend returned an unexpected response (HTTP ${res.status}).`);
      setSubmitting(false);
      return;
    }

    if (!res.ok) {
      const detail = Array.isArray(data?.detail)
        ? data.detail.map((d) => d.msg).join("; ")
        : data?.detail || data?.error || "Deploy failed";
      setStepStatus("feedback_bundle", "error", detail);
      setSubmitting(false);
      return;
    }

    pollStatus(data.deployment_id, generation, form.region, form.instance_name.trim() || null);
  }

  return (
    <div className="card-wrap">
      <div className="card">
        <form onSubmit={handleSubmit} className="form">
          <section className="form-section">
            <h2 className="section-title">Deploy feedback stack</h2>
            <p className="section-hint">
              Independent of the agent deploy above — provisions Postgres, the feedback form (:8002), and the
              token-gated feedback dashboard (:8003) in your AWS account.
            </p>
          </section>

          <section className="form-section">
            <div className="field">
              <label htmlFor="questionnaire_file">Questionnaire (JSON)</label>
              <input
                id="questionnaire_file"
                type="file"
                accept="application/json,.json"
                className="input"
                onChange={handleFileChange}
              />
              {questionnaireFile && <p className="field-status field-status--ok">✓ {questionnaireFile.name}</p>}
              {fileError && <p className="field-status field-status--error">✗ {fileError}</p>}
            </div>
          </section>

          <section className="form-section">
            <div className="section-heading">
              <h2 className="section-title">AWS credentials</h2>
              {!reuseCredentials && (
                <span className="section-hint">Leave blank to reuse credentials already cached from a prior deploy.</span>
              )}
            </div>
            {reuseCredentials ? (
              <div className="reuse-creds-row">
                <span className="field-status field-status--ok">
                  ✓ Using AWS credentials from your last deploy ({form.region})
                </span>
                <button type="button" className="link-btn" onClick={() => setReuseCredentials(false)}>
                  Change
                </button>
              </div>
            ) : (
              <div className="field-row">
                <div className="field">
                  <label htmlFor="feedback_access_key_id">Access key ID</label>
                  <input
                    id="feedback_access_key_id"
                    type="text"
                    className="input"
                    value={form.access_key_id}
                    onChange={(e) => updateField("access_key_id", e.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="feedback_secret_access_key">Secret access key</label>
                  <input
                    id="feedback_secret_access_key"
                    type="password"
                    className="input"
                    value={form.secret_access_key}
                    onChange={(e) => updateField("secret_access_key", e.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="feedback_region">Region</label>
                  <select
                    id="feedback_region"
                    className="input"
                    value={form.region}
                    onChange={(e) => updateField("region", e.target.value)}
                  >
                    {AWS_REGIONS.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            )}
          </section>

          <section className="form-section">
            <div className="field">
              <label htmlFor="feedback_instance_name">Instance name</label>
              <input
                id="feedback_instance_name"
                type="text"
                className="input"
                placeholder="e.g. my-agent-feedback"
                value={form.instance_name}
                onChange={(e) => updateField("instance_name", e.target.value)}
              />
            </div>
          </section>

          <button type="submit" className="submit-btn" disabled={submitting || !questionnaireFile}>
            {submitting && <span className="spinner" />}
            {submitting ? "Deploying…" : "Deploy feedback stack"}
          </button>

          {hasStarted && <DeploySteps steps={steps} />}

          {result && (
            <div className="banner banner--success">
              <p className="banner-title">Feedback stack deployed</p>
              <ul className="link-list">
                <li className="link-row">
                  <span className="link-label">Feedback form</span>
                  <a className="link-chip" href={result.feedback_form_url} target="_blank" rel="noreferrer">
                    {result.feedback_form_url}
                  </a>
                </li>
                <li className="link-row">
                  <span className="link-label">Feedback dashboard (private, token-gated)</span>
                  <a className="link-chip" href={result.feedback_dashboard_url} target="_blank" rel="noreferrer">
                    {result.feedback_dashboard_url}
                  </a>
                </li>
              </ul>
            </div>
          )}

          {result && (
            <CostMonitor
              key={deployCount}
              costEstimateUrl={`/deploy-feedback/${result.deploymentId}/cost-estimate`}
              instanceId={result.feedback_instance_id}
              instanceName={result.instanceName}
              region={result.region}
              deploymentId={result.deploymentId}
            />
          )}
        </form>
      </div>
    </div>
  );
}
