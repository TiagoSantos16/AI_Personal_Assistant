import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { api } from "./api.js";
import { Brand, Confirm, Icon, Mark } from "./components.jsx";
import { noteUrl, isOpenTask } from "./format.js";
import { Login } from "./Login.jsx";
import { Memory } from "./memory.jsx";
import { NoteDetail, Notes } from "./notes.jsx";
import { Tasks } from "./tasks.jsx";
import { Activity, Settings, Today } from "./views.jsx";

const views = {
  Today: "A clear view of what’s next, and what’s worth keeping.",
  Notes: "Ideas worth returning to.",
  Tasks: "Small steps, kept in sight.",
  Memory: "The context that makes this workspace yours.",
  Activity: "What has been saved, processed, and reviewed.",
  Settings: "A workspace that feels right for you.",
};

function initialTheme() {
  try {
    return localStorage.getItem("frida-theme-v1") === "light"
      ? "light"
      : "dark";
  } catch {
    return "dark";
  }
}

export default function App() {
  const [session, setSession] = useState(undefined);
  const [data, setData] = useState(null);
  const [location, setLocation] = useState(window.location.search);
  const [note, setNote] = useState(null);
  const [noteError, setNoteError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const [messages, setMessages] = useState([]);
  const [theme, setTheme] = useState(initialTheme);
  const actionLock = useRef(false);
  const parameters = new URLSearchParams(location);
  const requestedView = parameters.get("view");
  const view =
    Object.keys(views).find((name) => name.toLowerCase() === requestedView) ||
    "Today";
  const target = parameters.get("note");
  const requestedSection = parameters.get("section");
  const collection =
    parameters.get("collection") === "failed" ? "failed" : "notes";
  const records = useMemo(
    () =>
      [...(data?.records || [])].sort(
        (a, b) => (b.saved || b.created || 0) - (a.saved || a.created || 0),
      ),
    [data?.records],
  );
  const activeTasks = useMemo(
    () => (data?.tasks || []).filter(isOpenTask),
    [data?.tasks],
  );

  const clearSession = useCallback(() => {
    api.setCsrf("");
    setSession(null);
    setData(null);
    setNote(null);
    setMessages([]);
    setConfirmation(null);
    setNotice("");
  }, []);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("frida-theme-v1", theme);
    } catch {
      /* Theme works without persistence. */
    }
  }, [theme]);

  useEffect(() => {
    const abort = new AbortController();
    api("session", undefined, abort.signal)
      .then((result) => {
        api.setCsrf(result.csrf);
        setSession(result);
      })
      .catch((failure) => {
        if (failure.name === "AbortError") return;
        setSession(null);
        if (failure.status !== 401) setError(failure.message);
      });
    return () => abort.abort();
  }, []);

  useEffect(() => {
    const change = () => setLocation(window.location.search);
    window.addEventListener("popstate", change);
    return () => window.removeEventListener("popstate", change);
  }, []);

  useEffect(() => {
    if (!session) return;
    const abort = new AbortController();
    api("workspace", undefined, abort.signal)
      .then(setData)
      .catch((failure) => {
        if (failure.name === "AbortError") return;
        setError(failure.message);
        if (failure.status === 401) clearSession();
      });
    return () => abort.abort();
  }, [session, clearSession]);

  useEffect(() => {
    setNote((current) =>
      current?.name === target &&
      (!requestedSection || current.section === requestedSection)
        ? current
        : null,
    );
    setNoteError("");
    if (!target || !data || !session) return;
    const record = data.records.find(
      (item) =>
        item.name === target &&
        (!requestedSection || item.section === requestedSection),
    );
    if (!record) {
      setNote(null);
      setNoteError(
        "This note is no longer available. Return to your collection.",
      );
      return;
    }
    const abort = new AbortController();
    api(
      `note?${new URLSearchParams({ name: target, section: record.section })}`,
      undefined,
      abort.signal,
    )
      .then(setNote)
      .catch((failure) => {
        if (failure.name === "AbortError") return;
        setNote(null);
        setNoteError(failure.message);
        if (failure.status === 401) clearSession();
      });
    return () => abort.abort();
  }, [target, requestedSection, data, session, clearSession]);

  async function refresh() {
    const result = await api("workspace");
    setData(result);
    return result;
  }

  async function run(operation, reload = true) {
    if (actionLock.current) return null;
    actionLock.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await operation();
      if (result.message) setNotice(result.message);
      // Refresh failure must not make a successful mutation look unsuccessful
      // or invite the user to repeat a possibly charged processing request.
      if (reload) {
        try {
          await refresh();
        } catch (failure) {
          setError(
            "The action completed, but the workspace could not refresh. Refresh the collection before continuing.",
          );
          if (failure.status === 401) clearSession();
        }
      }
      return result;
    } catch (failure) {
      setError(failure.message);
      if (failure.status === 401) clearSession();
      return null;
    } finally {
      actionLock.current = false;
      setBusy(false);
    }
  }

  function navigate(next, options = {}) {
    const query = new URLSearchParams(options);
    if (next !== "Today") query.set("view", next.toLowerCase());
    const url = window.location.pathname + (query.size ? `?${query}` : "");
    window.history.pushState({}, "", url);
    setLocation(window.location.search);
    setError("");
    setNotice("");
    window.scrollTo({ top: 0 });
  }

  function openNote(event, record) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0)
      return;
    event.preventDefault();
    window.history.pushState({}, "", noteUrl(record.name, record.section));
    setLocation(window.location.search);
    window.scrollTo({ top: 0 });
  }

  async function signOut() {
    const result = await run(() => api("logout", {}), false);
    if (result) clearSession();
  }

  if (session === undefined)
    return (
      <main className="loading-screen" role="status">
        <Mark />
        <p>Opening your workspace…</p>
      </main>
    );
  if (!session)
    return (
      <>
        {error && (
          <div className="login-error" role="alert">
            {error}
          </div>
        )}
        <Login
          signedIn={(result) => {
            setError("");
            setSession(result);
          }}
        />
      </>
    );

  const helper = { run, confirm: setConfirmation, openNote };
  let content;
  if (!data) {
    content = (
      <div className="loading-block" role="status">
        Loading notes, tasks, and memory…
      </div>
    );
  } else if (target) {
    content = note ? (
      <NoteDetail
        key={`${note.section}/${note.name}`}
        note={note}
        back={() => navigate("Notes", { collection: note.section })}
        {...helper}
      />
    ) : (
      <section className="section">
        <button className="text-button" onClick={() => navigate("Notes")}>
          Back to collection
        </button>
        <p role={noteError ? "alert" : "status"}>
          {noteError || "Opening your note…"}
        </p>
      </section>
    );
  } else {
    const pages = {
      Today: (
        <Today
          data={data}
          records={records}
          activeTasks={activeTasks}
          messages={messages}
          setMessages={setMessages}
          busy={busy}
          navigate={navigate}
          {...helper}
        />
      ),
      Notes: (
        <Notes
          key={collection}
          records={records}
          initialCollection={collection}
          {...helper}
        />
      ),
      Tasks: <Tasks tasks={data.tasks} revision={data.revision} {...helper} />,
      Memory: <Memory data={data} {...helper} />,
      Activity: <Activity data={data} records={records} openNote={openNote} />,
      Settings: (
        <Settings
          theme={theme}
          setTheme={setTheme}
          run={run}
          refresh={refresh}
          onSignOut={signOut}
        />
      ),
    };
    content = (
      <>
        <header className="page-heading">
          <h1>{view === "Today" ? "Your day, in focus." : view}</h1>
          <p>{views[view]}</p>
        </header>
        {pages[view]}
      </>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Brand />
        <p className="workspace-label">Personal workspace</p>
        <nav aria-label="Main navigation">
          {Object.keys(views).map((name) => (
            <button
              key={name}
              aria-current={!target && view === name ? "page" : undefined}
              className={!target && view === name ? "selected" : ""}
              onClick={() => navigate(name)}
            >
              <Icon name={name.toLowerCase()} />
              <span>{name}</span>
              {name === "Tasks" && activeTasks.length > 0 && (
                <span className="nav-count">{activeTasks.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <p>
            Room to think.
            <br />
            Space to remember.
          </p>
          <span>F.R.I.D.A. / Personal intelligence</span>
        </div>
      </aside>
      <main className="main">
        <header className="topbar">
          <span>{target ? "Notes / Saved note" : `Workspace / ${view}`}</span>
          <span>
            {new Intl.DateTimeFormat("en-GB", {
              weekday: "short",
              day: "numeric",
              month: "long",
              timeZone: "Europe/Lisbon",
            }).format(new Date())}
          </span>
        </header>
        {session.preview && (
          <div className="preview-banner">
            Local preview · fixture data · no connected services
          </div>
        )}
        {error && (
          <div className="notice error-text" role="alert">
            {error}
            <button className="text-button" onClick={() => run(refresh, false)}>
              Refresh collection
            </button>
          </div>
        )}
        {notice && (
          <div className="notice" role="status">
            {notice}
          </div>
        )}
        <div className="workspace-content" aria-busy={busy}>
          <fieldset className="workspace-controls" disabled={busy}>
            {content}
          </fieldset>
        </div>
        {confirmation && (
          <Confirm
            value={confirmation}
            busy={busy}
            close={() => setConfirmation(null)}
            accept={async () => {
              const result = await run(confirmation.execute);
              if (result) setConfirmation(null);
            }}
          />
        )}
      </main>
    </div>
  );
}
