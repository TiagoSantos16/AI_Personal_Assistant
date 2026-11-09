import React, { useState } from "react";
import { api } from "./api.js";
import { Brand, Icon } from "./components.jsx";
export function Login({ signedIn }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    try {
      const result = await api("login", {
        password: form.get("password"),
        remember: form.get("remember") === "on",
      });
      api.setCsrf(result.csrf);
      signedIn(result);
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <section className="login-panel">
        <Brand />
        <h1>Your private workspace.</h1>
        <p>
          Sign in to your notes, ideas, and everything you want to remember.
        </p>
        <form onSubmit={submit}>
          <label htmlFor="password">Password</label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            autoFocus
            maxLength={1024}
          />
          <label className="checkbox">
            <input type="checkbox" name="remember" defaultChecked />
            Remember this device for 30 days
          </label>
          {error && (
            <p role="alert" className="error-text">
              {error}
            </p>
          )}
          <button className="primary" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
            <Icon name="arrow" />
          </button>
        </form>
        <p className="fine-print">A little space for what matters.</p>
      </section>
    </main>
  );
}
