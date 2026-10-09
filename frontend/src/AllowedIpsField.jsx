import { useEffect, useState } from "react";
import { BACKEND_URL } from "./config";
import { apiFetch } from "./apiClient";

// Textarea text -> the list the backend validates (one entry per line or comma).
export function parseAllowedIps(text) {
  return text
    .split(/[\s,]+/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

// Shared by both deploy forms and the Deployments tab's editor.
export default function AllowedIpsField({ id, value, onChange, disabled }) {
  const [myIp, setMyIp] = useState(null);

  useEffect(() => {
    apiFetch(`${BACKEND_URL}/my-ip`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => setMyIp(data?.ip ?? null))
      .catch(() => {});
  }, []);

  return (
    <div className="field">
      <label htmlFor={id}>Allowed IPs</label>
      <textarea
        id={id}
        className="input allowed-ips-input"
        rows={3}
        placeholder={"Leave empty to allow everyone.\nOne IPv4 address or range per line, e.g. 203.0.113.7 or 203.0.113.0/24"}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="section-hint">
        Reviewers can find their IP at checkip.amazonaws.com.{" "}
        {myIp ? `Your IP (${myIp}) is always included` : "Your own IP is always included"} so you're never locked
        out.
      </span>
    </div>
  );
}
