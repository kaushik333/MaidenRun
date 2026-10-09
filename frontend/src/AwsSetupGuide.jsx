import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { BACKEND_URL } from "./config";
import { apiFetch } from "./apiClient";

function CodeBlock({ json }) {
  const [copied, setCopied] = useState(false);
  const text = JSON.stringify(json, null, 2);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard access can be blocked (permissions, non-HTTPS context, etc.) —
      // the JSON is still fully visible and selectable below, so this is a
      // convenience failure, not a dead end.
    }
  }

  return (
    <div className="setup-code">
      <button type="button" className="setup-copy-btn" onClick={copy}>
        {copied ? "Copied" : "Copy"}
      </button>
      <pre>
        <code>{text}</code>
      </pre>
    </div>
  );
}

const STATIC_STEPS = [
  {
    title: "Create an IAM user",
    body: "IAM console → Users → Create user. Programmatic access only — it doesn't need a console password.",
  },
  { title: "Attach this policy", body: "Add permissions → Create inline policy → paste the JSON below.", code: "iam_policy" },
  {
    title: "Create an instance role",
    body: null, // filled in once setupInfo loads, since it names the exact role
    code: "instance_role_policy",
  },
  {
    title: "Create an access key",
    body: "On that same user: Security credentials → Create access key. Copy both values now — AWS only shows the secret once.",
  },
  {
    title: "Give MaidenRun the key",
    body:
      "Recommended: save it as an AWS CLI profile (aws configure --profile maidenrun), then set " +
      "AWS_PROFILE=maidenrun — plus AWS_CONFIG_DIR=~/.aws under docker compose — in .env. " +
      "Or paste it into the form; it's cached locally on your machine and never asked for again.",
  },
];

export default function AwsSetupGuide() {
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!open || info || error) return;
    apiFetch(`${BACKEND_URL}/setup-info`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then(setInfo)
      .catch(() => setError("Could not load the exact policy JSON right now — see infra/README.md instead."));
  }, [open, info, error]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  return (
    <>
      <button type="button" className="setup-toggle" onClick={() => setOpen(true)}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M12 2 4 5v6c0 5 3.4 8.4 8 11 4.6-2.6 8-6 8-11V5l-8-3Z"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinejoin="round"
          />
        </svg>
        AWS one-time setup
      </button>

      {open &&
        createPortal(
          <div className="setup-backdrop" onClick={() => setOpen(false)}>
            <aside
              className="setup-modal"
              role="dialog"
              aria-label="AWS one-time setup"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="setup-modal-header">
                <h2>AWS one-time setup</h2>
                <button type="button" className="setup-close-btn" onClick={() => setOpen(false)} aria-label="Close">
                  ×
                </button>
              </div>
              <p className="setup-modal-intro">
                Five steps, once, in the AWS account you're deploying into. Full reference version:{" "}
                <code>infra/README.md</code>.
              </p>
              <p className="setup-modal-intro">
                Already done this before? These policies can grow as new features are added — if AWS starts
                rejecting a call with <em>"is not authorized to perform..."</em>, re-paste the (always current)
                JSON below over what you attached previously.
              </p>

              <ol className="setup-steps">
                {STATIC_STEPS.map((step, i) => (
                  <li className="setup-step" key={step.title}>
                    <span className="setup-step-dot">{i + 1}</span>
                    <div className="setup-step-body">
                      <h3>
                        {i === 2 && info ? (
                          <>
                            Create an instance role named exactly{" "}
                            <code>{info.instance_role_name}</code>
                          </>
                        ) : (
                          step.title
                        )}
                      </h3>
                      {i === 2 ? (
                        <p>
                          IAM console → Roles → Create role → Trusted entity: <strong>AWS service</strong> → Use
                          case: <strong>EC2</strong>. Then attach this inline policy:
                        </p>
                      ) : (
                        <p>{step.body}</p>
                      )}
                      {step.code && info && <CodeBlock json={info[step.code]} />}
                      {step.code && error && <p className="setup-step-error">{error}</p>}
                      {step.code && !info && !error && <p className="setup-step-loading">Loading…</p>}
                    </div>
                  </li>
                ))}
              </ol>
            </aside>
          </div>,
          document.body
        )}
    </>
  );
}
