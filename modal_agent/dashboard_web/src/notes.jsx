import React, { useState } from "react";
import { api, deleteNotes } from "./api.js";
import { Icon, Prose, Empty, Status, SourceLink } from "./components.jsx";
import { date, noteUrl } from "./format.js";
function NoteBody({ content }) {
  const body = String(content || "")
    .split(/^\s*\*\*Generation Info:\*\*/m)[0]
    .replace(/^# .*\n?|^\*\*Category:\*\*.*\n?/gm, "")
    .trim();
  const pieces = [];
  const pattern = /<details>\s*<summary>.*?<\/summary>(.*?)<\/details>/gis;
  let cursor = 0;
  for (const match of body.matchAll(pattern)) {
    pieces.push(
      <Prose key={`body-${cursor}`}>{body.slice(cursor, match.index)}</Prose>,
      <details className="spoiler" key={`spoiler-${cursor}`}>
        <summary>Spoiler</summary>
        <Prose>{match[1]}</Prose>
      </details>,
    );
    cursor = match.index + match[0].length;
  }
  return (
    <>
      {pieces}
      <Prose>{body.slice(cursor)}</Prose>
    </>
  );
}

export function NoteList({ records, openNote, selection, setSelection }) {
  if (!records.length)
    return (
      <Empty>
        No matching notes. Save a post or reel in Telegram, or change your
        filters.
      </Empty>
    );
  return (
    <div className="note-list">
      {records.map((note) => (
        <article className="note-row" key={`${note.section}/${note.name}`}>
          {setSelection && (
            <input
              type="checkbox"
              aria-label={`Select ${note.title}`}
              checked={selection.has(`${note.section}/${note.name}`)}
              onChange={() =>
                setSelection((old) => {
                  const next = new Set(old);
                  const key = `${note.section}/${note.name}`;
                  next.has(key) ? next.delete(key) : next.add(key);
                  return next;
                })
              }
            />
          )}
          <div className="note-row-main">
            <a
              className="note-title"
              href={noteUrl(note.name, note.section)}
              onClick={(e) => openNote(e, note)}
            >
              {note.title}
            </a>
            <div className="note-meta">
              <span>{note.category || "General"}</span>
              {note.creator && <span>{note.creator}</span>}
              <time>{date(note.saved || note.created)}</time>
            </div>
          </div>
          <Status note={note} />
          <a
            className="row-arrow"
            href={noteUrl(note.name, note.section)}
            aria-label={`Open ${note.title}`}
            onClick={(e) => openNote(e, note)}
          >
            <Icon name="arrow" />
          </a>
        </article>
      ))}
    </div>
  );
}

function DetailsTable({ rows, columns }) {
  if (!rows?.length) return <Empty>No details were recorded.</Empty>;
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column.replaceAll("_", " ")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td key={column}>
                  {row[column] === null || row[column] === undefined
                    ? "Unavailable"
                    : typeof row[column] === "boolean"
                      ? row[column]
                        ? "Yes"
                        : "No"
                      : String(row[column])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function NoteDetail({ note, back, run, confirm }) {
  const [media, setMedia] = useState(false);
  const generation = note.generation || {};
  const legacy =
    note.legacy && note.content.split(/^\s*\*\*Generation Info:\*\*/m)[1];
  const action =
    note.status === "failed"
      ? "Retry"
      : note.coverage?.length
        ? "Retry missing media"
        : "Redo";
  const identity = { name: note.name, section: note.section };
  return (
    <div className="note-detail">
      <button className="text-button back" onClick={back}>
        <Icon name="back" />
        Back to collection
      </button>
      <header className="page-heading">
        <h1>{note.title}</h1>
        <div className="actions">
          <Status note={note} />
          <span className="muted">
            {note.category} · {date(note.saved || note.created)}
          </span>
        </div>
      </header>
      <div className="source-bar">
        <SourceLink url={note.url}>Source post</SourceLink>
        <SourceLink url={note.creator_url}>Account</SourceLink>
      </div>
      {note.status === "superseded" && (
        <p className="notice">
          Previous version — replaced by redo. This version remains available
          for reference.
        </p>
      )}
      {note.status === "failed" && (
        <p className="notice error-text">
          Processing failed. Review the error details below, then retry.
        </p>
      )}
      {note.index_pending && (
        <p className="notice">
          Saved, but not searchable yet. Reindex in Settings to retry indexing.
        </p>
      )}
      {note.coverage?.length > 0 && (
        <div className="notice attention-text">
          Saved with incomplete source evidence. Some details may be missing.
          <ul>
            {note.coverage.map((entry, index) => (
              <li key={index}>{entry}</li>
            ))}
          </ul>
        </div>
      )}
      <article className="note-body panel">
        <NoteBody content={note.content} />
      </article>
      <section className="note-metadata">
        <details>
          <summary>Source instructions</summary>
          {generation.instructions?.length ? (
            generation.instructions.map((instruction, index) => (
              <div className="instruction" key={index}>
                <p className="preserve">{instruction.text}</p>
                <p className="fine-print">
                  Message {instruction.message_id || "not recorded"} ·{" "}
                  {date(instruction.date, true)}
                </p>
              </div>
            ))
          ) : (
            <Empty>No source instructions were recorded.</Empty>
          )}
        </details>
        <details>
          <summary>Generation details</summary>
          {legacy && <Prose>{legacy}</Prose>}
          {generation.routing_degraded && (
            <p className="notice">Category routing degraded to general.</p>
          )}
          {["description", "transcript", "visual_extraction"].map(
            (key) =>
              generation[key] && (
                <section key={key}>
                  <h3>{key.replaceAll("_", " ")}</h3>
                  <p className="preserve">{generation[key]}</p>
                </section>
              ),
          )}
          {generation.writer_history?.map((draft, index) => (
            <details key={index}>
              <summary>Draft {index + 1}</summary>
              <Prose>{draft}</Prose>
            </details>
          ))}
          {generation.critique_history?.map((review, index) => (
            <section key={index}>
              <h3>Review {index + 1}</h3>
              <p className="preserve">{review}</p>
            </section>
          ))}
          <DetailsTable
            rows={generation.model_log}
            columns={["label", "model", "tried"]}
          />
          {note.media?.length > 0 && (
            <>
              <button onClick={() => setMedia((old) => !old)}>
                {media ? "Hide selected media" : "Show selected media"}
              </button>
              {media && (
                <div className="media-grid">
                  {note.media.map((_, index) => (
                    <a
                      key={index}
                      href={`/api/media/${index}?${new URLSearchParams(identity)}`}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <img
                        src={`/api/media/${index}?${new URLSearchParams(identity)}`}
                        alt={`Selected source image ${index + 1} for ${note.title}`}
                        loading="lazy"
                        onError={(e) => {
                          e.currentTarget.alt =
                            "Selected media could not be loaded. Refresh to try again.";
                        }}
                      />
                      <span>Open image {index + 1} full size</span>
                    </a>
                  ))}
                </div>
              )}
            </>
          )}
        </details>
        <details>
          <summary>Cost details</summary>
          <p>{note.cost_summary}</p>
          <DetailsTable
            rows={note.accounting}
            columns={[
              "step",
              "model",
              "cost",
              "cost_status",
              "duration",
              "success",
            ]}
          />
          <p className="fine-print">
            USD. Reported $0 is free usage; unavailable charges are not zero.
            Failed calls may still be billed.
          </p>
          <h3>Compute usage</h3>
          <DetailsTable
            rows={note.compute?.details}
            columns={["resource", "seconds", "usd_per_second", "estimate"]}
          />
          <p className="fine-print">
            {note.compute?.explanation ||
              "Compute cost unavailable. No rates are assumed."}
          </p>
        </details>
        {(note.diagnostic ||
          note.index_diagnostic ||
          generation.visual_failures?.length > 0) && (
          <details>
            <summary>Owner error details</summary>
            <p className="fine-print">
              Job reference:{" "}
              {(note.job_id || note.version_id || "").slice(0, 12)}
            </p>
            <pre>{note.diagnostic || note.index_diagnostic}</pre>
            {generation.visual_failures?.map((failure, index) => (
              <pre key={index}>
                Media {failure.media_ids?.join(", ")}
                {"\n"}
                {failure.diagnostic}
              </pre>
            ))}
          </details>
        )}
      </section>
      <div className="actions note-actions">
        <button onClick={() => run(() => api("notes/redo", identity))}>
          <Icon name="refresh" />
          {action}
        </button>
        <button
          className="danger-quiet"
          onClick={() =>
            confirm({
              title: "Delete this note?",
              description:
                "This cannot be undone. Linked Telegram messages remain unchanged.",
              label: "Delete note",
              execute: async () => {
                const result = await deleteNotes([identity]);
                back();
                return result;
              },
            })
          }
        >
          Delete note
        </button>
      </div>
    </div>
  );
}

export function Notes({
  records,
  initialCollection = "notes",
  run,
  confirm,
  openNote,
}) {
  const [search, setSearch] = useState("");
  const [section, setSection] = useState(initialCollection);
  const [review, setReview] = useState("All reviews");
  const [category, setCategory] = useState("All categories");
  const [selection, setSelection] = useState(new Set());
  const filteredNotes = records.filter(
    (r) =>
      r.section === section &&
      (category === "All categories" || r.category === category) &&
      (review === "All reviews" ||
        (r.review_status || "not_recorded") === review) &&
      `${r.title} ${r.creator || ""} ${r.category || ""}`
        .toLowerCase()
        .includes(search.toLowerCase().trim()),
  );
  return (
    <>
      <div className="filters">
        <label className="search">
          <Icon name="search" />
          <span className="sr-only">Search notes</span>
          <input
            aria-label="Search notes"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search title, creator, or category"
          />
        </label>
        <select
          aria-label="Collection"
          value={section}
          onChange={(e) => {
            setSection(e.target.value);
            setSelection(new Set());
          }}
        >
          <option value="notes">Notes</option>
          <option value="failed">Error / Failed & previous versions</option>
        </select>
        <select
          aria-label="Category"
          value={category}
          onChange={(e) => setCategory(e.target.value)}
        >
          <option>All categories</option>
          {[...new Set(records.map((r) => r.category).filter(Boolean))]
            .sort()
            .map((value) => (
              <option key={value}>{value}</option>
            ))}
        </select>
        <select
          aria-label="Review status"
          value={review}
          onChange={(e) => setReview(e.target.value)}
        >
          <option>All reviews</option>
          {[
            ...new Set(records.map((r) => r.review_status || "not_recorded")),
          ].map((value) => (
            <option key={value} value={value}>
              {value.replaceAll("_", " ")}
            </option>
          ))}
        </select>
      </div>
      <div className="section-title">
        <p className="muted">
          {filteredNotes.length} notes · Open a title to read
        </p>
        {selection.size > 0 && (
          <button
            className="danger-quiet"
            onClick={() =>
              confirm({
                title: `Delete ${selection.size} selected notes?`,
                description: "This cannot be undone.",
                label: "Delete notes",
                execute: async () => {
                  const result = await deleteNotes(
                    records.filter((r) =>
                      selection.has(`${r.section}/${r.name}`),
                    ),
                  );
                  setSelection(new Set());
                  return result;
                },
              })
            }
          >
            Delete selected ({selection.size})
          </button>
        )}
      </div>
      <div className="panel">
        <NoteList
          records={filteredNotes}
          openNote={openNote}
          selection={selection}
          setSelection={setSelection}
        />
      </div>
    </>
  );
}
