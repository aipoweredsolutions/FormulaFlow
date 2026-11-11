import { useState } from "react";
import { Link } from "react-router-dom";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

const initialForm = {
  recipient_name: "",
  phone_number: "",
  location: "",
  county: "",
  birth_month: "",
  formula_needs: "",
};

async function getErrorMessage(response) {
  const body = await response.json().catch(() => null);
  if (typeof body?.detail === "string") {
    return body.detail;
  }
  if (Array.isArray(body?.detail)) {
    return body.detail.map((error) => error.msg).join(", ");
  }
  return `Request failed (${response.status})`;
}

export default function RecipientForm() {
  const [form, setForm] = useState(initialForm);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [submittedRequest, setSubmittedRequest] = useState(null);
  const currentMonth = new Date().toISOString().slice(0, 7);

  function updateField(event) {
    setForm((current) => ({ ...current, [event.target.name]: event.target.value }));
  }

  async function submitRequest(event) {
    event.preventDefault();
    setIsSubmitting(true);
    setError("");
    setSubmittedRequest(null);

    try {
      const response = await fetch(`${API_BASE_URL}/api/requests`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          birth_month: `${form.birth_month}-01`,
        }),
      });

      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }

      const request = await response.json();
      setForm(initialForm);
      setSubmittedRequest(request);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "We could not submit your request. Please try again.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section className="content-card request-card">
      <div className="eyebrow">Formula support</div>
      <h1>Tell us what your family needs.</h1>
      <p className="intro">
        Share a few details and our team will work to connect you with local
        support.
      </p>

      {submittedRequest && (
        <>
          <div className="notice success" role="status">
            Request received for {submittedRequest.containers_per_month || "a"}{" "}
            container(s) this month.
            {submittedRequest.total_cost_usd
              ? ` Estimated total: USD ${Number(submittedRequest.total_cost_usd).toFixed(2)}. `
              : " We are finding a nearby merchant and will follow up. "}
            Your private reference code is{" "}
            <code className="request-reference-code">
              {submittedRequest.public_reference_code}
            </code>
            . Keep it private to check your status.
          </div>
          <p className="privacy-note status-follow-up">
            <Link to="/status">Check your request status</Link>
          </p>
        </>
      )}
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}

      <form className="request-form" onSubmit={submitRequest}>
        <label>
          Recipient name
          <input
            autoComplete="name"
            maxLength={200}
            name="recipient_name"
            onChange={updateField}
            required
            value={form.recipient_name}
          />
        </label>
        <label>
          Phone number
          <input
            autoComplete="tel"
            maxLength={50}
            name="phone_number"
            onChange={updateField}
            required
            type="tel"
            value={form.phone_number}
          />
        </label>
        <label>
          Location
          <input
            autoComplete="address-level2"
            maxLength={500}
            name="location"
            onChange={updateField}
            placeholder="City, neighborhood, or ZIP code"
            required
            value={form.location}
          />
        </label>
        <label>
          County
          <input
            autoComplete="address-level1"
            maxLength={200}
            name="county"
            onChange={updateField}
            placeholder="County"
            required
            value={form.county}
          />
        </label>
        <label>
          Baby’s birth month and year
          <input
            max={currentMonth}
            name="birth_month"
            onChange={updateField}
            required
            type="month"
            value={form.birth_month}
          />
        </label>
        <p className="privacy-note">
          We ask for month and year only—not the day of birth—to track monthly
          eligibility. Each month is reviewed by our team; support ends at the
          start of the month the child reaches 24 months.
        </p>
        <label>
          Formula needs
          <textarea
            maxLength={5000}
            name="formula_needs"
            onChange={updateField}
            placeholder="Include type, brand, or any other details that may help."
            required
            rows={4}
            value={form.formula_needs}
          />
        </label>
        <button className="primary-button" disabled={isSubmitting} type="submit">
          {isSubmitting ? "Sending request..." : "Submit request"}
        </button>
      </form>
      <p className="privacy-note">
        Your contact details are used only to follow up on this request.
      </p>
    </section>
  );
}
