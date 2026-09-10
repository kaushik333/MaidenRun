import { useEffect, useState } from "react";
import "./index.css";

function QuestionField({ question, value, onChange }) {
  if (question.type === "multiple_choice") {
    return (
      <div className="options" role="radiogroup" aria-label={question.question}>
        {question.options.map((opt) => (
          <label key={opt} className={`option ${value === opt ? "is-selected" : ""}`}>
            <input
              type="radio"
              name={question.id}
              value={opt}
              checked={value === opt}
              onChange={() => onChange(opt)}
            />
            {opt}
          </label>
        ))}
      </div>
    );
  }

  if (question.type === "scale") {
    const [lo, hi] = question.range || [1, 5];
    const steps = Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
    return (
      <div className="options" role="radiogroup" aria-label={question.question}>
        {steps.map((n) => (
          <label key={n} className={`option option--scale ${value === n ? "is-selected" : ""}`}>
            <input
              type="radio"
              name={question.id}
              value={n}
              checked={value === n}
              onChange={() => onChange(n)}
            />
            {n}
          </label>
        ))}
      </div>
    );
  }

  return (
    <textarea
      className="textarea"
      rows={4}
      value={value || ""}
      onChange={(e) => onChange(e.target.value)}
      placeholder={question.optional ? "Optional" : ""}
    />
  );
}

export default function App() {
  const [questions, setQuestions] = useState(null);
  const [answers, setAnswers] = useState({});
  const [status, setStatus] = useState("loading"); // loading | ready | submitting | done | error
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch("/api/questions")
      .then((res) => res.json())
      .then((data) => {
        setQuestions(data);
        setStatus("ready");
      })
      .catch(() => {
        setError("Could not load the questionnaire. Try refreshing the page.");
        setStatus("error");
      });
  }, []);

  function setAnswer(id, value) {
    setAnswers((a) => ({ ...a, [id]: value }));
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
        body: JSON.stringify({ answers }),
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
        <div className="card">
          <h1 className="title">Thank you</h1>
          <p className="hint">Your feedback has been recorded.</p>
        </div>
      </main>
    );
  }

  return (
    <main className="page">
      <div className="card">
        <h1 className="title">Feedback</h1>
        <form className="form" onSubmit={handleSubmit}>
          {(questions || []).map((q) => (
            <div className="field" key={q.id}>
              <label className="label">
                {q.question}
                {q.optional && <span className="hint"> (optional)</span>}
              </label>
              <QuestionField question={q} value={answers[q.id]} onChange={(v) => setAnswer(q.id, v)} />
            </div>
          ))}

          {error && <p className="error">{error}</p>}

          <button type="submit" className="submit-btn" disabled={status === "submitting"}>
            {status === "submitting" ? "Submitting…" : "Submit feedback"}
          </button>
        </form>
      </div>
    </main>
  );
}
