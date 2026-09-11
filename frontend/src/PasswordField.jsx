import { useState } from "react";

// Shared by every secret-shaped input in the app (AWS secret keys, the LLM
// API key, advanced env var values) — masked by default, with an optional
// reveal toggle so a user can confirm what they actually typed.
export default function PasswordField({ id, value, onChange, required = false, placeholder }) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="password-field">
      <input
        id={id}
        type={visible ? "text" : "password"}
        className="input"
        required={required}
        placeholder={placeholder}
        value={value}
        onChange={onChange}
      />
      <button
        type="button"
        className="password-toggle"
        onClick={() => setVisible((v) => !v)}
        aria-label={visible ? "Hide value" : "Show value"}
        tabIndex={-1}
      >
        {visible ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M3 3l18 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            <path
              d="M9.9 5.1A10.6 10.6 0 0 1 12 5c6.5 0 10 7 10 7a15.6 15.6 0 0 1-3.2 4.1M6.2 6.2C3.6 7.9 2 12 2 12s3.5 7 10 7a9.7 9.7 0 0 0 3.9-.8"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
            <path d="M9.9 14.1a3 3 0 0 0 4.2-4.2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinejoin="round"
            />
            <circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="2" />
          </svg>
        )}
      </button>
    </div>
  );
}
