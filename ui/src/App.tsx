import { useCallback, useEffect, useState } from "react";
import { api, AuthError, type Health, type ReviewItem } from "./api";
import ReviewCard from "./ReviewCard";

const TOKEN_KEY = "dealdesk.token";

// sessionStorage, not localStorage: the token is gone when the tab closes.
function readToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function saveToken(token: string | null) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked: the token simply lives for this page only */
  }
}

export default function App() {
  const [token, setToken] = useState<string | null>(readToken);
  const [health, setHealth] = useState<Health | null>(null);
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [loading, setLoading] = useState(false);

  const signOut = useCallback((message?: string) => {
    saveToken(null);
    setToken(null);
    setItems(null);
    if (message) setError(message);
  }, []);

  const load = useCallback(
    async (t: string) => {
      setLoading(true);
      setError(null);
      try {
        setItems(await api.reviews(t));
      } catch (e) {
        if (e instanceof AuthError) signOut("That token was not accepted.");
        else setError(e instanceof Error ? e.message : "Could not load the review queue.");
      } finally {
        setLoading(false);
      }
    },
    [signOut],
  );

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  useEffect(() => {
    if (token) void load(token);
  }, [token, load]);

  const onDone = (item: ReviewItem, message: string) => {
    setItems((prev) => prev?.filter((i) => i !== item) ?? null);
    setStatus(message);
  };

  return (
    <div className="app">
      <header className="topbar">
        <div>
          <h1>Deal Desk review queue</h1>
          <p className="muted">Deals the agent wasn't allowed to action on its own.</p>
        </div>
        <div className="topbar-right">
          {health && (
            <span className={`mode ${health.dry_run ? "mode-dry" : "mode-live"}`}>
              {health.dry_run ? "Dry run: approvals write nothing" : "Live: approvals create Salesforce Tasks"}
            </span>
          )}
          {token && (
            <button type="button" className="btn btn-quiet" onClick={() => signOut()}>
              Sign out
            </button>
          )}
        </div>
      </header>

      <main>
        <p className="sr-only" role="status" aria-live="polite">
          {status}
        </p>
        {status && <div className="banner banner-ok">{status}</div>}
        {error && (
          <div className="banner banner-error" role="alert">
            {error}
          </div>
        )}

        {!token ? (
          <TokenForm
            onSubmit={(t) => {
              saveToken(t);
              setToken(t);
            }}
          />
        ) : (
          <>
            <div className="toolbar">
              <h2>
                {items === null ? "Loading…" : `${items.length} waiting for review`}
              </h2>
              <button type="button" className="btn" onClick={() => load(token)} disabled={loading}>
                {loading ? "Refreshing…" : "Refresh"}
              </button>
            </div>
            {items?.length === 0 && <p className="empty">Nothing is waiting for review.</p>}
            <ul className="queue">
              {items?.map((item) => (
                <li key={`${item.run_id}/${item.opportunity_id}`}>
                  <ReviewCard
                    item={item}
                    token={token}
                    live={health ? !health.dry_run : false}
                    onDone={onDone}
                    onAuthError={() => signOut("Your token is no longer valid.")}
                  />
                </li>
              ))}
            </ul>
          </>
        )}
      </main>
    </div>
  );
}

function TokenForm({ onSubmit }: { onSubmit: (token: string) => void }) {
  const [value, setValue] = useState("");
  return (
    <form
      className="card token-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (value.trim()) onSubmit(value.trim());
      }}
    >
      <label htmlFor="token">Review API token</label>
      <p className="muted" id="token-help">
        Stored in this browser tab only, and sent only to this service.
      </p>
      <input
        id="token"
        type="password"
        autoComplete="off"
        aria-describedby="token-help"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      <button type="submit" className="btn btn-primary" disabled={!value.trim()}>
        Open the queue
      </button>
    </form>
  );
}
