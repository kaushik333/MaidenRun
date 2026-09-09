export default function DeploySteps({ steps }) {
  const failed = steps.find((s) => s.status === "error");

  return (
    <div className="form-section">
      <div className="steps" role="list">
        {steps.map((step) => (
          <div className="step" data-status={step.status} role="listitem" key={step.id}>
            <span className="step-dot" aria-hidden="true">
              {step.status === "done" && "✓"}
              {step.status === "error" && "!"}
            </span>
            <span className="step-label">{step.label}</span>
          </div>
        ))}
      </div>
      {failed && <div className="banner banner--error">{failed.label}: {failed.errorMessage}</div>}
    </div>
  );
}
