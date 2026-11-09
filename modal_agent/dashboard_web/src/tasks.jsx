import React, { useState } from "react";
import { api } from "./api.js";
import { Icon, Empty, SourceLink } from "./components.jsx";
import { date, noteUrl, isOpenTask, isSnoozedTask } from "./format.js";
export function TaskList({ tasks, revision, run, confirm, openNote, limit }) {
  const [editing, setEditing] = useState(null);
  const [timing, setTiming] = useState("");
  const act = (task, action, extra = {}) =>
    run(() =>
      api(`tasks/${encodeURIComponent(task.id)}`, {
        action,
        revision,
        ...extra,
      }),
    );
  if (!tasks.length)
    return (
      <Empty>
        No tasks here. Ask F.R.I.D.A. in Telegram to remind you about something.
      </Empty>
    );
  return (
    <div className="task-list">
      {tasks.slice(0, limit || tasks.length).map((task) => (
        <article className="task-row" key={task.id}>
          <div className="task-line">
            {!["done", "cancelled"].includes(task.status) ? (
              <button
                className="complete-button"
                aria-label={`Complete ${task.text}`}
                onClick={() => act(task, "done")}
              >
                <Icon name="check" />
              </button>
            ) : (
              <span className="completed-icon">
                <Icon name="check" />
              </span>
            )}
            <div className="task-main">
              <p>{task.text}</p>
              <div className="note-meta">
                <span>
                  <Icon name="clock" />
                  {date(task.due, true)} · Lisbon
                </span>
                <span>
                  {task.status === "done"
                    ? "Completed"
                    : task.status === "notified"
                      ? "Reminded · still open"
                      : task.status}
                </span>
                {task.kind === "notification" && (
                  <span>One-off notification</span>
                )}
              </div>
              {task.repair_status === "needs_clarification" && (
                <p className="attention-text">
                  Needs clarification in Telegram
                </p>
              )}
              <div className="task-links">
                {task.linked_note && (
                  <a
                    href={noteUrl(task.linked_note.name)}
                    onClick={(e) => openNote(e, task.linked_note)}
                  >
                    {task.linked_note.title}
                  </a>
                )}
                <SourceLink url={task.source_url} />
              </div>
            </div>
            {!["done", "cancelled"].includes(task.status) && (
              <div className="task-tools">
                <button
                  className="text-button"
                  onClick={() => {
                    setEditing(editing === task.id ? null : task.id);
                    setTiming("");
                  }}
                >
                  Snooze
                </button>
                {["failed", "uncertain"].includes(task.status) && (
                  <button
                    className="text-button"
                    onClick={() => act(task, "retry")}
                  >
                    Retry delivery
                  </button>
                )}
                <button
                  className="text-button muted"
                  onClick={() =>
                    confirm({
                      title: "Cancel this task?",
                      description: task.text,
                      label: "Cancel task",
                      execute: () =>
                        api(`tasks/${encodeURIComponent(task.id)}`, {
                          action: "cancel",
                          revision,
                        }),
                    })
                  }
                >
                  Cancel task
                </button>
              </div>
            )}
          </div>
          {editing === task.id && (
            <form
              className="inline-form"
              onSubmit={async (e) => {
                e.preventDefault();
                const result = await act(task, "reschedule", { timing });
                if (result) setEditing(null);
              }}
            >
              <label htmlFor={`snooze-${task.id}`}>
                New time · Europe/Lisbon
              </label>
              <div className="actions">
                <input
                  id={`snooze-${task.id}`}
                  value={timing}
                  onChange={(e) => setTiming(e.target.value)}
                  placeholder="tomorrow at 18:00"
                  required
                  maxLength={100}
                />
                <button className="primary">Snooze task</button>
              </div>
              <p className="fine-print">
                Use “tomorrow at 18:00” or “on 2026-12-10 at 09:00”.
              </p>
            </form>
          )}
        </article>
      ))}
    </div>
  );
}

export function Tasks({ tasks, revision, run, confirm, openNote }) {
  const [taskFilter, setTaskFilter] = useState("Active");
  const activeTasks = tasks.filter(isOpenTask);
  const filteredTasks =
    taskFilter === "Completed"
      ? tasks.filter((t) => t.status === "done")
      : taskFilter === "Snoozed"
        ? activeTasks.filter(isSnoozedTask)
        : activeTasks.filter((t) => !isSnoozedTask(t));
  const helper = { run, confirm, openNote };
  return (
    <>
      <div className="tabs" role="group" aria-label="Task status">
        {["Active", "Completed", "Snoozed"].map((value) => (
          <button
            aria-pressed={taskFilter === value}
            key={value}
            onClick={() => setTaskFilter(value)}
          >
            {value}
          </button>
        ))}
      </div>
      <div className="panel">
        <TaskList
          tasks={[...filteredTasks].sort((a, b) => a.due - b.due)}
          revision={revision}
          {...helper}
        />
      </div>
    </>
  );
}
