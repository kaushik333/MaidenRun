import { useRef, useState } from "react";
import { BACKEND_URL } from "./config";
import AwsSetupGuide from "./AwsSetupGuide.jsx";
import CostMonitor from "./CostMonitor.jsx";
import DeploySteps from "./DeploySteps.jsx";

const GPU_INSTANCE_TYPES = ["g4dn.xlarge", "g5.xlarge"];
const CPU_INSTANCE_TYPES = ["t3.small", "t3.medium", "m5.xlarge"];
const EBS_SIZES = [20, 50, 100];
const LLM_PROVIDERS = ["openai", "anthropic", "azure_openai", "openrouter"];
// Curated, not exhaustive — mirrors backend/app/api/deploy.py's AWS_REGIONS.
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

const PROVIDER_LABELS = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  azure_openai: "Azure OpenAI",
  openrouter: "OpenRouter",
};

// validate/ports/aws_creds resolve together from POST /deploy's single
// synchronous response (fast: repo validation + local credential lookup,
// no AWS calls yet). The remaining two ids are polled live from
// GET /deploy/{id}/status — they're named identically to the keys in that
// endpoint's `steps` object so status values can be applied directly with
// no translation layer.
const PIPELINE_STEPS = [
  { id: "validate", label: "Validate repo & config" },
  { id: "ports", label: "Check port clashes" },
  { id: "aws_creds", label: "Authorize AWS access" },
  { id: "app_compute", label: "Provision agent compute" },
  { id: "app_health", label: "Run agent & check :8001" },
];
const POLLED_STEP_IDS = ["app_compute", "app_health"];
const POLL_INTERVAL_MS = 2000;

function freshSteps() {
  return PIPELINE_STEPS.map((step) => ({ ...step, status: "pending", errorMessage: null }));
}

const initialState = {
  repo_url: "",
  aws: { access_key_id: "", secret_access_key: "", region: AWS_REGIONS[0] },
  self_hosted_llm: false,
  instance_type: CPU_INSTANCE_TYPES[0],
  ebs_size_gb: EBS_SIZES[0],
  app_instance_name: "",
  llm_provider: LLM_PROVIDERS[0],
  api_key: "",
  env_vars: [],
};

const IDLE_REPO_CHECK = { status: "idle", entry_point: null, error: null };

