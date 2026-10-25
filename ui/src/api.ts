// Typed client for the Deal Desk review API. Same origin as the service, bearer-token auth.

export interface Evidence {
  field: string;
  value: string;
}

export interface Assessment {
  opportunity_id: string;
  status: "healthy" | "at_risk";
  risk_reasons: string[];
  confidence: number;
  evidence: Evidence[];
  next_step: string;
  follow_up_email: { to_contact_name: string | null; subject: string; body: string } | null;
  task_due_in_days: number;
}

export interface ReviewItem {
  run_id: string;
  opportunity_id: string;
  opportunity_name: string | null;
  amount: number | null;
  ts: string;
  status: "healthy" | "at_risk" | null;
  confidence: number | null;
  reasons: string[];
  rule_flags: string[];
  assessment: Assessment | null;
  error: string | null;
  model: string | null;
  prompt_version: string;
}

export interface Health {
  ok: boolean;
  model: string;
  prompt_version: string;
  dry_run: boolean;
}

export interface ReviewResult {
  review_id: string;
  action: "approve" | "reject";
  reviewer: string;
  task_id: string | null;
  dry_run: boolean;
}

export interface ApproveBody {
  note?: string;
  subject?: string;
  description?: string;
}

/** 401: the token is missing or wrong. The UI drops it and asks again. */
export class AuthError extends Error {}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(token: string | null, path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  if (res.status === 401) throw new AuthError("The token was not accepted.");
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b: { detail?: unknown }) => (typeof b.detail === "string" ? b.detail : null))
      .catch(() => null);
    throw new ApiError(res.status, detail ?? `Request failed (${res.status})`);
  }
  return (await res.json()) as T;
}

const itemPath = (item: ReviewItem, action: "approve" | "reject") =>
  `/reviews/${encodeURIComponent(item.run_id)}/${encodeURIComponent(item.opportunity_id)}/${action}`;

export const api = {
  health: () => request<Health>(null, "/health"),
  reviews: (token: string) => request<ReviewItem[]>(token, "/reviews"),
  approve: (token: string, item: ReviewItem, body: ApproveBody) =>
    request<ReviewResult>(token, itemPath(item, "approve"), { method: "POST", body: JSON.stringify(body) }),
  reject: (token: string, item: ReviewItem, note: string) =>
    request<ReviewResult>(token, itemPath(item, "reject"), {
      method: "POST",
      body: JSON.stringify({ note: note || undefined }),
    }),
};
