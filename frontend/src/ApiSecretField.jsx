import { useState } from "react";
import { getStoredSecret, setStoredSecret } from "./apiClient";
import PasswordField from "./PasswordField.jsx";

// Only relevant once the backend has CONTROL_PLANE_SECRET set (see
// backend/app/core/auth.py) — local/dev with no secret configured never
// needs this open. One paste, once per browser; apiClient.js signs every
// request with it from then on.
export default function ApiSecretField() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(getStoredSecret);
  const [saved, setSaved] = useState(false);

  function save() {
    setStoredSecret(value.trim());
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  return (
    <div className="api-secret-field">
      <button type="button" className="link-btn" onClick={() => setOpen((v) => !v)}>
        API secret {getStoredSecret() ? "(set)" : ""}
      </button>
      {open && (
        <div className="field">
          <label htmlFor="control-plane-secret">Control-plane secret</label>
          <PasswordField
            id="control-plane-secret"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="Only needed if the backend requires one"
          />
          <button type="button" className="submit-btn" onClick={save}>
            {saved ? "Saved" : "Save"}
          </button>
        </div>
      )}
    </div>
  );
}
