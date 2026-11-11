import { useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const stages = {
  submitted: "Submitted",
  approved: "Approved",
  ready_for_pickup: "Ready for pickup",
  collected: "Collected",
};

async function getErrorMessage(response) {
  const body = await response.json().catch(() => null);
  if (response.status === 404) {
    return "We couldn’t find a trackable request for that reference code. Check the code and try again.";
  }
  if (response.status === 429) {
    return "Too many lookup attempts. Please wait a minute before trying again.";
  }
  if (typeof body?.detail === "string") {
    return body.detail;
  }
  return `Status lookup failed (${response.status}). Please try again.`;
}

export default function StatusPage() {
  const [referenceCode, setReferenceCode] = useState("");
  const [statusResult, setStatusResult] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState("");

  async function checkStatus(event) {
    event.preventDefault();
    setIsLoading(true);
    setError("");
    setStatusResult(null);

    try {
      const response = await fetch(`${API_BASE_URL}/api/public/request-status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reference_code: referenceCode.trim() }),
      });
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }

      setStatusResult(await response.json());
    } catch (lookupError) {
      setError(
        lookupError instanceof Error
          ? lookupError.message
          : "Status lookup failed. Please try again.",
      );
    } finally {
      setIsLoading(false);
    }
  }

  return (
    <section className="content-card status-card">
      <div className="eyebrow">Request tracking</div>
      <h1>Check your request status</h1>
      <p className="intro">
        Enter the reference code you received when you submitted your request.
      </p>

      <form className="request-form" onSubmit={checkStatus}>
        <label>
          Reference code
          <input
            autoComplete="off"
            maxLength={32}
            minLength={32}
            onChange={(event) => setReferenceCode(event.target.value)}
            required
            spellCheck="false"
            value={referenceCode}
          />
        </label>
        <button className="primary-button" disabled={isLoading} type="submit">
          {isLoading ? "Checking status…" : "Check status"}
        </button>
      </form>

      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}

      {statusResult && (
        <section className="status-result" aria-live="polite">
          <div className="eyebrow">Current stage</div>
          <h2>{stages[statusResult.stage]}</h2>
          <p>
            Last updated{" "}
            <time dateTime={statusResult.last_updated_at}>
              {new Intl.DateTimeFormat(undefined, {
                dateStyle: "medium",
                timeStyle: "short",
              }).format(new Date(statusResult.last_updated_at))}
            </time>
          </p>
        </section>
      )}

      <p className="privacy-note">
        Your reference code is private. This page only displays your request’s
        stage and last-updated time.
      </p>
    </section>
  );
}
