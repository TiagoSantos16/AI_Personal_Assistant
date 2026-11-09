import React from "react";
import { api } from "./api.js";
import { Icon, Empty, SourceLink, Status } from "./components.jsx";
import { date, noteUrl } from "./format.js";
import { Assistant } from "./assistant.jsx";
import { TaskList } from "./tasks.jsx";
import { NoteList } from "./notes.jsx";
export function Today({
  data,
  records,
  activeTasks,
  messages,
  setMessages,
  busy,
  navigate,
  run,
  confirm,
  openNote,
}) {
  const helper = { run, confirm, openNote };
  return (
    <>
      <Assistant
        messages={messages}
        setMessages={setMessages}
        busy={busy}
        {...helper}
      />
      <div className="today-grid">
        <div>
          <section className="section">
            <div className="section-title">
              <h2>
                Unfinished tasks{" "}
                <span className="count">{activeTasks.length}</span>
              </h2>
              <button className="text-button" onClick={() => navigate("Tasks")}>
                All tasks
                <Icon name="arrow" />
              </button>
            </div>
            <div className="panel">
              <TaskList
                tasks={[...activeTasks].sort((a, b) => a.due - b.due)}
                revision={data.revision}
                limit={5}
                {...helper}
              />
            </div>
          </section>
          <section className="section">
            <div className="section-title">
              <h2>Recently saved</h2>
              <button className="text-button" onClick={() => navigate("Notes")}>
                All notes
                <Icon name="arrow" />
              </button>
            </div>
            <div className="panel">
              <NoteList
                records={records
                  .filter((r) => r.section === "notes")
                  .slice(0, 6)}
                openNote={openNote}
              />
            </div>
          </section>
        </div>
        <aside className="day-column">
          <section className="panel brief">
            <div className="section-title">
              <h2>Morning brief</h2>
              <Icon name="today" />
            </div>
            <p className="brief-time">
              {data.brief.time} <span>· Lisbon</span>
            </p>
            <p className="muted">
              Tasks currently eligible for your brief. Delivery happens in
              Telegram.
            </p>
            {data.brief.task_ids.length ? (
              <ul>
                {data.brief.task_ids.map((id) => (
                  <li key={id}>{data.tasks.find((t) => t.id === id)?.text}</li>
                ))}
              </ul>
            ) : (
              <Empty>No tasks are eligible for the brief right now.</Empty>
            )}
          </section>
          <section className="panel memory-summary">
            <div className="section-title">
              <h2>In memory</h2>
              <Icon name="memory" />
            </div>
            <p className="muted">
              Preferences, personal context, and sources to bring back.
            </p>
            {data.memory
              .filter((m) => m.active !== false)
              .slice(0, 2)
              .map((m) => (
                <p className="memory-snippet" key={m.id}>
                  {m.text}
                </p>
              ))}
            {!data.memory.some((m) => m.active !== false) && (
              <Empty>No personal memory saved yet.</Empty>
            )}
            <button className="text-button" onClick={() => navigate("Memory")}>
              Explore memory
              <Icon name="arrow" />
            </button>
          </section>
        </aside>
      </div>
    </>
  );
}

export function Activity({ data, records, openNote }) {
  return (
    <div className="panel activity-list">
      {data.jobs.map((job) => (
        <article key={job.job_id}>
          <Icon name="activity" />
          <div>
            <h2>{job.title || "Source processing"}</h2>
            <p className="muted">
              {job.status} · {date(job.created, true)}
            </p>
            <SourceLink url={job.url} />
          </div>
        </article>
      ))}
      {records.map((record) => (
        <article key={`${record.section}/${record.name}`}>
          <Icon name={record.status === "failed" ? "activity" : "notes"} />
          <div>
            <a
              href={noteUrl(record.name, record.section)}
              onClick={(e) => openNote(e, record)}
            >
              {record.title}
            </a>
            <p className="muted">
              {date(record.saved || record.created, true)}
            </p>
          </div>
          <Status note={record} />
        </article>
      ))}
      {!records.length && !data.jobs.length && (
        <Empty>No processing activity yet.</Empty>
      )}
    </div>
  );
}

export function Settings({ theme, setTheme, run, refresh, onSignOut }) {
  return (
    <div className="settings panel">
      <section>
        <h2>Appearance</h2>
        <p className="muted">Choose the light that suits your workspace.</p>
        <div className="tabs" role="group" aria-label="Appearance">
          {["dark", "light"].map((value) => (
            <button
              key={value}
              aria-pressed={theme === value}
              onClick={() => setTheme(value)}
            >
              {value === "dark" ? "Dark" : "Light"}
            </button>
          ))}
        </div>
      </section>
      <section>
        <h2>Collection</h2>
        <p className="muted">
          Refresh saved items or rebuild semantic search. A failed rebuild
          retains the previous index.
        </p>
        <div className="actions">
          <button onClick={() => run(refresh, false)}>
            <Icon name="refresh" />
            Refresh collection
          </button>
          <button onClick={() => run(() => api("reindex", {}))}>
            Reindex notes
          </button>
        </div>
      </section>
      <section>
        <h2>Private session</h2>
        <p className="muted">
          Remembered devices stay signed in for 30 days. Sign out to remove this
          browser’s session.
        </p>
        <button onClick={onSignOut}>
          <Icon name="logout" />
          Sign out
        </button>
      </section>
    </div>
  );
}
