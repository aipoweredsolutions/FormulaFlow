import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

const demoMetrics = {
  total_donations_usd: 24850,
  families_supported_count: 412,
  infants_sustained: 412,
  verified_merchants: 18,
};

function BrandMark() {
  return (
    <span className="grid size-10 place-items-center rounded-2xl bg-emerald-700 text-white shadow-lg shadow-emerald-900/15">
      <svg
        aria-hidden="true"
        className="size-6"
        fill="none"
        viewBox="0 0 24 24"
      >
        <path
          d="M12 21s-7-4.35-7-10.1A4.1 4.1 0 0 1 12 8.5a4.1 4.1 0 0 1 7 2.4C19 16.65 12 21 12 21Z"
          fill="currentColor"
        />
        <path
          d="M12 7V3m-2 2 2-2 2 2"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.7"
        />
      </svg>
    </span>
  );
}

function ArrowIcon({ className = "size-4" }) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      viewBox="0 0 20 20"
    >
      <path
        d="M4.25 10h11.5m-4.5-4.5 4.5 4.5-4.5 4.5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.7"
      />
    </svg>
  );
}

function CheckIcon({ negative = false }) {
  return (
    <span
      className={`grid size-7 shrink-0 place-items-center rounded-full ${
        negative
          ? "bg-rose-100 text-rose-600"
          : "bg-emerald-100 text-emerald-700"
      }`}
    >
      {negative ? (
        <svg aria-hidden="true" className="size-4" fill="none" viewBox="0 0 20 20">
          <path
            d="m6 6 8 8m0-8-8 8"
            stroke="currentColor"
            strokeLinecap="round"
            strokeWidth="1.8"
          />
        </svg>
      ) : (
        <svg aria-hidden="true" className="size-4" fill="none" viewBox="0 0 20 20">
          <path
            d="m4.5 10 3.7 3.7 7.3-7.4"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.8"
          />
        </svg>
      )}
    </span>
  );
}

function MetricIcon({ children }) {
  return (
    <span className="grid size-11 place-items-center rounded-2xl bg-emerald-100 text-emerald-800">
      {children}
    </span>
  );
}

