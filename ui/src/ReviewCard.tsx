import { useId, useState } from "react";
import { api, ApiError, AuthError, type ReviewItem } from "./api";

const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

interface Props {
  item: ReviewItem;
  token: string;
  live: boolean;
  onDone: (item: ReviewItem, message: string) => void;
  onAuthError: () => void;
}

export default function ReviewCard({ item, token, live, onDone, onAuthError }: Props) {
  const a = item.assessment;
  const id = useId();
  // Without a valid model output there is no draft to approve: the reviewer must write the Task.
  const [editing, setEditing] = useState(!a);
  const [subject, setSubject] = useState("");
  const [description, setDescription] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const writesTask = editing || a?.status === "at_risk";
  const editIncomplete = editing && (!subject.trim() || !description.trim());

  async function act(action: "approve" | "reject") {
    setBusy(action);
    setError(null);
    try {
      const result =
        action === "approve"
          ? await api.approve(token, item, {
              note: note || undefined,
              ...(editing ? { subject: subject.trim(), description: description.trim() } : {}),
            })
          : await api.reject(token, item, note);
      const name = item.opportunity_name ?? item.opportunity_id;
      onDone(
        item,
        action === "reject"
          ? `Rejected: ${name}.`
          : result.task_id
            ? `Approved: ${name}. Task ${result.task_id} created in Salesforce.`
            : `Approved: ${name}. ${result.dry_run ? "Dry run, so nothing was written." : "No Task needed."}`,
      );
    } catch (e) {
      if (e instanceof AuthError) return onAuthError();
      setError(e instanceof ApiError || e instanceof Error ? e.message : "Something went wrong.");
      setBusy(null);
    }
  }

  return (
    <article className="card" aria-labelledby={`${id}-title`}>
      <div className="card-head">
        <div>
          <h3 id={`${id}-title`}>{item.opportunity_name ?? item.opportunity_id}</h3>
          <p className="muted small">
            {item.amount != null ? money.format(item.amount) : "Amount unknown"}
            {" · "}
            {a ? `${a.status === "at_risk" ? "At risk" : "Healthy"}, confidence ${a.confidence.toFixed(2)}` : "No valid model output"}
          </p>
        </div>
        {item.rule_flags.length > 0 && (
          <ul className="chips" aria-label="Rule flags">
            {item.rule_flags.map((f) => (
              <li key={f} className="chip">
                {f.replaceAll("_", " ")}
              </li>
            ))}
          </ul>
        )}
      </div>

      <section className="why">
        <h4>Why it needs a human</h4>
        <ul>
          {item.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        {item.error && <p className="small error-text">Model error: {item.error}</p>}
      </section>

      {a && (
        <details>
          <summary>What the agent proposed</summary>
          {a.risk_reasons.length > 0 && (
            <>
              <h4>Risk reasons</h4>
              <ul>
                {a.risk_reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </>
          )}
          <h4>Recommended next step</h4>
          <p>{a.next_step || "None"}</p>
          {a.follow_up_email && (
            <>
              <h4>Draft email to {a.follow_up_email.to_contact_name ?? "(no contact on this deal)"}</h4>
              <div className="email">
                <p className="email-subject">{a.follow_up_email.subject}</p>
                <p className="email-body">{a.follow_up_email.body}</p>
              </div>
            </>
          )}
          <h4>Evidence it cited</h4>
          <table>
            <thead>
              <tr>
                <th scope="col">Salesforce field</th>
                <th scope="col">Value</th>
              </tr>
            </thead>
            <tbody>
              {a.evidence.map((e) => (
                <tr key={e.field}>
                  <td>
                    <code>{e.field}</code>
                  </td>
                  <td>{e.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted small">
            Run {item.run_id} · {item.model ?? "no model"} · prompt {item.prompt_version} ·{" "}
            {new Date(item.ts).toLocaleString()}
          </p>
        </details>
      )}

      <div className="actions">
        {a && (
          <label className="check">
            <input type="checkbox" checked={editing} onChange={(e) => setEditing(e.target.checked)} />
            Write the Task myself instead of using the agent's draft
          </label>
        )}
        {editing && (
          <div className="edit">
            <label htmlFor={`${id}-subject`}>Task subject</label>
            <input id={`${id}-subject`} value={subject} maxLength={200} onChange={(e) => setSubject(e.target.value)} />
            <label htmlFor={`${id}-description`}>Task description</label>
            <textarea
              id={`${id}-description`}
              rows={5}
              value={description}
              maxLength={30000}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
        )}
        <label htmlFor={`${id}-note`}>Note for the audit log (optional)</label>
        <textarea id={`${id}-note`} rows={2} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />

        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
        <div className="buttons">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy !== null || editIncomplete}
            onClick={() => act("approve")}
          >
            {busy === "approve"
              ? "Approving…"
              : writesTask && live
                ? "Approve and create Task"
                : writesTask
                  ? "Approve (dry run)"
                  : "Approve: no follow-up needed"}
          </button>
          <button type="button" className="btn" disabled={busy !== null} onClick={() => act("reject")}>
            {busy === "reject" ? "Rejecting…" : "Reject"}
          </button>
        </div>
      </div>
    </article>
  );
}
