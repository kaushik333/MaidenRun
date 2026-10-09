import { useEffect, useState } from "react";
import { BACKEND_URL } from "./config";
import { apiFetch } from "./apiClient";

// Shows which AWS identity deploys will use (an AWS profile, env vars or a
// saved key — see get_session on the backend) and tells the parent form
// whether to show the access-key fields. Shared by both deploy forms.
export default function AwsIdentity({ showKeys, onShowKeysChange }) {
  const [identity, setIdentity] = useState(null);

  useEffect(() => {
    apiFetch(`${BACKEND_URL}/aws-identity`)
      .then((res) => res.json())
      .then((data) => {
        setIdentity(data);
        if (!data.available) onShowKeysChange(true);
      })
      .catch(() => onShowKeysChange(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!identity) return null;

  if (!identity.available) {
    return <p className="section-hint aws-identity-missing">{identity.error}</p>;
  }

  return (
    <div className="aws-identity">
      <span className="field-status field-status--ok">
        ✓ {showKeys ? `The key below will be used instead of your ${identity.source}` : `Using your ${identity.source}`}
      </span>
      <code className="aws-identity-arn">{identity.arn}</code>
      <button type="button" className="link-btn" onClick={() => onShowKeysChange(!showKeys)}>
        {showKeys ? `Use your ${identity.source} instead` : "Use a different access key"}
      </button>
    </div>
  );
}
