import { useEffect, useState } from "react";

const DATA_LIMIT = 5000;

const dataMappings = {
  donationId: {
    type: "field",
    supportedRoles: ["numeric"],
    required: true,
    aiDescription: "Unique donation record ID from the Donations source.",
  },
  donationAmount: {
    type: "field",
    supportedRoles: ["numeric"],
    requires: { per: "dataMapping.donationId", cardinality: "one" },
    required: true,
    aiDescription: "Donation amount in USD from the Donations source.",
  },
  payoutId: {
    type: "field",
    supportedRoles: ["numeric"],
    required: true,
    aiDescription: "Unique payout record ID from the Payouts source.",
  },
  payoutAmount: {
    type: "field",
    supportedRoles: ["numeric"],
    requires: { per: "dataMapping.payoutId", cardinality: "one" },
    required: true,
    aiDescription: "Payout amount in USD from the Payouts source.",
  },
  requestId: {
    type: "field",
    supportedRoles: ["numeric"],
    required: true,
    aiDescription: "Unique request ID from the Requests source.",
  },
  requestLifecycle: {
    type: "field",
    supportedRoles: ["category"],
    requires: { per: "dataMapping.requestId", cardinality: "one" },
    required: true,
    aiDescription:
      "Request lifecycle status from Requests, such as ready_for_pickup or collected.",
  },
  requestAmount: {
    type: "field",
    supportedRoles: ["numeric"],
    requires: { per: "dataMapping.requestId", cardinality: "one" },
    required: true,
    aiDescription: "Matched request cost in USD from the Requests source.",
  },
  payoutPendingAt: {
    type: "field",
    supportedRoles: ["temporal"],
    requires: { per: "dataMapping.requestId", cardinality: "one" },
    required: true,
    aiDescription: "Payout pending timestamp from the Requests source.",
  },
  payoutConfirmedAt: {
    type: "field",
    supportedRoles: ["temporal"],
    requires: { per: "dataMapping.requestId", cardinality: "one" },
    required: true,
    aiDescription: "Payout confirmed timestamp from the Requests source.",
  },
};

function createFormatShape({ api }) {
  return api.defineAiCommand((s) => ({
    input: s.object({
      title: s
        .object({
          enabled: s.boolean().optional(),
          text: s.string().optional(),
        })
        .optional(),
      subtitle: s
        .object({
          enabled: s.boolean().optional(),
          text: s.string().optional(),
        })
        .optional(),
      caption: s
        .object({
          enabled: s.boolean().optional(),
          text: s.string().optional(),
        })
        .optional(),
    }),
    execute: () => ({ success: true, value: null }),
  })).shape;
}

function getMappedField(dataMapping, key) {
  return dataMapping[key]?.[0];
}

async function loadRows(widgetApi, fields, queryId) {
  const { results } = await widgetApi.getData(
    {
      kind: "flat",
      fields,
      limit: { count: DATA_LIMIT },
    },
    { queryId },
  );
  return results.rows;
}

function fieldValue(row, field) {
  return row[field.key];
}

function toAmount(value) {
  const amount = Number(value);
  return Number.isFinite(amount) ? amount : 0;
}

function useStudioRows(widgetApi, dataMapping, queryId, fieldKeys) {
  const fields = fieldKeys.map((key) => getMappedField(dataMapping, key));
  const [state, setState] = useState({ status: "loading", rows: [], error: "" });

  useEffect(() => {
    let active = true;
    if (fields.some((field) => !field)) {
      widgetApi.setDisplayState("incompleteDataMapping");
      setState({ status: "incomplete", rows: [], error: "" });
      return () => {
        active = false;
      };
    }

    widgetApi.setDisplayState("loading", { prominent: false });
    setState({ status: "loading", rows: [], error: "" });
    loadRows(widgetApi, fields, queryId)
      .then((rows) => {
        if (active) {
          widgetApi.setDisplayState("displayed");
          setState({ status: "ready", rows, error: "" });
        }
      })
      .catch((error) => {
        if (active) {
          widgetApi.setDisplayState("displayed");
          setState({
            status: "error",
            rows: [],
            error:
              error instanceof Error ? error.message : "Could not load widget data.",
          });
        }
      });

    return () => {
      active = false;
    };
  }, [widgetApi, queryId, ...fields]);

  return { ...state, fields };
}

function formatCurrency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(value);
}

function WidgetHeading({ title, subtitle, fallbackTitle }) {
  return (
    <header className="formula-widget-heading">
      {title?.enabled !== false && (
        <h2>{title?.text || fallbackTitle}</h2>
      )}
      {subtitle?.enabled !== false && subtitle?.text && <p>{subtitle.text}</p>}
    </header>
  );
}

function WidgetMessage({ state }) {
  if (state.status === "error") {
    return (
      <p className="formula-widget-message" role="alert">
        Could not load this widget’s data: {state.error}
      </p>
    );
  }
  if (state.status === "loading") {
    return (
      <p className="formula-widget-message" role="status">
        Loading data…
      </p>
    );
  }
  if (state.status === "incomplete") {
    return (
      <p className="formula-widget-message">
        Select the required data fields in the widget setup.
      </p>
    );
  }
  return null;
}

