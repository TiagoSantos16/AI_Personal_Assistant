const day = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  timeZone: "Europe/Lisbon",
});
const timestamp = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "Europe/Lisbon",
});
export function date(value, full = false) {
  const instant = new Date(Number(value) * 1000);
  return value && Number.isFinite(instant.getTime())
    ? (full ? timestamp : day).format(instant)
    : "Date not recorded";
}
export const safeUrl = (value) => {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
};
export const noteUrl = (name, section = "notes") =>
  `?${new URLSearchParams({ note: name, section })}`;
export const isOpenTask = (task) =>
  !["done", "cancelled"].includes(task.status);
export const isSnoozedTask = (task) =>
  task.due > Date.now() / 1000 &&
  task.changes?.at(-1)?.command === "reschedule";