export default function DeployForm() {
  const [form, setForm] = useState(initialState);
  const [result, setResult] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [steps, setSteps] = useState(freshSteps());
  const [hasStarted, setHasStarted] = useState(false);
  const [repoCheck, setRepoCheck] = useState(IDLE_REPO_CHECK);
  const [deployCount, setDeployCount] = useState(0);
  // Bumped on every submit; each poll checks it still matches before touching
  // state, so a stale poll from an old deployment can never clobber a newer
  // one's steps (e.g. after the user starts a second deploy).
  const pollGenerationRef = useRef(0);

  function setStepStatus(id, status, errorMessage = null) {
    setSteps((prev) => prev.map((s) => (s.id === id ? { ...s, status, errorMessage } : s)));
  }

  function updateRepoUrl(value) {
    setForm((f) => ({ ...f, repo_url: value }));
    setRepoCheck(IDLE_REPO_CHECK);
  }

  async function checkRepo() {
    setRepoCheck({ status: "checking", entry_point: null, error: null });

    let res;
    try {
      res = await fetch(`${BACKEND_URL}/validate-repo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_url: form.repo_url }),
      });
    } catch {
      setRepoCheck({ status: "invalid", entry_point: null, error: "Could not reach the backend" });
      return;
    }

    let data;
    try {
      data = await res.json();
    } catch {
      setRepoCheck({
        status: "invalid",
        entry_point: null,
        error: `Backend returned an unexpected response (HTTP ${res.status}).`,
      });
      return;
    }

    if (res.ok && data.valid) {
      setRepoCheck({ status: "valid", entry_point: data.entry_point, error: null });
    } else {
      setRepoCheck({ status: "invalid", entry_point: null, error: data.error || "Repository check failed" });
    }
  }

  function updateAws(field, value) {
    setForm((f) => ({ ...f, aws: { ...f.aws, [field]: value } }));
  }

  function toggleSelfHosted(value) {
    setForm((f) => ({
      ...f,
      self_hosted_llm: value,
      instance_type: value ? GPU_INSTANCE_TYPES[0] : CPU_INSTANCE_TYPES[0],
      llm_provider: value ? null : LLM_PROVIDERS[0],
      api_key: value ? "" : f.api_key,
    }));
  }

  function updateEnvVar(index, field, value) {
    setForm((f) => {
      const env_vars = [...f.env_vars];
      env_vars[index] = { ...env_vars[index], [field]: value };
      return { ...f, env_vars };
    });
  }

  function addEnvVar() {
    setForm((f) => ({ ...f, env_vars: [...f.env_vars, { key: "", value: "" }] }));
  }

  function removeEnvVar(index) {
    setForm((f) => ({ ...f, env_vars: f.env_vars.filter((_, i) => i !== index) }));
  }

  // Polls GET /deploy/{id}/status every POLL_INTERVAL_MS via setTimeout
  // rescheduling (not setInterval — a slow/hung poll would otherwise pile
  // up overlapping requests). Runs entirely independently of the POST
  // /deploy connection: the backend keeps provisioning in a background
  // task regardless of whether any particular poll succeeds, so a dropped
  // poll just means the UI is stale for one more tick, not that the
  // deployment itself stalls.
  function pollDeployStatus(deploymentId, generation, appInstanceName, region) {
    if (pollGenerationRef.current !== generation) return;

    fetch(`${BACKEND_URL}/deploy/${deploymentId}/status`)
      .then((res) => res.json())
      .then((data) => {
        if (pollGenerationRef.current !== generation) return;

        for (const id of POLLED_STEP_IDS) {
          if (data.steps?.[id]) setStepStatus(id, data.steps[id]);
        }

        if (data.result) {
          // Snapshot the region/name/deployment id actually used for *this*
          // deploy onto the result — reading them live off `form` later
          // would drift if the user keeps editing the form afterward.
          setResult({
            ...data.result,
            region,
            instanceName: appInstanceName,
            deploymentId,
          });
          setDeployCount((c) => c + 1);
          setSubmitting(false);
          return;
        }

        if (data.error) {
          const failedStep = POLLED_STEP_IDS.find((id) => data.steps?.[id] === "error");
          setStepStatus(failedStep || "app_compute", "error", data.error);
          setSubmitting(false);
          return;
        }

        setTimeout(
          () => pollDeployStatus(deploymentId, generation, appInstanceName, region),
          POLL_INTERVAL_MS
        );
      })
      .catch(() => {
        // A blip in reaching our own backend for a poll — not a deployment
        // failure. Just try again; the background task on the server side
        // is unaffected either way.
        setTimeout(
          () => pollDeployStatus(deploymentId, generation, appInstanceName, region),
          POLL_INTERVAL_MS
        );
      });
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (repoCheck.status !== "valid" || submitting) return;
    setResult(null);
    setSubmitting(true);
    setHasStarted(true);
    setSteps(freshSteps());
    const generation = ++pollGenerationRef.current;

    // app_instance_name maps to /deploy's instance_name field, which
    // becomes the instance's Name tag.
    const { app_instance_name, ...formForBackend } = form;
    const payload = {
      ...formForBackend,
      instance_name: app_instance_name.trim() || null,
      env_vars: form.env_vars.filter((v) => v.key.trim() !== ""),
      llm_provider: form.self_hosted_llm ? null : form.llm_provider,
      api_key: form.self_hosted_llm ? null : form.api_key,
    };

    setStepStatus("validate", "active");

    let res;
    try {
      res = await fetch(`${BACKEND_URL}/deploy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch {
      setStepStatus("validate", "error", "Could not reach the backend");
      setSubmitting(false);
      return;
    }

    let data;
    try {
      data = await res.json();
    } catch {
      setStepStatus("validate", "error", `Backend returned an unexpected response (HTTP ${res.status}).`);
      setSubmitting(false);
      return;
    }

    if (!res.ok) {
      const detail = Array.isArray(data?.detail)
        ? data.detail.map((d) => d.msg).join("; ")
        : data?.detail || data?.error || "Deploy failed";

      if (res.status === 400) {
        // validate_repo() failed — covers both repo structure and the
        // reserved-port checks, so both steps stay tied to this one outcome.
        setStepStatus("validate", "error", detail);
      } else {
        // Repo validation passed; no AWS credentials were cached and none
        // were provided — nothing has touched AWS yet.
        setStepStatus("validate", "done");
        setStepStatus("ports", "done");
        setStepStatus("aws_creds", "error", detail);
      }
      setSubmitting(false);
      return;
    }

    // A 202 means validation and the local credential lookup both passed —
    // actual provisioning now runs server-side; poll for its real progress.
    setStepStatus("validate", "done");
    setStepStatus("ports", "done");
    setStepStatus("aws_creds", "done");
    pollDeployStatus(data.deployment_id, generation, app_instance_name.trim() || null, form.aws.region);
  }

  return (
    <div className="card-wrap">
      <div className="card">
      <form onSubmit={handleSubmit} className="form">
        <section className="form-section">
          <h2 className="section-title">Repository</h2>
          <div className="field">
            <label htmlFor="repo_url">GitHub repo link</label>
            <div className="repo-check-row">
              <input
                id="repo_url"
                type="text"
                className="input"
                placeholder="https://github.com/you/your-agent"
                required
                value={form.repo_url}
                onChange={(e) => updateRepoUrl(e.target.value)}
              />
              <button
                type="button"
                className="check-btn"
                disabled={!form.repo_url.trim() || repoCheck.status === "checking"}
                onClick={checkRepo}
              >
                {repoCheck.status === "checking" ? <span className="spinner spinner--dark" /> : "Check"}
              </button>
            </div>
            {repoCheck.status === "valid" && (
              <p className="field-status field-status--ok">
                ✓ Compatible —{" "}
                {repoCheck.entry_point === "compose" ? "docker-compose.yml" : "Dockerfile"} entry point found
              </p>
            )}
            {repoCheck.status === "invalid" && (
              <p className="field-status field-status--error">✗ {repoCheck.error}</p>
            )}
          </div>
        </section>

        <section className="form-section">
          <div className="section-heading">
            <div className="section-title-group">
              <h2 className="section-title">AWS credentials</h2>
              <AwsSetupGuide />
            </div>
            <span className="section-hint">
              Cached locally on your machine after the first deploy — never sent anywhere else.
            </span>
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="access_key_id">Access key ID</label>
              <input
                id="access_key_id"
                type="text"
                className="input"
                required
                value={form.aws.access_key_id}
                onChange={(e) => updateAws("access_key_id", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="secret_access_key">Secret access key</label>
              <input
                id="secret_access_key"
                type="password"
                className="input"
                required
                value={form.aws.secret_access_key}
                onChange={(e) => updateAws("secret_access_key", e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="region">Region</label>
              <select
                id="region"
                className="input"
                value={form.aws.region}
                onChange={(e) => updateAws("region", e.target.value)}
              >
                {AWS_REGIONS.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </section>

        <section className="form-section">
          <h2 className="section-title">Does your code spin up its own LLM?</h2>
          <div className="segmented" role="group" aria-label="Self-hosted LLM">
            <button
              type="button"
              className={`segmented-option ${!form.self_hosted_llm ? "is-active" : ""}`}
              onClick={() => toggleSelfHosted(false)}
            >
              No — external API
            </button>
            <button
              type="button"
              className={`segmented-option ${form.self_hosted_llm ? "is-active" : ""}`}
              onClick={() => toggleSelfHosted(true)}
            >
              Yes — self-hosted
            </button>
          </div>
        </section>

        {form.self_hosted_llm ? (
          <section className="form-section">
            <h2 className="section-title">Compute</h2>
            <div className="field-row">
              <div className="field">
                <label htmlFor="instance_type">EC2 instance (GPU)</label>
                <select
                  id="instance_type"
                  className="select"
                  value={form.instance_type}
                  onChange={(e) => setForm((f) => ({ ...f, instance_type: e.target.value }))}
                >
                  {GPU_INSTANCE_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="ebs_size_gb">EBS storage</label>
                <select
                  id="ebs_size_gb"
                  className="select"
                  value={form.ebs_size_gb}
                  onChange={(e) => setForm((f) => ({ ...f, ebs_size_gb: Number(e.target.value) }))}
                >
                  {EBS_SIZES.map((s) => (
                    <option key={s} value={s}>
                      {s} GB
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="app_instance_name">Instance name</label>
                <input
                  id="app_instance_name"
                  type="text"
                  className="input"
                  placeholder="e.g. my-agent"
                  value={form.app_instance_name}
                  onChange={(e) => setForm((f) => ({ ...f, app_instance_name: e.target.value }))}
                />
              </div>
            </div>
          </section>
        ) : (
          <>
            <section className="form-section">
              <h2 className="section-title">LLM provider</h2>
              <div className="field-row">
                <div className="field">
                  <label htmlFor="llm_provider">Provider</label>
                  <select
                    id="llm_provider"
                    className="select"
                    value={form.llm_provider}
                    onChange={(e) => setForm((f) => ({ ...f, llm_provider: e.target.value }))}
                  >
                    {LLM_PROVIDERS.map((p) => (
                      <option key={p} value={p}>
                        {PROVIDER_LABELS[p]}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="api_key">API key</label>
                  <input
                    id="api_key"
                    type="password"
                    className="input"
                    required
                    value={form.api_key}
                    onChange={(e) => setForm((f) => ({ ...f, api_key: e.target.value }))}
                  />
                </div>
              </div>
            </section>

            <section className="form-section">
              <h2 className="section-title">Compute</h2>
              <div className="field-row">
                <div className="field">
                  <label htmlFor="instance_type">EC2 instance</label>
                  <select
                    id="instance_type"
                    className="select"
                    value={form.instance_type}
                    onChange={(e) => setForm((f) => ({ ...f, instance_type: e.target.value }))}
                  >
                    {CPU_INSTANCE_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="ebs_size_gb">EBS storage</label>
                  <select
                    id="ebs_size_gb"
                    className="select"
                    value={form.ebs_size_gb}
                    onChange={(e) => setForm((f) => ({ ...f, ebs_size_gb: Number(e.target.value) }))}
                  >
                    {EBS_SIZES.map((s) => (
                      <option key={s} value={s}>
                        {s} GB
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="app_instance_name">Instance name</label>
                  <input
                    id="app_instance_name"
                    type="text"
                    className="input"
                    placeholder="e.g. my-agent"
                    value={form.app_instance_name}
                    onChange={(e) => setForm((f) => ({ ...f, app_instance_name: e.target.value }))}
                  />
                </div>
              </div>
            </section>
          </>
        )}

        <details className="advanced">
          <summary>
            Advanced — additional environment variables
            <span className="chevron" />
          </summary>
          <div className="advanced-body">
            {form.env_vars.map((v, i) => (
              <div className="env-row" key={i}>
                <input
                  type="text"
                  className="input"
                  placeholder="KEY"
                  value={v.key}
                  onChange={(e) => updateEnvVar(i, "key", e.target.value)}
                />
                <input
                  type="text"
                  className="input"
                  placeholder="value"
                  value={v.value}
                  onChange={(e) => updateEnvVar(i, "value", e.target.value)}
                />
                <button
                  type="button"
                  className="icon-btn"
                  aria-label="Remove variable"
                  onClick={() => removeEnvVar(i)}
                >
                  ×
                </button>
              </div>
            ))}
            <button type="button" className="link-btn" onClick={addEnvVar}>
              + Add variable
            </button>
          </div>
        </details>

        {repoCheck.status !== "valid" && (
          <p className="section-hint deploy-hint">Check your repository above to enable Deploy.</p>
        )}

        <button type="submit" className="submit-btn" disabled={submitting || repoCheck.status !== "valid"}>
          {submitting && <span className="spinner" />}
          {submitting ? "Deploying…" : "Deploy"}
        </button>

        {hasStarted && <DeploySteps steps={steps} />}

        {result && (
          <div className="banner banner--success">
            <p className="banner-title">Deployed — the link below is live and answering</p>
            <ul className="link-list">
              <li className="link-row">
                <span className="link-label">Agent UI</span>
                <a className="link-chip" href={result.app_url} target="_blank" rel="noreferrer">
                  {result.app_url}
                </a>
              </li>
            </ul>
          </div>
        )}

        {result && (
          <CostMonitor
            key={deployCount}
            costEstimateUrl={`/deploy/${result.deploymentId}/cost-estimate`}
            instanceId={result.app_instance_id}
            instanceName={result.instanceName}
            region={result.region}
          />
        )}
      </form>
      </div>
    </div>
  );
}
