import { useEffect, useState } from "react";
import "./index.css";

function QuestionField({ question, onChange }) {
  if (question.options) {
    return (
      <div className="options" role="radiogroup" aria-label={question.question}>
        {question.options.map((opt) => (
          <label key={opt} className={`option ${question.answer === opt ? "is-selected" : ""}`}>
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
        body: JSON.stringify({ answers: questions }),
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
    </main>
  );
}