function sumField(rows, field) {
  return rows.reduce((total, row) => total + toAmount(fieldValue(row, field)), 0);
}

function MetricTile({ label, value, detail }) {
  return (
    <article className="formula-widget-tile">
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </article>
  );
}

export function AidPipelineFunnel({ dataMapping, format = {}, widgetApi }) {
  const donations = useStudioRows(
    widgetApi,
    dataMapping,
    "aid-pipeline-donations",
    ["donationId", "donationAmount"],
  );
  const payouts = useStudioRows(
    widgetApi,
    dataMapping,
    "aid-pipeline-payouts",
    ["payoutId", "payoutAmount"],
  );
  const requests = useStudioRows(
    widgetApi,
    dataMapping,
    "aid-pipeline-requests",
    ["requestId", "requestLifecycle", "requestAmount"],
  );
  const states = [donations, payouts, requests];
  const stageRows = [
    {
      label: "Donations",
      count: donations.rows.length,
      amount: sumField(donations.rows, donations.fields[1]),
    },
    {
      label: "Payouts",
      count: payouts.rows.length,
      amount: sumField(payouts.rows, payouts.fields[1]),
    },
    ...["ready_for_pickup", "collected"].map((stage) => {
      const rows = requests.rows.filter(
        (row) => fieldValue(row, requests.fields[1]) === stage,
      );
      return {
        label: stage === "ready_for_pickup" ? "Ready for pickup" : "Collected",
        count: rows.length,
        amount: sumField(rows, requests.fields[2]),
      };
    }),
  ];
  const maxAmount = Math.max(0, ...stageRows.map(({ amount }) => amount));
  const hasError = states.find((state) => state.status === "error");
  const isLoading = states.some((state) => state.status === "loading");
  const isIncomplete = states.some((state) => state.status === "incomplete");
  const messageState =
    hasError || (isLoading && { status: "loading" }) ||
    (isIncomplete && { status: "incomplete" });

  return (
    <section className="formula-custom-widget">
      <WidgetHeading
        fallbackTitle="Aid Pipeline Funnel"
        subtitle={format.subtitle}
        title={format.title}
      />
      {messageState ? (
        <WidgetMessage state={messageState} />
      ) : (
        <div className="formula-funnel-stages">
          {stageRows.map((stage) => (
            <article className="formula-funnel-stage" key={stage.label}>
              <div className="formula-funnel-stage-heading">
                <strong>{stage.label}</strong>
                <span>{stage.count.toLocaleString("en-US")}</span>
              </div>
              <div
                className="formula-funnel-track"
                role="meter"
                aria-label={`${stage.label} amount compared to the largest stage amount`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={
                  maxAmount === 0 ? 0 : Math.round((stage.amount / maxAmount) * 100)
                }
              >
                <span
                  style={{
                    width: `${maxAmount === 0 ? 0 : (stage.amount / maxAmount) * 100}%`,
                  }}
                />
              </div>
              <small>{formatCurrency(stage.amount)}</small>
            </article>
          ))}
        </div>
      )}
      {format.caption?.enabled !== false && format.caption?.text && (
        <p className="formula-widget-caption">{format.caption.text}</p>
      )}
    </section>
  );
}

function median(values) {
  if (values.length === 0) {
    return null;
  }
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 === 0
    ? (ordered[middle - 1] + ordered[middle]) / 2
    : ordered[middle];
}

function formatDuration(milliseconds) {
  if (milliseconds === null) {
    return "—";
  }
  const hours = milliseconds / (1000 * 60 * 60);
  if (hours < 48) {
    return `${hours.toFixed(1)} hours`;
  }
  return `${(hours / 24).toFixed(1)} days`;
}

export function ImpactScoreboard({ dataMapping, format = {}, widgetApi }) {
  const donations = useStudioRows(
    widgetApi,
    dataMapping,
    "impact-scoreboard-donations",
    ["donationId", "donationAmount"],
  );
  const requests = useStudioRows(
    widgetApi,
    dataMapping,
    "impact-scoreboard-requests",
    ["requestId", "requestLifecycle", "payoutPendingAt", "payoutConfirmedAt"],
  );
  const states = [donations, requests];
  const hasError = states.find((state) => state.status === "error");
  const isLoading = states.some((state) => state.status === "loading");
  const isIncomplete = states.some((state) => state.status === "incomplete");
  const messageState =
    hasError || (isLoading && { status: "loading" }) ||
    (isIncomplete && { status: "incomplete" });

  const collected = requests.rows.filter(
    (row) => fieldValue(row, requests.fields[1]) === "collected",
  ).length;
  const payoutDurations = requests.rows
    .map((row) => {
      const pendingAt = Date.parse(fieldValue(row, requests.fields[2]));
      const confirmedAt = Date.parse(fieldValue(row, requests.fields[3]));
      return Number.isFinite(pendingAt) &&
        Number.isFinite(confirmedAt) &&
        confirmedAt >= pendingAt
        ? confirmedAt - pendingAt
        : null;
    })
    .filter((duration) => duration !== null);
  const medianPayoutTime = median(payoutDurations);

  return (
    <section className="formula-custom-widget">
      <WidgetHeading
        fallbackTitle="Impact Scoreboard"
        subtitle={format.subtitle}
        title={format.title}
      />
      {messageState ? (
        <WidgetMessage state={messageState} />
      ) : (
        <div className="formula-scoreboard">
          <MetricTile label="Families collected" value={collected.toLocaleString("en-US")} detail="Requests marked collected" />
          <MetricTile
            label="Total pledged"
            value={formatCurrency(sumField(donations.rows, donations.fields[1]))}
            detail="Captured donation records"
          />
          <MetricTile
            label="Median payout time"
            value={formatDuration(medianPayoutTime)}
            detail={
              payoutDurations.length === 0
                ? "No confirmed payout timing data"
                : `${payoutDurations.length.toLocaleString("en-US")} confirmed payouts`
            }
          />
        </div>
      )}
      {format.caption?.enabled !== false && format.caption?.text && (
        <p className="formula-widget-caption">{format.caption.text}</p>
      )}
    </section>
  );
}

const aidPipelineMapping = {
  donationId: dataMappings.donationId,
  donationAmount: dataMappings.donationAmount,
  payoutId: dataMappings.payoutId,
  payoutAmount: dataMappings.payoutAmount,
  requestId: dataMappings.requestId,
  requestLifecycle: dataMappings.requestLifecycle,
  requestAmount: dataMappings.requestAmount,
};

const impactScoreboardMapping = {
  donationId: dataMappings.donationId,
  donationAmount: dataMappings.donationAmount,
  requestId: dataMappings.requestId,
  requestLifecycle: dataMappings.requestLifecycle,
  payoutPendingAt: dataMappings.payoutPendingAt,
  payoutConfirmedAt: dataMappings.payoutConfirmedAt,
};

export const formulaFlowWidgetDefinitions = [
  {
    id: "aid-pipeline-funnel",
    label: "Aid Pipeline Funnel",
    dataMapping: aidPipelineMapping,
    formatShape: createFormatShape,
    form: (params) =>
      params.createDefaults({
        dataMappingItems: [
          { key: "donationId", label: "Donation ID" },
          { key: "donationAmount", label: "Donation amount (USD)" },
          { key: "payoutId", label: "Payout ID" },
          { key: "payoutAmount", label: "Payout amount (USD)" },
          { key: "requestId", label: "Request ID" },
          { key: "requestLifecycle", label: "Request lifecycle status" },
          { key: "requestAmount", label: "Request cost (USD)" },
        ],
      }),
    comp: AidPipelineFunnel,
    defaultSize: { width: 520, height: 360 },
    minSize: { width: 320, height: 260 },
    ai: {
      label: "Aid Pipeline Funnel",
      description:
        "Four-stage operational funnel with record counts and USD totals for donations, payouts, requests ready for pickup, and collected requests.",
      usage:
        "Use for an at-a-glance view of the aid pipeline. Amounts come from donation records, payout records, and request costs respectively; ready/collected stages are filtered from request lifecycle status.",
      configuration:
        "Map Donation ID and amount from Donations, Payout ID and amount from Payouts, and Request ID, lifecycle status, and request cost from Requests. Keep each ID and its related fields on the same source.",
    },
  },
  {
    id: "impact-scoreboard",
    label: "Impact Scoreboard",
    dataMapping: impactScoreboardMapping,
    formatShape: createFormatShape,
    form: (params) =>
      params.createDefaults({
        dataMappingItems: [
          { key: "donationId", label: "Donation ID" },
          { key: "donationAmount", label: "Donation amount (USD)" },
          { key: "requestId", label: "Request ID" },
          { key: "requestLifecycle", label: "Request lifecycle status" },
          { key: "payoutPendingAt", label: "Payout pending timestamp" },
          { key: "payoutConfirmedAt", label: "Payout confirmed timestamp" },
        ],
      }),
    comp: ImpactScoreboard,
    defaultSize: { width: 520, height: 300 },
    minSize: { width: 320, height: 220 },
    ai: {
      label: "Impact Scoreboard",
      description:
        "Three large KPI tiles for families whose formula was collected, total captured donation amount in USD, and median elapsed time from payout pending to payout confirmation.",
      usage:
        "Families collected counts Requests with lifecycle status collected. Total captured sums donation records. Median payout time uses requests with both payout pending and payout confirmed timestamps.",
      configuration:
        "Map Donation ID and amount from Donations. Map Request ID, lifecycle status, payout pending timestamp, and payout confirmed timestamp from Requests.",
    },
  },
];
