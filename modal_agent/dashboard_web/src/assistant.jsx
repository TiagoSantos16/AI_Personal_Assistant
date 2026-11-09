import React, { useState } from "react";
import { api } from "./api.js";
import { Icon, Mark, Prose } from "./components.jsx";
import { noteUrl } from "./format.js";
export function Assistant({ messages, setMessages, run, busy, openNote }) {
  const [question, setQuestion] = useState("");
  async function ask(event) {
    event.preventDefault();
    if (!question.trim()) return;
    const value = question.trim();
    let length = 0;
    const history = messages
      .slice(-12)
      .reverse()
      .filter((m) => {
        length += m.content.length;
        return length <= 12000;
      })
      .reverse()
      .map(({ role, content }) => ({ role, content }));
    const result = await run(
      () => api("chat", { question: value, history }),
      false,
    );
    if (result) {
      setMessages((old) => [
        ...old,
        { role: "user", content: value },
        { role: "assistant", content: result.answer, sources: result.sources },
      ]);
      setQuestion("");
    }
  }
  return (
    <section className="assistant panel" aria-labelledby="assistant-title">
      <div className="assistant-heading">
        <Mark />
        <div>
          <h2 id="assistant-title">A thought, a question, a next step.</h2>
          <p>Ask your saved notes and personal memory.</p>
        </div>
      </div>
      <form className="composer" onSubmit={ask}>
        <label className="sr-only" htmlFor="question">
          Ask F.R.I.D.A.
        </label>
        <textarea
          id="question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="What would you like to think through?"
          maxLength={4000}
          rows={2}
          disabled={busy}
        />
        <div className="composer-footer">
          <span>Grounded in what you’ve saved</span>
          <button
            className="primary icon-button"
            title="Ask F.R.I.D.A."
            aria-label="Ask F.R.I.D.A."
            disabled={busy || !question.trim()}
          >
            <Icon name="arrow" />
          </button>
        </div>
      </form>
      {busy && (
        <p role="status" className="muted">
          Working on your request…
        </p>
      )}
      {messages.length > 0 && (
        <div className="conversation">
          {messages.map((message, index) => (
            <article key={index} className={`message ${message.role}`}>
              <h3>{message.role === "user" ? "You" : "F.R.I.D.A."}</h3>
              <Prose>{message.content}</Prose>
              {message.sources?.length > 0 && (
                <details>
                  <summary>Sources · {message.sources.length}</summary>
                  <div className="citation-list">
                    {message.sources.map((source) => (
                      <a
                        key={source.label}
                        href={noteUrl(source.name)}
                        onClick={(e) => openNote(e, source)}
                      >
                        <span>{source.label}</span>
                        {source.title}
                      </a>
                    ))}
                  </div>
                </details>
              )}
            </article>
          ))}
          <button
            className="text-button"
            disabled={busy}
            onClick={() => setMessages([])}
          >
            Clear conversation
          </button>
        </div>
      )}
    </section>
  );
}
