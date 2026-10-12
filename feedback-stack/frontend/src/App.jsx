import { useEffect, useState } from "react";
import "./index.css";

// A numeric rating scale (e.g. 1-5, or 1-10) gets a nicer connected-dot
// control than plain pills — detected purely from the option values
// themselves, so it applies to any scale a questionnaire defines, not a
// specific one. No assumption is made about what the endpoints "mean"
// (there's no such field in the schema) — the numbers speak for themselves.
function isNumericScale(options) {
  return options.every((opt) => opt.trim() !== "" && !Number.isNaN(Number(opt)));
}

function QuestionField({ question, onChange }) {
  if (question.options) {
    const groupProps = { role: "radiogroup", "aria-label": question.question };

    if (isNumericScale(question.options)) {
      return (
        <div className="scale" {...groupProps}>
          <div className="scale-track">
            {question.options.map((opt) => (
              <label key={opt} className={`scale-dot ${question.answer === opt ? "is-selected" : ""}`}>
                <input
                  type="radio"
                  name={question.question}
                  value={opt}
                  checked={question.answer === opt}
                  onChange={() => onChange(opt)}
                />
                {opt}
              </label>
            ))}
          </div>
        </div>
      );
    }

    return (
      <div className="segmented" {...groupProps}>
        {question.options.map((opt) => (
          <label key={opt} className={`segmented-option ${question.answer === opt ? "is-selected" : ""}`}>
            <input
              type="radio"
              name={question.question}
              value={opt}
              checked={question.answer === opt}
              onChange={() => onChange(opt)}
            />
            {opt}
          </label>
        ))}
      </div>
    );
  }

  return (
    <textarea
      className="textarea"
      rows={4}
      value={question.answer}
      onChange={(e) => onChange(e.target.value)}
      placeholder={question.optional ? "Optional" : ""}
    />
  );
}

export default function App() {
  const [questions, setQuestions] = useState(null);
  const [version, setVersion] = useState(null);
  const [status, setStatus] = useState("loading"); // loading | ready | submitting | done | error
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch("/api/questions")
      .then((res) => res.json())
      .then((data) => {
        setQuestions(data.questions);
        setVersion(data.version);
        setStatus("ready");
      })
      .catch(() => {
        setError("Could not load the questionnaire. Try refreshing the page.");
        setStatus("error");
      });
  }, []);

  function setAnswer(index, value) {
    setQuestions((qs) => qs.map((q, i) => (i === index ? { ...q, answer: value } : q)));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setStatus("submitting");
    setError(null);

    let res;
    try {
      res = await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // The version this page showed — stays correct even if a newer one is
        // published while the reviewer is still filling it in.
        body: JSON.stringify({ answers: questions, questionnaire_version: version }),
      });
    } catch {
      setError("Could not reach the server. Check your connection and try again.");
      setStatus("ready");
      return;
    }

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      setError(data.detail || "Submission failed. Please review your answers and try again.");
      setStatus("ready");
      return;
    }

    setStatus("done");
  }

  if (status === "loading") {
    return (
      <main className="page">
        <p className="hint">Loading…</p>
      </main>
    );
  }

  if (status === "done") {
    return (
      <main className="page">
        <div className="card-shell">
          <div className="card">
            <div className="card-body">
              <h1 className="title">Thank you</h1>
              <p className="hint">Your feedback has been recorded.</p>
            </div>
          </div>
        </div>
      </main>
    );
  }

  const requiredQuestions = (questions || []).filter((q) => !q.optional);
  const answeredCount = requiredQuestions.filter((q) => q.answer).length;
  const progressPct = requiredQuestions.length
    ? Math.round((answeredCount / requiredQuestions.length) * 100)
    : 100;

  return (
    <main className="page">
      <div className="card-shell">
        <div className="card">
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${progressPct}%` }} />
          </div>
          <div className="card-body">
            <h1 className="title">Feedback</h1>
            <form className="form" onSubmit={handleSubmit}>
              {(questions || []).map((q, i) => (
                <div className="field" key={i}>
                  <label className="label">
                    {q.question}
                    {q.optional && <span className="hint"> (optional)</span>}
                  </label>
                  <QuestionField question={q} onChange={(v) => setAnswer(i, v)} />
                </div>
              ))}

              {error && <p className="error">{error}</p>}

              <button type="submit" className="submit-btn" disabled={status === "submitting"}>
                {status === "submitting" ? "Submitting…" : "Submit feedback"}
              </button>
            </form>
          </div>
        </div>
        <p className="footer-note">Your response is anonymous and only visible to the person who deployed this agent.</p>
      </div>
    </main>
  );
}