export default function LandingPage() {
  const [metrics, setMetrics] = useState(null);
  const [isDemo, setIsDemo] = useState(false);

  useEffect(() => {
    let active = true;
    let controller;
    let hasLiveMetrics = false;
    const loadImpact = async () => {
      controller?.abort();
      const requestController = new AbortController();
      controller = requestController;
      try {
        const response = await fetch(`${API_BASE_URL}/api/public/impact`, {
          signal: requestController.signal,
        });
        if (!response.ok) {
          throw new Error("Impact data is unavailable");
        }
        const data = await response.json();
        if (active) {
          hasLiveMetrics = true;
          setMetrics(data);
          setIsDemo(false);
        }
      } catch {
        if (active && !requestController.signal.aborted && !hasLiveMetrics) {
          setMetrics(demoMetrics);
          setIsDemo(true);
        }
      }
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void loadImpact();
      }
    };

    void loadImpact();
    const intervalId = window.setInterval(refreshWhenVisible, 30000);
    window.addEventListener("focus", refreshWhenVisible);
    return () => {
      active = false;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshWhenVisible);
      controller?.abort();
    };
  }, []);

  const metricItems = [
    {
      label: "Total pooled donations captured",
      value: metrics
        ? `$${Number(metrics.total_donations_usd).toLocaleString("en-US")} USD`
        : "Loading",
      icon: (
        <svg aria-hidden="true" className="size-5" fill="none" viewBox="0 0 24 24">
          <path
            d="M12 3.75v16.5m5-12.75c0-1.65-2.24-3-5-3s-5 1.35-5 3 2.24 3 5 3 5 1.35 5 3-2.24 3-5 3-5-1.35-5-3"
            stroke="currentColor"
            strokeLinecap="round"
            strokeWidth="1.7"
          />
        </svg>
      ),
    },
    {
      label: "Families funded · confirmed PayPal payouts",
      value: metrics
        ? `${Number(
            metrics.families_supported_count ?? metrics.infants_sustained,
          ).toLocaleString("en-US")} families`
        : "Loading",
      icon: (
        <svg aria-hidden="true" className="size-5" fill="none" viewBox="0 0 24 24">
          <path
            d="M12 20s-7-4.35-7-10.1A4.1 4.1 0 0 1 12 7.5a4.1 4.1 0 0 1 7 2.4C19 15.65 12 20 12 20Z"
            stroke="currentColor"
            strokeLinejoin="round"
            strokeWidth="1.7"
          />
        </svg>
      ),
    },
    {
      label: "Verified merchant partners in Kenya",
      value: metrics
        ? `${Number(metrics.verified_merchants).toLocaleString("en-US")} retailers`
        : "Loading",
      icon: (
        <svg aria-hidden="true" className="size-5" fill="none" viewBox="0 0 24 24">
          <path
            d="M4 10V20h16V10M3 9l2-5h14l2 5M3 9c0 1.1.9 2 2 2s2-.9 2-2c0 1.1.9 2 2 2s2-.9 2-2c0 1.1.9 2 2 2s2-.9 2-2c0 1.1.9 2 2 2s2-.9 2-2M9 20v-5h6v5"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.5"
          />
        </svg>
      ),
    },
  ];

  return (
    <div className="min-h-screen overflow-hidden bg-slate-50 text-slate-900">
      <header className="relative z-10 border-b border-slate-200/80 bg-slate-50/90 backdrop-blur">
        <nav
          aria-label="Main navigation"
          className="mx-auto flex max-w-7xl items-center justify-between px-5 py-4 sm:px-8 lg:px-10"
        >
          <Link
            className="flex items-center gap-3 rounded-xl focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-emerald-700"
            to="/"
          >
            <BrandMark />
            <span className="text-lg font-extrabold tracking-tight text-slate-900">
              FormulaFlow <span className="text-emerald-700">AI</span>
            </span>
          </Link>
          <div className="hidden items-center gap-8 md:flex">
            <a
              className="text-sm font-semibold text-slate-600 transition hover:text-emerald-800"
              href="#how-it-works"
            >
              How It Works
            </a>
            <a
              className="text-sm font-semibold text-slate-600 transition hover:text-emerald-800"
              href="#sponsor-pool"
            >
              Sponsor Pool
            </a>
            <Link
              className="text-sm font-semibold text-slate-600 transition hover:text-emerald-800"
              to="/admin"
            >
              Internal Operations
            </Link>
          </div>
          <Link
            className="inline-flex min-h-11 items-center justify-center rounded-full bg-slate-900 px-5 text-sm font-bold text-white transition hover:bg-slate-700 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-slate-900"
            to="/donate"
          >
            Sponsor a family
          </Link>
        </nav>
      </header>

      <main>
        <section className="relative isolate">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -right-36 -top-32 -z-10 size-[34rem] rounded-full bg-emerald-200/35 blur-3xl"
          />
          <div className="mx-auto grid max-w-7xl items-center gap-12 px-5 pb-20 pt-16 sm:px-8 sm:pb-24 sm:pt-20 lg:grid-cols-[1.08fr_0.92fr] lg:px-10 lg:pb-28 lg:pt-24">
            <div className="max-w-3xl">
              <div className="mb-7 inline-flex items-center gap-2 rounded-full border border-emerald-200 bg-white/80 px-4 py-2 text-xs font-bold uppercase tracking-[0.13em] text-emerald-800 shadow-sm">
                <span className="relative flex size-2">
                  <span className="absolute inline-flex size-full animate-ping rounded-full bg-emerald-500 opacity-40" />
                  <span className="relative inline-flex size-2 rounded-full bg-emerald-600" />
                </span>
                Local care. Global generosity.
              </div>
              <h1 className="max-w-4xl text-5xl font-black leading-[1.04] tracking-[-0.055em] text-slate-900 sm:text-6xl lg:text-[4.35rem]">
                Nourishing Kenya’s Tomorrow,{" "}
                <span className="text-emerald-700">
                  One Hyper-Local Mile at a Time.
                </span>
              </h1>
              <p className="mt-7 max-w-2xl text-base leading-8 text-slate-600 sm:text-lg">
                Traditional international relief takes weeks, incurs customs
                duties, and risks spoilage. FormulaFlow AI eliminates physical
                transit entirely. Our AI matching engine routes global
                donations directly to verified neighborhood stores down the
                street from the families who need it most, sustaining infants
                up to two years old.
              </p>
              <div className="mt-9 flex flex-col gap-3 sm:flex-row">
                <Link
                  className="group inline-flex min-h-14 items-center justify-center gap-2 rounded-2xl bg-emerald-700 px-6 text-center text-sm font-extrabold text-white shadow-xl shadow-emerald-900/15 transition hover:-translate-y-0.5 hover:bg-emerald-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-700"
                  to="/donate"
                >
                  Fund the Relief Pool
                  <span className="rounded-full bg-white/20 px-2 py-1 text-[10px] font-bold uppercase tracking-wide">
                    Global donors
                  </span>
                  <ArrowIcon className="ml-1 size-4 transition group-hover:translate-x-1" />
                </Link>
                <Link
                  className="inline-flex min-h-14 items-center justify-center gap-2 rounded-2xl border border-slate-300 bg-white px-6 text-sm font-extrabold text-slate-800 shadow-sm transition hover:border-emerald-500 hover:bg-emerald-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-700"
                  to="/request"
                >
                  Request Formula Assistance
                  <span className="text-[10px] font-bold uppercase tracking-wide text-slate-500">
                    Kenya
                  </span>
                </Link>
              </div>
              <p className="mt-4 text-sm font-semibold text-slate-600">
                Already submitted a request?{" "}
                <Link
                  className="text-emerald-800 underline underline-offset-4"
                  to="/status"
                >
                  Check its status
                </Link>
              </p>
              <div className="mt-7 flex flex-wrap items-center gap-x-6 gap-y-3 text-xs font-semibold text-slate-500">
                <span className="inline-flex items-center gap-2">
                  <CheckIcon />
                  Direct-to-merchant PayPal payouts
                </span>
                <span className="inline-flex items-center gap-2">
                  <CheckIcon />
                  Designed for birth to 24 months
                </span>
              </div>
            </div>

            <div className="relative mx-auto w-full max-w-[34rem]">
              <div className="absolute -left-5 top-14 size-24 rounded-full bg-emerald-200/70 blur-2xl sm:-left-10" />
              <div className="absolute -bottom-5 right-4 size-32 rounded-full bg-emerald-200/70 blur-2xl" />
              <div className="relative overflow-hidden rounded-[2rem] border border-white/80 bg-white p-3 shadow-[0_35px_100px_-40px_rgba(15,23,42,0.38)]">
                <div className="relative min-h-[430px] overflow-hidden rounded-[1.5rem] bg-gradient-to-br from-emerald-100 via-slate-50 to-emerald-100 p-6 sm:min-h-[490px] sm:p-8">
                  <div
                    aria-hidden="true"
                    className="absolute inset-0 opacity-30"
                    style={{
                      backgroundImage:
                        "radial-gradient(#6c9e84 0.7px, transparent 0.7px)",
                      backgroundSize: "18px 18px",
                    }}
                  />
                  <div className="relative flex items-center justify-between">
                    <span className="rounded-full bg-white/75 px-3 py-1.5 text-[10px] font-extrabold uppercase tracking-[0.16em] text-emerald-800">
                      A shorter path to care
                    </span>
                    <span className="grid size-10 place-items-center rounded-full bg-white/80 text-emerald-800 shadow-sm">
                      <svg aria-hidden="true" className="size-5" fill="none" viewBox="0 0 24 24">
                        <path
                          d="M12 21s-7-4.35-7-10.1A4.1 4.1 0 0 1 12 8.5a4.1 4.1 0 0 1 7 2.4C19 16.65 12 21 12 21Z"
                          fill="currentColor"
                        />
                      </svg>
                    </span>
                  </div>

                  <div className="relative mt-8 flex min-h-72 items-center justify-center">
                    <svg
                      aria-label="Illustration of a local store connected to a family"
                      className="h-auto w-full max-w-[430px]"
                      fill="none"
                      role="img"
                      viewBox="0 0 440 300"
                    >
                      <path
                        d="M61 242c54-49 90-64 143-49 43 12 81 8 127-31 16-14 33-26 52-34"
                        stroke="#B7C7A7"
                        strokeDasharray="7 9"
                        strokeLinecap="round"
                        strokeWidth="5"
                      />
                      <circle cx="64" cy="241" r="13" fill="#fff" stroke="#047857" strokeWidth="4" />
                      <circle cx="382" cy="128" r="13" fill="#fff" stroke="#047857" strokeWidth="4" />
                      <rect x="218" y="106" width="157" height="111" rx="14" fill="#fff" />
                      <path d="M204 111 234 68h124l30 43H204Z" fill="#047857" />
                      <path d="M221 108h150" stroke="#A7F3D0" strokeWidth="8" />
                      <rect x="236" y="128" width="44" height="89" rx="5" fill="#D1FAE5" />
                      <path d="M258 128v89" stroke="#fff" strokeWidth="4" />
                      <rect x="294" y="133" width="62" height="33" rx="5" fill="#D1FAE5" />
                      <path d="M303 151h44m-35-9h26" stroke="#065F46" strokeLinecap="round" strokeWidth="3" />
                      <rect x="300" y="179" width="48" height="38" rx="5" fill="#E2E8F0" />
                      <path d="M305 186h38m-38 8h38m-38 8h38" stroke="#94A3B8" strokeWidth="3" />
                      <path d="M70 246c1-30 20-47 44-47s43 17 44 47" fill="#0F766E" />
                      <circle cx="114" cy="178" r="25" fill="#475569" />
                      <path d="M89 178c1-24 13-38 30-38 19 0 31 16 28 40-7-8-12-17-15-26-8 11-22 19-43 24Z" fill="#1E293B" />
                      <path d="M102 199c9 12 17 14 26 0" stroke="#475569" strokeLinecap="round" strokeWidth="8" />
                      <path d="M83 226c12 7 29 10 48 7" stroke="#CCFBF1" strokeLinecap="round" strokeWidth="4" />
                      <rect x="21" y="38" width="128" height="57" rx="15" fill="#fff" />
                      <path d="M45 61h75m-75 13h49" stroke="#CBD5E1" strokeLinecap="round" strokeWidth="6" />
                      <circle cx="382" cy="127" r="32" fill="#D1FAE5" />
                      <path d="M371 128h22m-11-11v22" stroke="#047857" strokeLinecap="round" strokeWidth="4" />
                    </svg>
                  </div>

                  <div className="relative grid grid-cols-2 gap-3">
                    <div className="rounded-2xl border border-white/70 bg-white/80 p-4 shadow-sm backdrop-blur">
                      <div className="text-[10px] font-bold uppercase tracking-[0.14em] text-slate-500">
                        From donor to store
                      </div>
                      <div className="mt-1 text-2xl font-black tracking-tight text-slate-900">
                        One local mile
                      </div>
                    </div>
                    <div className="rounded-2xl border border-white/70 bg-emerald-800 p-4 text-white shadow-sm">
                      <div className="text-[10px] font-bold uppercase tracking-[0.14em] text-emerald-100">
                        Typical connection
                      </div>
                      <div className="mt-1 text-2xl font-black tracking-tight">
                        ~30 min
                      </div>
                    </div>
                  </div>
                </div>
              </div>
              <div className="absolute -right-3 top-32 hidden rounded-2xl border border-slate-100 bg-white px-4 py-3 shadow-xl sm:block lg:-right-8">
                <div className="flex items-center gap-3">
                  <span className="grid size-9 place-items-center rounded-xl bg-emerald-100 text-emerald-700">
                    <CheckIcon />
                  </span>
                  <div>
                    <div className="text-xs font-extrabold text-slate-900">
                      Local match found
                    </div>
                    <div className="mt-0.5 text-[10px] text-slate-500">
                      Neighborhood retailer
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </section>

        <section
          aria-labelledby="how-it-works-title"
          className="bg-white py-20 sm:py-24"
          id="how-it-works"
        >
          <div className="mx-auto max-w-7xl px-5 sm:px-8 lg:px-10">
            <div className="mx-auto max-w-2xl text-center">
              <span className="text-xs font-extrabold uppercase tracking-[0.17em] text-emerald-800">
                A better way to deliver relief
              </span>
              <h2
                className="mt-3 text-3xl font-black tracking-[-0.04em] text-slate-900 sm:text-4xl"
                id="how-it-works-title"
              >
                Replace a long supply chain with one local connection.
              </h2>
              <p className="mt-4 leading-7 text-slate-600">
                Less time in transit means more support reaching families and
                more of every contribution staying in the community.
              </p>
            </div>

            <div className="mt-12 grid gap-5 lg:grid-cols-2">
              <article className="rounded-[1.75rem] border border-rose-100 bg-rose-50/70 p-6 sm:p-8">
                <div className="flex items-center gap-3">
                  <span className="grid size-10 place-items-center rounded-xl bg-rose-100 text-rose-700">
                    <svg aria-hidden="true" className="size-5" fill="none" viewBox="0 0 24 24">
                      <path
                        d="M3 8h12v10H3zM15 11h3l3 3v4h-6M7 18a2 2 0 1 0 4 0m6 0a2 2 0 1 0 4 0"
                        stroke="currentColor"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth="1.7"
                      />
                    </svg>
                  </span>
                  <div>
                    <span className="text-[10px] font-extrabold uppercase tracking-[0.16em] text-rose-700">
                      The old way
                    </span>
                    <h3 className="text-xl font-black text-slate-900">
                      International shipping
                    </h3>
                  </div>
                </div>
                <div className="mt-7 space-y-5">
                  {[
                    ["30-day transit window", "Delays at ports and customs."],
                    ["High spoilage risk", "Cans can be damaged in transit."],
                    ["High overhead costs", "Up to 40% spent on freight and duties."],
                  ].map(([title, detail]) => (
                    <div className="flex gap-3" key={title}>
                      <CheckIcon negative />
                      <div>
                        <div className="font-bold text-slate-800">{title}</div>
                        <p className="mt-1 text-sm leading-6 text-slate-600">
                          {detail}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              </article>

              <article className="relative overflow-hidden rounded-[1.75rem] border border-emerald-100 bg-emerald-50/70 p-6 sm:p-8">
                <div
                  aria-hidden="true"
                  className="absolute -right-20 -top-24 size-64 rounded-full bg-emerald-200/40 blur-3xl"
                />
                <div className="relative flex items-center gap-3">
                  <span className="grid size-10 place-items-center rounded-xl bg-emerald-700 text-white">
                    <svg aria-hidden="true" className="size-5" fill="none" viewBox="0 0 24 24">
                      <path
                        d="M12 3v3m0 12v3m9-9h-3M6 12H3m15.36-6.36-2.12 2.12M7.76 16.24l-2.12 2.12m12.72 0-2.12-2.12M7.76 7.76 5.64 5.64M15 9l-6 6m.5-5.5h.01M14.5 14.5h.01"
                        stroke="currentColor"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth="1.7"
                      />
                    </svg>
                  </span>
                  <div>
                    <span className="text-[10px] font-extrabold uppercase tracking-[0.16em] text-emerald-800">
                      FormulaFlow way
                    </span>
                    <h3 className="text-xl font-black text-slate-900">
                      Agentic local commerce
                    </h3>
                  </div>
                </div>
                <div className="relative mt-7 space-y-5">
                  {[
                    ["30-minute delivery", "AI matches requests with nearby store shelves."],
                    ["Zero logistical waste", "Local retailers act as decentralized micro-warehouses."],
                    [
                      "100% capital efficiency*",
                      "Allocated aid goes straight to a local retail business.",
                    ],
                  ].map(([title, detail]) => (
                    <div className="flex gap-3" key={title}>
                      <CheckIcon />
                      <div>
                        <div className="font-bold text-slate-800">{title}</div>
                        <p className="mt-1 text-sm leading-6 text-slate-600">
                          {detail}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              </article>
            </div>
            <p className="mx-auto mt-5 max-w-3xl text-center text-xs leading-5 text-slate-500">
              *Capital-efficiency figure describes the intended use of aid
              allocations; PayPal processing fees may apply. Delivery times,
              spoilage avoidance, and the 40% overhead comparison are project
              targets and illustrative estimates, not guarantees or independently
              audited outcomes.
            </p>
          </div>
        </section>

        <section className="bg-slate-50 py-20 sm:py-24">
          <div className="mx-auto max-w-7xl px-5 sm:px-8 lg:px-10">
            <div className="mx-auto max-w-2xl text-center">
              <span className="text-xs font-extrabold uppercase tracking-[0.17em] text-emerald-800">
                Thoughtful support at every stage
              </span>
              <h2 className="mt-3 text-3xl font-black tracking-[-0.04em] text-slate-900 sm:text-4xl">
                Growing needs. Steady local care.
              </h2>
              <p className="mt-4 leading-7 text-slate-600">
                Monthly container amounts follow FormulaFlow’s current support
                model. Formula choices and feeding needs should be confirmed
                with a qualified health professional.
              </p>
            </div>

            <div className="mt-12 grid gap-5 md:grid-cols-3">
              {[
                {
                  phase: "The Newborn Phase",
                  range: "0–2 months",
                  amount: "3",
                  description:
                    "A baseline monthly supply tailored for early growth and development.",
                  accent: "bg-emerald-600",
                  highlight: false,
                },
                {
                  phase: "The Growth Phase",
                  range: "3–5 months",
                  amount: "4",
                  description:
                    "An increased monthly supply that tracks early infant growth spurts.",
                  accent: "bg-emerald-800",
                  highlight: true,
                },
                {
                  phase: "The Toddler Transition",
                  range: "6–24 months",
                  amount: "4",
                  description:
                    "A sustained monthly supply as solid foods are safely introduced.",
                  accent: "bg-emerald-700",
                  highlight: false,
                },
              ].map((item) => (
                <article
                  className={`relative overflow-hidden rounded-[1.75rem] border bg-white p-6 shadow-sm transition hover:-translate-y-1 hover:shadow-xl sm:p-7 ${
                    item.highlight
                      ? "border-emerald-300 ring-2 ring-emerald-100"
                      : "border-slate-200"
                  }`}
                  key={item.phase}
                >
                  <div className={`absolute inset-x-0 top-0 h-1.5 ${item.accent}`} />
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <span className="inline-flex rounded-full bg-slate-100 px-3 py-1 text-xs font-bold text-slate-600">
                        {item.range}
                      </span>
                      <h3 className="mt-5 text-xl font-black tracking-tight text-slate-900">
                        {item.phase}
                      </h3>
                    </div>
                    <MetricIcon>
                      <svg aria-hidden="true" className="size-5" fill="none" viewBox="0 0 24 24">
                        <path
                          d="M7 4h10l1 3H6l1-3Zm-1 3h12v13H6V7Zm3 4h6m-6 3h6"
                          stroke="currentColor"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth="1.6"
                        />
                      </svg>
                    </MetricIcon>
                  </div>
                  <div className="mt-8 flex items-baseline gap-2">
                    <span className="text-5xl font-black tracking-[-0.06em] text-slate-900">
                      {item.amount}
                    </span>
                    <span className="text-sm font-bold text-slate-500">
                      containers / month
                    </span>
                  </div>
                  <p className="mt-4 min-h-14 text-sm leading-6 text-slate-600">
                    {item.description}
                  </p>
                  <div className="mt-6 border-t border-slate-100 pt-4 text-xs font-semibold text-slate-500">
                    One month of locally sourced support
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section
          aria-labelledby="sponsor-pool-title"
          className="relative overflow-hidden bg-slate-900 py-16 text-white sm:py-20"
          id="sponsor-pool"
        >
          <div
            aria-hidden="true"
            className="absolute -left-20 top-0 size-80 rounded-full bg-emerald-500/15 blur-3xl"
          />
          <div className="relative mx-auto max-w-7xl px-5 sm:px-8 lg:px-10">
            <div className="flex flex-col justify-between gap-8 lg:flex-row lg:items-end">
              <div className="max-w-xl">
                <span className="text-xs font-extrabold uppercase tracking-[0.17em] text-emerald-300">
                  Transparent. Local. Accountable.
                </span>
                <h2
                  className="mt-3 text-3xl font-black tracking-[-0.04em] sm:text-4xl"
                  id="sponsor-pool-title"
                >
                  Every contribution moves through a traceable local pool.
                </h2>
                <p className="mt-4 leading-7 text-slate-300">
                  Donations are captured securely. Approved assistance is paid
                  directly to a matched merchant, with request and payout
                  activity recorded for operations.
                </p>
              </div>
              <Link
                className="inline-flex min-h-12 shrink-0 items-center justify-center gap-2 rounded-xl bg-emerald-300 px-5 text-sm font-extrabold text-emerald-950 transition hover:bg-emerald-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-200"
                to="/donate"
              >
                Fund the Relief Pool
                <ArrowIcon />
              </Link>
            </div>

            <div className="mt-11 grid divide-y divide-white/10 rounded-3xl border border-white/10 bg-white/[0.04] sm:grid-cols-3 sm:divide-x sm:divide-y-0">
              {metricItems.map((item) => (
                <article className="flex gap-4 p-5 sm:p-6" key={item.label}>
                  <MetricIcon>{item.icon}</MetricIcon>
                  <div className="min-w-0">
                    <div className="text-2xl font-black tracking-tight text-white sm:text-3xl">
                      {item.value}
                    </div>
                    <div className="mt-1 text-xs font-semibold leading-5 text-slate-300">
                      {item.label}
                    </div>
                  </div>
                </article>
              ))}
            </div>
            <p
              aria-live="polite"
              className="mt-3 text-right text-[11px] text-slate-400"
            >
              {metrics ? (isDemo ? "Illustrative demo snapshot" : "Live platform data") : "Loading platform data…"}
            </p>
          </div>
        </section>

        <section className="bg-emerald-50 py-16 sm:py-20">
          <div className="mx-auto flex max-w-7xl flex-col items-start justify-between gap-6 px-5 sm:px-8 md:flex-row md:items-center lg:px-10">
            <div className="max-w-2xl">
              <span className="text-xs font-extrabold uppercase tracking-[0.16em] text-emerald-800">
                Shorten the distance to care
              </span>
              <h2 className="mt-2 text-3xl font-black tracking-[-0.04em] text-slate-900">
                A nearby shelf can make all the difference.
              </h2>
            </div>
            <Link
              className="inline-flex min-h-[3.25rem] items-center justify-center gap-2 rounded-xl bg-emerald-700 px-6 py-3 text-sm font-extrabold text-white shadow-lg shadow-emerald-900/15 transition hover:bg-emerald-800 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-700"
              to="/request"
            >
              Request Formula Assistance
              <ArrowIcon />
            </Link>
          </div>
        </section>
      </main>

      <footer className="bg-white">
        <div className="mx-auto flex max-w-7xl flex-col gap-8 px-5 py-10 sm:px-8 lg:px-10">
          <div className="flex flex-col justify-between gap-7 md:flex-row">
            <div className="max-w-sm">
              <Link className="inline-flex items-center gap-3" to="/">
                <BrandMark />
                <span className="text-base font-extrabold tracking-tight text-slate-900">
                  FormulaFlow <span className="text-emerald-700">AI</span>
                </span>
              </Link>
              <p className="mt-4 text-sm leading-6 text-slate-500">
                Connecting global generosity to neighborhood care for families
                across Kenya.
              </p>
            </div>
            <div className="grid grid-cols-2 gap-x-12 gap-y-3 text-sm font-semibold text-slate-600">
              <a className="hover:text-emerald-800" href="#how-it-works">
                How it works
              </a>
              <a className="hover:text-emerald-800" href="#sponsor-pool">
                Sponsor pool
              </a>
              <Link className="hover:text-emerald-800" to="/request">
                Request assistance
              </Link>
              <Link className="hover:text-emerald-800" to="/admin">
                Internal operations
              </Link>
            </div>
          </div>
          <div className="flex flex-col justify-between gap-4 border-t border-slate-200 pt-6 text-xs text-slate-500 sm:flex-row sm:items-center">
            <p>
              © {new Date().getFullYear()} FormulaFlow AI. Built for the PayPal
              and AI Online Hackathon.
            </p>
            <p className="max-w-xl leading-5 sm:text-right">
              Hackathon tracks: Agentic Commerce · Most Impactful · AG Grid /
              AG Studio · Render
            </p>
          </div>
        </div>
      </footer>
    </div>
  );
}
