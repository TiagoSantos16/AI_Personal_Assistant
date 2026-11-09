import React, { useEffect, useRef } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { safeUrl } from "./format.js";

const paths = {
  today: "M3 5h18v16H3z M7 3v4 M17 3v4 M3 10h18 M7 14h3 M14 14h3",
  notes: "M6 3h9l4 4v14H6z M14 3v5h5 M9 12h7 M9 16h7",
  tasks: "M9 6h12 M9 12h12 M9 18h12 M3 5l1 1 2-2 M3 11l1 1 2-2 M3 17l1 1 2-2",
  memory:
    "M9 3a3 3 0 0 0-3 3v1a4 4 0 0 0-2 7 4 4 0 0 0 5 6h1V3z M14 3a3 3 0 0 1 3 3v1a4 4 0 0 1 2 7 4 4 0 0 1-5 6h-1V3z",
  activity: "M3 12h4l3-7 4 14 3-7h4",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M10 3h4l1 3 3 1 3 2v4l-3 2-1 3-3 3h-4l-1-3-3-1-3-2V9l3-2 1-3z",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  link: "M9 15l6-6 M8 16l-2 2a4 4 0 0 1-5-5l4-4a4 4 0 0 1 5 0 M16 8l2-2a4 4 0 0 1 5 5l-4 4a4 4 0 0 1-5 0",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6",
  check: "M5 12l4 4L19 6",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v6l4 2",
  refresh: "M20 8a8 8 0 1 0 0 8 M20 3v6h-6",
  logout: "M9 4H4v16h5 M12 12h9 M17 8l4 4-4 4",
  back: "M19 12H5 M11 6l-6 6 6 6",
};

export function Icon({ name, ...props }) {
  return (
    <svg
      width="19"
      height="19"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      <path d={paths[name] || paths.notes} />
    </svg>
  );
}

export function Mark() {
  return (
    <svg
      className="assistant-mark"
      viewBox="0 0 44 44"
      fill="none"
      stroke="currentColor"
      aria-hidden="true"
    >
      <circle cx="22" cy="22" r="20" opacity=".3" />
      <path
        d="M22 2A20 20 0 0 1 42 22M22 42A20 20 0 0 1 2 22"
        strokeWidth="2"
      />
      <circle cx="22" cy="22" r="13" strokeDasharray="20 7 14 7 20 14" />
      <path d="M22 15 29 22 22 29 15 22Z" />
      <circle cx="22" cy="22" r="2" fill="currentColor" />
    </svg>
  );
}

export function Brand() {
  return (
    <div className="brand">
      <Mark />
      <svg
        className="wordmark"
        viewBox="0 0 177 32"
        role="img"
        aria-label="F.R.I.D.A."
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinejoin="bevel"
      >
        <title>F.R.I.D.A.</title>
        <path d="M3 27V5H23M3 15H19 M39 27V5H53L59 11V15L53 19H39M49 19 60 27 M76 5H88M82 5V27M76 27H88 M104 27V5H116L124 13V19L116 27Z M140 27 150 5H154L164 27M144 19H160" />
        <g className="brand-dots" fill="currentColor" stroke="none">
          {[29, 67, 95, 132, 173].map((x) => (
            <circle key={x} cx={x} cy="27" r="1.5" />
          ))}
        </g>
      </svg>
    </div>
  );
}

export function SourceLink({ url, children = "Original source" }) {
  return safeUrl(url) ? (
    <a
      className="source-link"
      href={safeUrl(url)}
      target="_blank"
      rel="noopener noreferrer"
    >
      <Icon name="link" />
      {children}
    </a>
  ) : null;
}

export function Prose({ children }) {
  return (
    <div className="prose">
      <Markdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ href, children }) =>
            safeUrl(href) ? (
              <a href={safeUrl(href)} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ alt }) => <span>Image: {alt}</span>,
        }}
      >
        {String(children || "")}
      </Markdown>
    </div>
  );
}

export function Status({ note }) {
  const state =
    note.status === "superseded"
      ? "Previous version"
      : note.status === "failed"
        ? "Failed"
        : note.coverage?.length
          ? "Incomplete evidence"
          : (note.review_status || "Review not recorded").replaceAll("_", " ");
  const tone =
    ["needs_review", "unverified"].includes(note.review_status) ||
    note.coverage?.length
      ? "attention"
      : note.status === "failed"
        ? "error"
        : note.review_status === "approved"
          ? "approved"
          : "";
  return <span className={`badge ${tone}`}>{state}</span>;
}

export function Empty({ children }) {
  return <p className="empty">{children}</p>;
}

export function Confirm({ value, busy, close, accept }) {
  const ref = useRef(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) close();
      }}
      aria-labelledby="confirm-title"
    >
      <h2 id="confirm-title">{value.title}</h2>
      <p>{value.description}</p>
      <div className="actions end">
        <button disabled={busy} onClick={close} autoFocus>
          Cancel
        </button>
        <button className="danger" disabled={busy} onClick={accept}>
          {busy ? "Working…" : value.label}
        </button>
      </div>
    </dialog>
  );
}
