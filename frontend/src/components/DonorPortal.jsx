import { useEffect, useRef, useState } from "react";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const PAYPAL_CLIENT_ID = import.meta.env.VITE_PAYPAL_CLIENT_ID || "";
const PAYPAL_CONTAINER_ID = "paypal-donation-buttons";
const DONATION_PRESETS = ["10", "25", "50", "100"];

let paypalSdkPromise;

function loadPayPalSdk(clientId) {
  if (window.paypal?.Buttons) {
    return Promise.resolve();
  }
  if (paypalSdkPromise) {
    return paypalSdkPromise;
  }

  const script = document.createElement("script");
  const parameters = new URLSearchParams({
    "client-id": clientId,
    components: "buttons",
    currency: "USD",
    intent: "capture",
  });
  script.src = `https://www.paypal.com/sdk/js?${parameters.toString()}`;
  script.async = true;
  script.dataset.formulaflowPaypal = "true";

  paypalSdkPromise = new Promise((resolve, reject) => {
    script.addEventListener("load", () => {
      if (window.paypal?.Buttons) {
        resolve();
      } else {
        paypalSdkPromise = undefined;
        reject(new Error("PayPal Checkout did not initialize."));
      }
    });
    script.addEventListener("error", () => {
      paypalSdkPromise = undefined;
      script.remove();
      reject(new Error("Could not load PayPal Checkout. Check the client ID and try again."));
    });
    document.head.appendChild(script);
  });
  return paypalSdkPromise;
}

async function requestJson(path, options) {
  const response = await fetch(`${API_BASE_URL}${path}`, options);
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("FormulaFlow returned an invalid response. Please try again.");
  }
  if (!response.ok) {
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "FormulaFlow could not process this donation. Please try again.",
    );
  }
  return result;
}

