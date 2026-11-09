export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function createApiClient(fetcher = fetch) {
  let csrf = "";

  async function request(path, body, signal) {
    let response;
    try {
      response = await fetcher(`/api/${path}`, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin",
        signal,
        headers:
          body === undefined
            ? {}
            : {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf,
              },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new ApiError(
        "Cannot reach your workspace. Check your connection and try again.",
      );
    }

    let data;
    try {
      data = await response.json();
    } catch {
      throw new ApiError(
        "The server returned an unexpected response. Please try again shortly.",
        response.status,
      );
    }
    if (!response.ok) {
      throw new ApiError(
        typeof data.detail === "string"
          ? data.detail
          : "Please check your input and try again.",
        response.status,
      );
    }
    return data;
  }

  request.setCsrf = (value) => {
    csrf = value;
  };
  return request;
}

export const api = createApiClient();

export function deleteNotes(records) {
  return api("notes/delete", {
    records: records.map(({ name, section }) => ({ name, section })),
  });
}
