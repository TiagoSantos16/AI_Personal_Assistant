import React, { useState } from "react";
import { api } from "./api.js";
import { Empty, SourceLink } from "./components.jsx";
import { date, noteUrl } from "./format.js";
export function Memory({ data, run, confirm, openNote }) {
  const [editing, setEditing] = useState(null);
  const [replacement, setReplacement] = useState("");
  const active = data.memory.filter((item) => item.active !== false);
  const groups = [
    ["Preferences", active.filter((m) => m.kind === "preference")],
    ["Personal facts", active.filter((m) => m.kind === "fact")],
    [
      "Personal plans",
      active.filter((m) => !["fact", "preference"].includes(m.kind)),
    ],
    ["Topic recall", data.recalls],
  ];
  return (
    <>
      {groups.map(([title, entries]) => (
        <section className="section" key={title}>
          <div className="section-title">
            <h2>{title}</h2>
            <span className="count">{entries.length}</span>
          </div>
          <div className="panel memory-list">
            {entries.length ? (
              entries.map((item) => (
                <article className="memory-row" key={item.id}>
                  <div className="memory-main">
                    <h3>{item.topic || item.text}</h3>
                    {item.repair_status === "needs_clarification" && (
                      <span className="badge attention">
                        Needs clarification
                      </span>
                    )}
                    <p className="fine-print">
                      {item.topic
                        ? "Bring this source up when you ask about this topic."
                        : `Scope: ${item.scope || "general"}`}{" "}
                      · {date(item.date || item.created, true)}
                    </p>
                    {item.linked_note && (
                      <a
                        href={noteUrl(item.linked_note.name)}
                        onClick={(e) => openNote(e, item.linked_note)}
                      >
                        {item.linked_note.title}
                      </a>
                    )}
                    <SourceLink url={item.source_url || item.url} />
                    <details>
                      <summary>Provenance</summary>
                      <p className="preserve">
                        {item.source_text ||
                          item.request ||
                          item.text ||
                          item.topic}
                      </p>
                      <p className="fine-print">
                        Telegram message: {item.message_id || "not recorded"}
                      </p>
                    </details>
                    {editing === item.id && (
                      <form
                        className="inline-form"
                        onSubmit={async (e) => {
                          e.preventDefault();
                          const result = await run(() =>
                            api(`memory/${encodeURIComponent(item.id)}`, {
                              action: "correct",
                              text: replacement,
                              revision: data.revision,
                            }),
                          );
                          if (result) setEditing(null);
                        }}
                      >
                        <label htmlFor="replacement">
                          Exact replacement statement
                        </label>
                        <textarea
                          id="replacement"
                          value={replacement}
                          onChange={(e) => setReplacement(e.target.value)}
                          maxLength={1000}
                          required
                        />
                        <button className="primary">Save correction</button>
                      </form>
                    )}
                  </div>
                  <div className="memory-tools">
                    {!item.topic && (
                      <button
                        className="text-button"
                        onClick={() => {
                          setEditing(editing === item.id ? null : item.id);
                          setReplacement(item.text);
                        }}
                      >
                        Correct
                      </button>
                    )}
                    <button
                      className="text-button muted"
                      onClick={() =>
                        confirm({
                          title: item.topic
                            ? "Forget this topic recall?"
                            : "Forget this memory?",
                          description:
                            "Existing notes and Telegram messages remain unchanged.",
                          label: "Forget",
                          execute: () =>
                            api(
                              item.topic
                                ? `recall/${encodeURIComponent(item.id)}/forget`
                                : `memory/${encodeURIComponent(item.id)}`,
                              item.topic
                                ? {}
                                : { action: "forget", revision: data.revision },
                            ),
                        })
                      }
                    >
                      Forget
                    </button>
                  </div>
                </article>
              ))
            ) : (
              <Empty>Nothing saved here yet.</Empty>
            )}
          </div>
        </section>
      ))}
    </>
  );
}
