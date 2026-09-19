// The one place network access is allowed to happen. See AGENTS.md rule 1.

export type ApiError = { status: number; code: string; message: string };

const BASE_URL = process.env.API_BASE_URL ?? "https://api.internal";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${process.env.API_TOKEN ?? ""}`,
      ...(init.headers ?? {}),
    },
  });

  if (!response.ok) {
    const error: ApiError = await response.json();
    throw Object.assign(new Error(error.message), error);
  }

  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "POST", body: JSON.stringify(body) }),
};