export default function DonorPortal() {
  const [amount, setAmount] = useState("25");
  const [sdkReady, setSdkReady] = useState(false);
  const [sdkLoading, setSdkLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [receipt, setReceipt] = useState(null);
  const paypalContainerRef = useRef(null);
  const parsedAmount = Number(amount);
  const validAmount =
    /^\d+(?:\.\d{1,2})?$/.test(amount) &&
    Number.isFinite(parsedAmount) &&
    parsedAmount >= 1 &&
    parsedAmount <= 10000;

  useEffect(() => {
    if (!PAYPAL_CLIENT_ID) {
      setSdkLoading(false);
      setError(
        "PayPal donations are not configured. Please contact the FormulaFlow team.",
      );
      return undefined;
    }

    let active = true;
    loadPayPalSdk(PAYPAL_CLIENT_ID)
      .then(() => {
        if (active) {
          setSdkReady(true);
          setSdkLoading(false);
          setError("");
        }
      })
      .catch((loadError) => {
        if (active) {
          setSdkLoading(false);
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Could not load PayPal Checkout.",
          );
        }
      });

    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const container = paypalContainerRef.current;
    if (!container) {
      return undefined;
    }
    container.replaceChildren();
    if (!sdkReady || !validAmount || !window.paypal?.Buttons) {
      return undefined;
    }

    let active = true;
    setError("");
    Promise.resolve(
      window.paypal
        .Buttons({
          createOrder: async () => {
            setError("");
            setNotice("");
            setReceipt(null);
            const result = await requestJson("/api/donations/orders", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ amount }),
            });
            return result.order_id;
          },
          onApprove: async (data) => {
            const result = await requestJson(
              `/api/donations/orders/${encodeURIComponent(data.orderID)}/capture`,
              { method: "POST" },
            );
            if (result.status === "COMPLETED" && result.donation_id) {
              if (active) {
                setReceipt({
                  amount: result.amount,
                  captureId: result.capture_id,
                });
                setNotice("");
                setError("");
              }
              return;
            }
            if (active) {
              setNotice(
                "PayPal accepted the payment, but capture is still pending. FormulaFlow will add it to the ledger once PayPal confirms it.",
              );
            }
          },
          onCancel: () => {
            if (active) {
              setNotice("Checkout canceled. No donation was recorded.");
              setError("");
            }
          },
          onError: (checkoutError) => {
            if (active) {
              setError(
                checkoutError instanceof Error
                  ? checkoutError.message
                  : "PayPal checkout could not be completed. Please try again.",
              );
            }
          },
        })
        .render(`#${PAYPAL_CONTAINER_ID}`),
    ).catch((renderError) => {
      if (active) {
        setError(
          renderError instanceof Error
            ? renderError.message
            : "Could not render the PayPal donation button.",
        );
      }
    });

    return () => {
      active = false;
      container.replaceChildren();
    };
  }, [amount, sdkReady, validAmount]);

  return (
    <section className="mx-auto my-10 w-full max-w-5xl px-4 sm:my-16 sm:px-6">
      <div className="overflow-hidden rounded-[2rem] bg-white shadow-[0_24px_80px_rgba(27,65,48,0.12)] ring-1 ring-emerald-950/5">
        <div className="relative overflow-hidden bg-gradient-to-br from-emerald-950 via-emerald-900 to-teal-800 px-6 py-10 text-white sm:px-12 sm:py-14">
          <div className="absolute -right-20 -top-28 h-72 w-72 rounded-full bg-lime-200/10 blur-2xl" />
          <div className="relative max-w-2xl">
            <span className="inline-flex items-center gap-2 rounded-full border border-white/20 bg-white/10 px-3 py-1.5 text-xs font-semibold uppercase tracking-[0.16em] text-emerald-100">
              <span aria-hidden="true">✦</span> Give a little, nourish a lot
            </span>
            <h1 className="mt-6 text-4xl font-semibold leading-tight tracking-tight text-white sm:text-5xl">
              Help a little one
              <span className="block text-lime-200">grow a little stronger.</span>
            </h1>
            <p className="mt-5 max-w-xl text-base leading-7 text-emerald-50/85 sm:text-lg">
              Your gift helps families find the formula they need, when they
              need it most. Choose your contribution and complete it securely
              through PayPal.
            </p>
          </div>
          <div
            aria-hidden="true"
            className="absolute bottom-7 right-12 hidden h-32 w-32 items-center justify-center rounded-full border border-white/15 bg-white/5 text-6xl sm:flex"
          >
            ♡
          </div>
        </div>

        <div className="grid gap-10 px-6 py-8 sm:px-12 sm:py-11 lg:grid-cols-[1.1fr_0.9fr]">
          <div>
            <div className="text-sm font-bold uppercase tracking-[0.14em] text-emerald-800">
              Support families
            </div>
            <h2 className="mt-2 text-2xl font-semibold tracking-tight text-slate-900">
              Every contribution makes a difference.
            </h2>
            <p className="mt-4 max-w-lg text-sm leading-6 text-slate-600">
              Completed PayPal donations are recorded in FormulaFlow&apos;s
              donation ledger and credited to the formula support pool.
            </p>
          </div>

          <aside className="h-fit rounded-3xl bg-slate-50 p-5 ring-1 ring-slate-200 sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-sm font-semibold text-slate-600">
                  Donate with PayPal
                </div>
                <h2 className="mt-2 text-2xl font-bold tracking-tight text-slate-950">
                  Your donation
                </h2>
              </div>
              <span
                aria-hidden="true"
                className="grid h-11 w-11 place-items-center rounded-2xl bg-emerald-100 text-xl text-emerald-800"
              >
                ♥
              </span>
            </div>
            <label
              className="mt-5 block text-sm font-medium text-slate-700"
              htmlFor="donation-amount"
            >
              Donation amount (USD)
            </label>
            <div className="mt-2 grid grid-cols-4 gap-2">
              {DONATION_PRESETS.map((preset) => (
                <button
                  aria-pressed={amount === preset}
                  className={`rounded-xl border px-2 py-2 text-sm font-semibold transition ${
                    amount === preset
                      ? "border-emerald-700 bg-emerald-50 text-emerald-800"
                      : "border-slate-200 bg-white text-slate-700 hover:border-emerald-400"
                  }`}
                  key={preset}
                  onClick={() => setAmount(preset)}
                  type="button"
                >
                  ${preset}
                </button>
              ))}
            </div>
            <div className="relative mt-3">
              <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-slate-500">
                $
              </span>
              <input
                aria-describedby="donation-amount-help"
                className="w-full rounded-xl border border-slate-300 bg-white py-2.5 pl-8 pr-3 text-sm text-slate-900 shadow-sm outline-none focus:border-emerald-600 focus:ring-2 focus:ring-emerald-600/20"
                id="donation-amount"
                inputMode="decimal"
                max="10000"
                min="1"
                onChange={(event) => setAmount(event.target.value)}
                step="0.01"
                type="number"
                value={amount}
              />
            </div>
            <p className="mt-2 text-xs text-slate-500" id="donation-amount-help">
              Enter an amount from $1 to $10,000 USD.
            </p>
            <div className="my-6 border-t border-slate-200" />
            <div
              aria-live="polite"
              id={PAYPAL_CONTAINER_ID}
              ref={paypalContainerRef}
            />
            {sdkLoading && (
              <p className="mt-3 text-center text-sm text-slate-500">
                Loading secure PayPal checkout…
              </p>
            )}
            {!validAmount && (
              <p className="mt-3 text-sm text-rose-700" role="alert">
                Enter a valid amount between $1 and $10,000 with up to two
                decimal places.
              </p>
            )}
            {receipt && (
              <div
                className="mt-4 rounded-xl bg-emerald-50 px-4 py-3 text-sm text-emerald-900"
                role="status"
              >
                <p className="font-semibold">
                  Thank you! Your ${Number(receipt.amount).toFixed(2)} donation
                  is recorded in the FormulaFlow ledger and credited to the
                  support pool.
                </p>
                <p className="mt-1 break-all text-xs">
                  PayPal capture ID: {receipt.captureId}
                </p>
              </div>
            )}
            {notice && (
              <p
                className="mt-3 rounded-xl bg-amber-50 px-3 py-2.5 text-sm text-amber-900"
                role="status"
              >
                {notice}
              </p>
            )}
            {error && (
              <p
                className="mt-3 rounded-xl bg-rose-50 px-3 py-2.5 text-sm text-rose-800"
                role="alert"
              >
                {error}
              </p>
            )}
          </aside>
        </div>
      </div>
      <p className="mx-auto mt-5 max-w-2xl text-center text-xs leading-5 text-slate-500">
        FormulaFlow is not a medical provider. Formula availability and
        assistance depend on local supply.
      </p>
    </section>
  );
}
