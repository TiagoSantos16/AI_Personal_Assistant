import assert from "node:assert/strict";
import { test } from "node:test";
import { ApiError, createApiClient } from "../src/api.js";
import { date, isOpenTask, isSnoozedTask, safeUrl } from "../src/format.js";

test("API errors retain status without exposing proxy HTML or validation input", async () => {
  const proxy = createApiClient(
    async () =>
      new Response("<html>private proxy details</html>", { status: 502 }),
  );
  await assert.rejects(
    proxy("workspace"),
    (error) =>
      error instanceof ApiError &&
      error.status === 502 &&
      !error.message.includes("private"),
  );
  const invalid = createApiClient(async () =>
    Response.json({ detail: [{ input: "private value" }] }, { status: 422 }),
  );
  await assert.rejects(
    invalid("notes/delete", {}),
    (error) => error.status === 422 && !error.message.includes("private value"),
  );
});

test("client isolates CSRF state and propagates cancellation", async () => {
  const calls = [];
  const client = createApiClient(async (url, options) => {
    calls.push({ url, options });
    return Response.json({ ok: true });
  });
  client.setCsrf("csrf-token");
  await client("reindex", {});
  assert.equal(calls[0].options.headers["X-CSRF-Token"], "csrf-token");
  assert.equal(calls[0].options.credentials, "same-origin");
  await client("workspace");
  assert.equal(calls[1].options.body, undefined);
  const canceled = createApiClient(async () => {
    throw new DOMException("Canceled", "AbortError");
  });
  await assert.rejects(
    canceled("workspace"),
    (error) => error.name === "AbortError",
  );
});

test("links reject executable protocols and embedded credentials", () => {
  assert.equal(safeUrl("javascript:alert(1)"), undefined);
  assert.equal(safeUrl("https://user:password@example.com"), undefined);
  assert.equal(safeUrl("https://example.com/note"), "https://example.com/note");
  assert.equal(date("invalid"), "Date not recorded");
});

test("snoozed tasks stay distinct from completed tasks and future active tasks", () => {
  const task = { status: "pending", due: Date.now() / 1000 + 86400 };
  assert.equal(isOpenTask(task), true);
  assert.equal(isSnoozedTask(task), false);
  assert.equal(
    isSnoozedTask({ ...task, changes: [{ command: "reschedule" }] }),
    true,
  );
  assert.equal(isOpenTask({ ...task, status: "done" }), false);
});
