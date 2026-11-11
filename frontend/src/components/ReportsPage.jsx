import { useEffect, useMemo, useRef, useState } from "react";
import { AgCharts, AllCommunityModule, ModuleRegistry } from "ag-charts-community";
import { Link, NavLink, Navigate, useParams } from "react-router-dom";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const GREEN_PALETTE = ["#194533", "#245c49", "#2f7652", "#3e8b6a", "#70a98a"];
const REPORTS = [
  { id: "overview", label: "Overview" },
  { id: "donations", label: "Donations" },
  { id: "payouts", label: "Payouts" },
  { id: "requests", label: "Requests" },
  { id: "fulfillment", label: "Fulfillment" },
  { id: "merchants", label: "Merchants & pricing" },
];
const SOURCES = [
  { key: "donations", endpoint: "/api/admin/studio/donations" },
  { key: "payouts", endpoint: "/api/admin/studio/payouts" },
  { key: "requests", endpoint: "/api/admin/studio/requests" },
  { key: "merchants", endpoint: "/api/admin/studio/merchants" },
  {
    key: "priceHistory",
    endpoint: "/api/admin/studio/merchant-price-history",
  },
];

ModuleRegistry.registerModules(AllCommunityModule);

function getErrorMessage(body, status) {
  if (typeof body?.detail === "string") {
    return body.detail;
  }
  if (Array.isArray(body?.detail)) {
    return body.detail.map((error) => error.msg).join(", ");
  }
  return `Request failed (${status})`;
}

async function fetchReportData(token, signal) {
  const entries = await Promise.all(
    SOURCES.map(async ({ key, endpoint }) => {
      const response = await fetch(`${API_BASE_URL}${endpoint}`, {
        headers: { Authorization: `Bearer ${token}` },
        signal,
      });
      if (!response.ok) {
        let body = null;
        try {
          body = await response.json();
        } catch {
          // Use the status-based message when the server does not return JSON.
        }
        throw new Error(getErrorMessage(body, response.status));
      }

      const rows = await response.json();
      if (!Array.isArray(rows)) {
        throw new Error(`The ${key} report endpoint did not return a list.`);
      }
      return [key, rows];
    }),
  );
  const dashboardResponse = await fetch(`${API_BASE_URL}/api/admin/dashboard`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  if (!dashboardResponse.ok) {
    let body = null;
    try {
      body = await dashboardResponse.json();
    } catch {
      // Use the status-based message when the server does not return JSON.
    }
    throw new Error(getErrorMessage(body, dashboardResponse.status));
  }
  const dashboard = await dashboardResponse.json();
  return { ...Object.fromEntries(entries), dashboard };
}

function amount(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function currency(value) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(amount(value));
}

function readable(value) {
  if (!value) {
    return "Not recorded";
  }
  return String(value)
    .replaceAll("_", " ")
    .toLowerCase()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : new Intl.DateTimeFormat("en-US", {
        dateStyle: "medium",
      }).format(date);
}

function dateInputStart(value) {
  if (!value) {
    return null;
  }
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day);
}

function filterRowsByDate(rows, field, preset, startDate, endDate) {
  let start = null;
  let end = null;
  const today = new Date();

  if (preset === "today") {
    start = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    end = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  } else if (preset === "month") {
    start = new Date(today.getFullYear(), today.getMonth(), 1);
    end = new Date(today.getFullYear(), today.getMonth() + 1, 1);
  } else if (preset === "custom") {
    start = dateInputStart(startDate);
    const inclusiveEnd = dateInputStart(endDate);
    end = inclusiveEnd
      ? new Date(
          inclusiveEnd.getFullYear(),
          inclusiveEnd.getMonth(),
          inclusiveEnd.getDate() + 1,
        )
      : null;
  }

  if (start && end && start >= end) {
    return [];
  }

  return rows.filter((row) => {
    const timestamp = Date.parse(row[field]);
    return (
      Number.isFinite(timestamp) &&
      (!start || timestamp >= start.getTime()) &&
      (!end || timestamp < end.getTime())
    );
  });
}

function groupByCount(rows, field) {
  const counts = new Map();
  rows.forEach((row) => {
    const label = readable(row[field]);
    counts.set(label, (counts.get(label) || 0) + 1);
  });
  return [...counts.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label));
}

function groupPayoutStatuses(rows) {
  const counts = new Map();
  rows.forEach((row) => {
    const label = `${readable(row.status)} · ${readable(row.payout_mode)}`;
    counts.set(label, (counts.get(label) || 0) + 1);
  });
  return [...counts.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label));
}

function groupMonthlyAmount(rows, dateField) {
  const totals = new Map();
  rows.forEach((row) => {
    const date = new Date(row[dateField]);
    if (Number.isNaN(date.getTime())) {
      return;
    }
    const month = date.toISOString().slice(0, 7);
    totals.set(month, (totals.get(month) || 0) + amount(row.amount));
  });
  return [...totals.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([month, value]) => ({
      label: new Intl.DateTimeFormat("en-US", {
        month: "short",
        year: "2-digit",
        timeZone: "UTC",
      }).format(new Date(`${month}-01T00:00:00Z`)),
      value: Number(value.toFixed(2)),
    }));
}

function groupMerchantPrices(merchants) {
  return merchants
    .filter((merchant) => merchant.price_per_container_usd !== null)
    .map((merchant) => ({
      label: merchant.name,
      value: amount(merchant.price_per_container_usd),
    }))
    .sort((left, right) => right.value - left.value);
}

function approvedRequestsByCounty(requests) {
  return groupByCount(
    requests.filter((request) => request.status === "Approved"),
    "county",
  );
}

const LIFECYCLE_RANK = {
  submitted: 0,
  approved: 1,
  payout_pending: 2,
  payout_confirmed: 3,
  ready_for_pickup: 4,
  collected: 5,
};

function reachedLifecycle(request, stage) {
  return (
    Boolean(request[`${stage}_at`]) ||
    (LIFECYCLE_RANK[request.lifecycle_status] ?? -1) >= LIFECYCLE_RANK[stage]
  );
}

function elapsedDays(startValue, endValue) {
  const start = new Date(startValue).getTime();
  const end = new Date(endValue).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return null;
  }
  return (end - start) / 86400000;
}

function average(values) {
  return values.length
    ? values.reduce((total, value) => total + value, 0) / values.length
    : null;
}

function averageDaysByCounty(requests, startField, endField) {
  const durations = new Map();
  requests.forEach((request) => {
    const days = elapsedDays(request[startField], request[endField]);
    if (days === null) {
      return;
    }
    const label = request.county || "Not recorded";
    const values = durations.get(label) || [];
    values.push(days);
    durations.set(label, values);
  });
  return [...durations.entries()]
    .map(([label, values]) => ({
      label,
      value: Number(average(values).toFixed(1)),
    }))
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label));
}

function fulfillmentByCounty(requests) {
  const byCounty = new Map();
  requests.forEach((request) => {
    const label = request.county || "Not recorded";
    const stats = byCounty.get(label) || {
      label,
      approved: 0,
      confirmed: 0,
      collected: 0,
      approvalToPayoutDays: [],
      payoutToCollectedDays: [],
    };
    if (reachedLifecycle(request, "approved")) {
      stats.approved += 1;
    }
    if (reachedLifecycle(request, "payout_confirmed")) {
      stats.confirmed += 1;
    }
    if (reachedLifecycle(request, "collected")) {
      stats.collected += 1;
    }
    const approvalToPayout = elapsedDays(
      request.approved_at,
      request.payout_confirmed_at,
    );
    if (approvalToPayout !== null) {
      stats.approvalToPayoutDays.push(approvalToPayout);
    }
    const payoutToCollected = elapsedDays(
      request.ready_for_pickup_at || request.payout_confirmed_at,
      request.collected_at,
    );
    if (payoutToCollected !== null) {
      stats.payoutToCollectedDays.push(payoutToCollected);
    }
    byCounty.set(label, stats);
  });
  return [...byCounty.values()]
    .map((stats) => ({
      ...stats,
      averageApprovalToPayoutDays: average(stats.approvalToPayoutDays),
      averagePayoutToCollectedDays: average(stats.payoutToCollectedDays),
    }))
    .sort((left, right) => right.approved - left.approved || left.label.localeCompare(right.label));
}

function daysLabel(value) {
  return value === null ? "—" : `${value.toFixed(1)} days`;
}

function ChartPanel({ title, description, data, chartType = "bar", seriesName }) {
  const chartElement = useRef(null);
  const chartData = useMemo(() => data, [data]);

  useEffect(() => {
    if (!chartElement.current || chartData.length === 0) {
      return undefined;
    }
    const chart = AgCharts.create({
      container: chartElement.current,
      data: chartData,
      legend: { enabled: false },
      theme: {
        baseTheme: "ag-default",
        palette: {
          fills: GREEN_PALETTE,
          strokes: GREEN_PALETTE,
        },
      },
      series: [
        {
          type: chartType,
          xKey: "label",
          yKey: "value",
          yName: seriesName,
          marker: chartType === "line" ? { enabled: true } : undefined,
        },
      ],
    });
    return () => chart.destroy();
  }, [chartData, chartType, seriesName]);

  return (
    <article className="report-card report-chart-card">
      <header className="report-card-heading">
        <div>
          <h2>{title}</h2>
          {description && <p>{description}</p>}
        </div>
      </header>
      {chartData.length ? (
        <div
          aria-label={title}
          className="report-chart"
          ref={chartElement}
          role="img"
        />
      ) : (
        <p className="report-empty-chart">No report data available yet.</p>
      )}
    </article>
  );
}

function MetricCard({ label, value, detail, tone = "" }) {
  return (
    <article className={`report-metric ${tone ? `report-metric-${tone}` : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail && <small>{detail}</small>}
    </article>
  );
}

function ReportTable({ columns, rows, emptyMessage }) {
  return (
    <div className="report-table-wrap">
      <table className="report-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.label} scope="col">
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? (
            rows.map((row, index) => (
              <tr key={`${row.id ?? row.vendor_id ?? row.merchant_name ?? "row"}-${index}`}>
                {columns.map((column) => (
                  <td key={column.label}>{column.render(row)}</td>
                ))}
              </tr>
            ))
          ) : (
            <tr>
              <td className="report-table-empty" colSpan={columns.length}>
                {emptyMessage}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function ReportSection({ title, description, children }) {
  return (
    <section className="report-section">
      <div className="report-section-heading">
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
      {children}
    </section>
  );
}

function OverviewReport({ data }) {
  const totalDonations = data.donations.reduce(
    (total, donation) => total + amount(donation.amount),
    0,
  );
  const totalPayouts = data.payouts.reduce(
    (total, payout) => total + amount(payout.amount),
    0,
  );
  return (
    <>
      <div className="report-metrics">
        <MetricCard
          detail={`${data.donations.length.toLocaleString("en-US")} captured records`}
          label="Donations"
          value={currency(totalDonations)}
        />
        <MetricCard
          detail={`${data.payouts.length.toLocaleString("en-US")} tracked records`}
          label="Payouts"
          value={currency(totalPayouts)}
        />
        <MetricCard
          detail={`${Number(data.dashboard.collected_count).toLocaleString("en-US")} collected · ${Number(data.dashboard.total_requests_count).toLocaleString("en-US")} total requests`}
          label="Families funded · PayPal confirmed"
          value={Number(data.dashboard.families_supported_count).toLocaleString("en-US")}
        />
        <MetricCard
          detail="Current supplier directory"
          label="Merchants"
          value={data.merchants.length.toLocaleString("en-US")}
        />
      </div>
      <div className="report-chart-grid">
        <ChartPanel
          data={groupMonthlyAmount(data.donations, "created_at")}
          description="Captured donation totals by month."
          seriesName="Donations (USD)"
          title="Donation trend"
          chartType="line"
        />
        <ChartPanel
          data={groupByCount(data.requests, "lifecycle_status")}
          description="Requests by their current support stage."
          seriesName="Requests"
          title="Support pipeline"
        />
        <ChartPanel
          data={approvedRequestsByCounty(data.requests)}
          description="Requests approved for support, grouped by the reported county."
          seriesName="Approved requests"
          title="Approved requests by county"
        />
      </div>
    </>
  );
}

function DonationsReport({ data }) {
  return (
    <>
      <ChartPanel
        data={groupMonthlyAmount(data.donations, "created_at")}
        description="Captured donation totals by month."
        seriesName="Donations (USD)"
        title="Donations by month"
        chartType="line"
      />
      <ReportSection
        description="Donations are pooled and donor locations are not collected, so donation totals cannot be accurately assigned to counties."
        title="Recent donations"
      >
        <p className="report-location-note">
          For a geographic view of approved support requests, see the Requests
          report. Those county counts describe request locations and are not an
          allocation of donations.
        </p>
        <ReportTable
          columns={[
            { label: "Date", render: (row) => dateLabel(row.created_at) },
            { label: "Amount", render: (row) => currency(row.amount) },
            { label: "Currency", render: (row) => row.currency || "—" },
          ]}
          emptyMessage="No donations have been recorded."
          rows={data.donations.slice(0, 25)}
        />
      </ReportSection>
    </>
  );
}

function PayoutsReport({ data, token, datePreset, dateFrom, dateTo, retry }) {
  return (
    <>
      <SupplierPayoutsReport
        data={data}
        token={token}
        datePreset={datePreset}
        dateFrom={dateFrom}
        dateTo={dateTo}
        retry={retry}
      />
      <ChartPanel
        data={groupPayoutStatuses(data.payouts)}
        description="Tracked payout records by status and payment mode. A submission response marked Success is not confirmed until a PayPal status refresh or verified webhook."
        seriesName="Payouts"
        title="Payout status"
      />
      <ReportSection
        description="Recent tracked transactions. Recipient account and batch identifiers are omitted."
        title="Recent payouts"
      >
        <ReportTable
          columns={[
            { label: "Date", render: (row) => dateLabel(row.created_at) },
            { label: "Request", render: (row) => `#${row.request_id}` },
            {
              label: "Containers",
              render: (row) =>
                row.containers == null ? "—" : row.containers.toLocaleString("en-US"),
            },
            { label: "Amount", render: (row) => currency(row.amount) },
            { label: "Status", render: (row) => readable(row.status) },
            { label: "Mode", render: (row) => readable(row.payout_mode) },
          ]}
          emptyMessage="No payout records have been recorded."
          rows={data.payouts.slice(0, 25)}
        />
      </ReportSection>
    </>
  );
}

function SupplierPayoutsReport({
  data,
  token,
  datePreset,
  dateFrom,
  dateTo,
  retry,
}) {
  const [supplier, setSupplier] = useState("");
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const dateBounds = useMemo(() => {
    if (datePreset === "custom") {
      return { start: dateFrom, end: dateTo };
    }
    if (datePreset === "all") {
      return { start: "", end: "" };
    }
    const now = new Date();
    const formatDate = (date) =>
      `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    if (datePreset === "today") {
      const today = formatDate(now);
      return { start: today, end: today };
    }
    const firstOfMonth = new Date(now.getFullYear(), now.getMonth(), 1);
    return { start: formatDate(firstOfMonth), end: formatDate(now) };
  }, [dateFrom, datePreset, dateTo]);

  useEffect(() => {
    if (!supplier) {
      setReport(null);
      setError("");
      setLoading(false);
      return undefined;
    }
    if (dateBounds.start && dateBounds.end && dateBounds.start > dateBounds.end) {
      setReport(null);
      setError("");
      setLoading(false);
      return undefined;
    }

    const controller = new AbortController();
    const loadSupplierReport = async () => {
      setLoading(true);
      setError("");
      const query = new URLSearchParams({ supplier });
      if (dateBounds.start) query.set("start_date", dateBounds.start);
      if (dateBounds.end) query.set("end_date", dateBounds.end);
      try {
        const response = await fetch(
          `${API_BASE_URL}/api/admin/reports/supplier-payouts?${query}`,
          {
            headers: { Authorization: "Bearer " + token },
            signal: controller.signal,
          },
        );
        if (!response.ok) {
          let body = null;
          try {
            body = await response.json();
          } catch {
            // Use the status-based message when the server does not return JSON.
          }
          throw new Error(getErrorMessage(body, response.status));
        }
        setReport(await response.json());
      } catch (loadError) {
        if (!controller.signal.aborted) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Could not load supplier payout report.",
          );
          setReport(null);
        }
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      }
    };
    void loadSupplierReport();
    return () => controller.abort();
  }, [dateBounds, retry, supplier]);

  const supplierOptions = [...new Set(data.merchants.map((merchant) => merchant.name))]
    .sort((left, right) => left.localeCompare(right));
  const periodLabel =
    datePreset === "all"
      ? "All dates"
      : datePreset === "today"
        ? "Today"
        : datePreset === "month"
          ? "This month"
          : (dateFrom || "Any start") +
            " through " +
            (dateTo || "Any end");

  return (
    <ReportSection
      description="Confirmed PayPal supplier payouts, grouped by the date the payment was confirmed. Pending, failed, and simulated payouts are excluded."
      title="Supplier payments and containers"
    >
      <div className="supplier-report-controls">
        <label>
          Supplier
          <select
            aria-label="Supplier for payout report"
            onChange={(event) => setSupplier(event.target.value)}
            value={supplier}
          >
            <option value="">Select a supplier</option>
            {supplierOptions.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </label>
        <p>{periodLabel}</p>
      </div>
      {error && <div className="notice error" role="alert">{error}</div>}
      {supplier && loading && !report && (
        <p className="report-location-note" role="status">Loading confirmed supplier payments…</p>
      )}
      {!supplier ? (
        <p className="report-location-note">Select a supplier to see its confirmed payments and containers purchased.</p>
      ) : report ? (
        <>
          <div className="report-metrics supplier-report-metrics">
            <MetricCard
              label="Total sent · PayPal confirmed"
              value={currency(report.total_amount_usd)}
            />
            <MetricCard
              label="Containers purchased"
              value={Number(report.total_containers).toLocaleString("en-US")}
            />
            <MetricCard
              label="Confirmed payments"
              value={Number(report.payment_count).toLocaleString("en-US")}
            />
          </div>
          <ReportTable
            columns={[
              { label: "Confirmed date", render: (row) => dateLabel(row.confirmed_at) },
              { label: "Request", render: (row) => "#" + row.request_id },
              {
                label: "Containers",
                render: (row) =>
                  row.containers == null ? "—" : row.containers.toLocaleString("en-US"),
              },
              { label: "Amount sent", render: (row) => currency(row.amount) },
            ]}
            emptyMessage="No confirmed supplier payments were found in this period."
            rows={report.payments}
          />
          {report.payment_count > report.payments.length && (
            <p className="report-location-note">
              Showing the latest {report.payments.length} payments. Totals include all matching payments.
            </p>
          )}
        </>
      ) : null}
    </ReportSection>
  );
}

function RequestsReport({ data }) {
  return (
    <>
      <div className="report-chart-grid">
        <ChartPanel
          data={groupByCount(data.requests, "lifecycle_status")}
          description="Current operational stage for each request."
          seriesName="Requests"
          title="Requests by lifecycle"
        />
        <ChartPanel
          data={groupByCount(data.requests, "status")}
          description="Review outcome/status for each request."
          seriesName="Requests"
          title="Requests by review status"
        />
        <ChartPanel
          data={approvedRequestsByCounty(data.requests)}
          description="Only approved requests are included, grouped by their reported county."
          seriesName="Approved requests"
          title="Approved requests by county"
        />
      </div>
      <ReportSection
        description="Personal contact details and precise locations are intentionally excluded."
        title="Recent requests"
      >
        <ReportTable
          columns={[
            { label: "Submitted", render: (row) => dateLabel(row.submitted_at) },
            { label: "Request", render: (row) => `#${row.id}` },
            { label: "County", render: (row) => row.county || "Not recorded" },
            { label: "Review status", render: (row) => readable(row.status) },
            {
              label: "Lifecycle",
              render: (row) => readable(row.lifecycle_status),
            },
            {
              label: "Merchant",
              render: (row) => row.matched_vendor || "Not assigned",
            },
            {
              label: "Support cost",
              render: (row) =>
                row.total_cost_usd === null ? "—" : currency(row.total_cost_usd),
            },
          ]}
          emptyMessage="No support requests have been recorded."
          rows={data.requests.slice(0, 25)}
        />
      </ReportSection>
    </>
  );
}

function FulfillmentReport({ data }) {
  const countyStats = fulfillmentByCounty(data.requests);
  const now = Date.now();
  const waitingForPickup = data.requests
    .filter(
      (request) =>
        request.lifecycle_status === "ready_for_pickup" && !request.collected_at,
    )
    .map((request) => ({
      ...request,
      waitingDays: elapsedDays(request.ready_for_pickup_at, now),
    }))
    .sort((left, right) => (right.waitingDays ?? 0) - (left.waitingDays ?? 0));
  const approvedCount = countyStats.reduce((total, county) => total + county.approved, 0);
  const confirmedCount = countyStats.reduce((total, county) => total + county.confirmed, 0);
  const collectedCount = countyStats.reduce((total, county) => total + county.collected, 0);
  const averagePickupWait = average(
    waitingForPickup
      .map((request) => request.waitingDays)
      .filter((days) => days !== null),
  );

  return (
    <>
      <div className="report-metrics fulfillment-metrics">
        <MetricCard
          detail="By reported county"
          label="Requests approved"
          tone="green"
          value={approvedCount.toLocaleString("en-US")}
        />
        <MetricCard
          detail="Verified PayPal payouts"
          label="Payouts confirmed"
          tone="teal"
          value={confirmedCount.toLocaleString("en-US")}
        />
        <MetricCard
          detail="Pickup completed"
          label="Requests collected"
          tone="lime"
          value={collectedCount.toLocaleString("en-US")}
        />
        <MetricCard
          detail={`Average wait across ${waitingForPickup.length.toLocaleString("en-US")} requests`}
          label="Waiting for pickup"
          tone="gold"
          value={daysLabel(averagePickupWait)}
        />
      </div>
      <div className="report-chart-grid">
        <ChartPanel
          data={countyStats.flatMap((county) => [
            { label: `${county.label} · Approved`, value: county.approved },
            { label: `${county.label} · Confirmed`, value: county.confirmed },
            { label: `${county.label} · Collected`, value: county.collected },
          ])}
          description="Approved, payout-confirmed, and collected requests, grouped by county."
          seriesName="Requests"
          title="Fulfillment stages by county"
        />
        <ChartPanel
          data={averageDaysByCounty(data.requests, "approved_at", "payout_confirmed_at")}
          description="Average time from approval to verified payout confirmation for completed payouts."
          seriesName="Average days"
          title="Approval to payout confirmation"
        />
        <ChartPanel
          data={averageDaysByCounty(
            data.requests,
            "ready_for_pickup_at",
            "collected_at",
          )}
          description="Average time from ready-for-pickup to collection, for completed pickups."
          seriesName="Average days"
          title="Pickup to collection"
        />
      </div>
      <ReportSection
        description="Counts follow each request through the support lifecycle; averages use records with both stage timestamps."
        title="County fulfillment summary"
      >
        <ReportTable
          columns={[
            { label: "County", render: (row) => row.label },
            { label: "Approved", render: (row) => row.approved.toLocaleString("en-US") },
            { label: "Payout confirmed", render: (row) => row.confirmed.toLocaleString("en-US") },
            { label: "Collected", render: (row) => row.collected.toLocaleString("en-US") },
            {
              label: "Avg. approval to payout",
              render: (row) => daysLabel(row.averageApprovalToPayoutDays),
            },
            {
              label: "Avg. ready to collected",
              render: (row) => daysLabel(row.averagePayoutToCollectedDays),
            },
          ]}
          emptyMessage="No fulfillment data has been recorded."
          rows={countyStats}
        />
      </ReportSection>
      <ReportSection
        description="Requests currently marked ready for pickup, ordered by longest wait. Recipient names and contact details are excluded."
        title="Waiting for pickup"
      >
        <ReportTable
          columns={[
            { label: "Request", render: (row) => `#${row.id}` },
            { label: "County", render: (row) => row.county || "Not recorded" },
            { label: "Ready since", render: (row) => dateLabel(row.ready_for_pickup_at) },
            { label: "Waiting", render: (row) => daysLabel(row.waitingDays) },
          ]}
          emptyMessage="No requests are currently waiting for pickup."
          rows={waitingForPickup.slice(0, 50)}
        />
      </ReportSection>
    </>
  );
}

function MerchantsReport({ data }) {
  const priceChanges = data.priceHistory.slice(0, 25);
  return (
    <>
      <div className="report-chart-grid">
        <ChartPanel
          data={groupByCount(data.merchants, "county")}
          description="Chemists grouped by their recorded county."
          seriesName="Chemists"
          title="Chemists by county"
        />
        <ChartPanel
          data={groupMerchantPrices(data.merchants)}
          description="Current unit price per container, where configured."
          seriesName="USD per container"
          title="Current merchant prices"
        />
      </div>
      <ReportSection
        description="Chemist location details provided in the supplier directory."
        title="Chemist locations"
      >
        <ReportTable
          columns={[
            { label: "Chemist", render: (row) => row.name },
            { label: "County", render: (row) => row.county || "Not recorded" },
            { label: "Town / locality", render: (row) => row.location || "—" },
          ]}
          emptyMessage="No chemists have been added to the directory."
          rows={data.merchants}
        />
      </ReportSection>
      <ReportSection
        description="Most recent available price comparisons. A change is shown only when two observations exist."
        title="Price history"
      >
        <ReportTable
          columns={[
            { label: "Merchant", render: (row) => row.merchant_name },
            {
              label: "Previous price",
              render: (row) => currency(row.previous_price_per_container_usd),
            },
            {
              label: "Latest price",
              render: (row) => currency(row.price_per_container_usd),
            },
            {
              label: "Change",
              render: (row) =>
                `${row.change_percent > 0 ? "+" : ""}${amount(row.change_percent).toFixed(1)}%`,
            },
            { label: "Observed", render: (row) => dateLabel(row.observed_at) },
          ]}
          emptyMessage="Price comparisons will appear after at least two observations per merchant."
          rows={priceChanges}
        />
      </ReportSection>
    </>
  );
}

const reportComponents = {
  overview: OverviewReport,
  donations: DonationsReport,
  payouts: PayoutsReport,
  requests: RequestsReport,
  fulfillment: FulfillmentReport,
  merchants: MerchantsReport,
};

export default function ReportsPage() {
  const { reportId } = useParams();
  const [token] = useState(
    () => window.sessionStorage.getItem("formulaflow-admin-session") || "",
  );
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(Boolean(token));
  const [retry, setRetry] = useState(0);
  const [datePreset, setDatePreset] = useState("all");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const activeReport = REPORTS.find((report) => report.id === reportId);
  const Report = activeReport ? reportComponents[activeReport.id] : null;
  const filteredData = useMemo(() => {
    if (!data || !["donations", "payouts", "requests"].includes(reportId)) {
      return data;
    }
    const field = reportId === "requests" ? "submitted_at" : "created_at";
    const key = reportId;
    return {
      ...data,
      [key]: filterRowsByDate(data[key], field, datePreset, dateFrom, dateTo),
    };
  }, [data, dateFrom, datePreset, dateTo, reportId]);
  const filterableRows =
    ["donations", "payouts", "requests"].includes(reportId)
      ? filteredData?.[reportId]
      : null;
  const invalidDateRange =
    datePreset === "custom" && dateFrom && dateTo && dateFrom > dateTo;

  useEffect(() => {
    if (!token) {
      return undefined;
    }
    let active = true;
    let isRefreshing = false;
    let controller;
    const loadReports = async (showLoading = false) => {
      if (isRefreshing) {
        return;
      }
      isRefreshing = true;
      const requestController = new AbortController();
      controller = requestController;
      if (showLoading) {
        setIsLoading(true);
        setError("");
      }
      try {
        const reportData = await fetchReportData(
          token,
          requestController.signal,
        );
        if (active) {
          setData(reportData);
          setError("");
        }
      } catch (loadError) {
        if (active && !requestController.signal.aborted) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Could not load reports.",
          );
        }
      } finally {
        isRefreshing = false;
        if (active && showLoading) {
          setIsLoading(false);
        }
      }
    };
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") {
        void loadReports();
      }
    };

    void loadReports(true);
    const intervalId = window.setInterval(refreshWhenVisible, 30000);
    window.addEventListener("focus", refreshWhenVisible);
    return () => {
      active = false;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshWhenVisible);
      controller?.abort();
    };
  }, [retry, token]);

  if (!reportId) {
    return <Navigate replace to="/reports/overview" />;
  }
  if (!activeReport) {
    return <Navigate replace to="/reports/overview" />;
  }

  return (
    <section className="reports-page">
      <header className="reports-heading">
        <div>
          <p className="reports-eyebrow">INTERNAL REPORTS</p>
          <h1>FormulaFlow reports</h1>
          <p>Read-only summaries for donations, support, payouts, and pricing.</p>
        </div>
        <span className="reports-data-note">Admin access · privacy-filtered data</span>
      </header>

      <nav className="report-tabs" aria-label="Report pages">
        {REPORTS.map((report) => (
          <NavLink
            className={({ isActive }) =>
              isActive ? "report-tab active" : "report-tab"
            }
            key={report.id}
            to={`/reports/${report.id}`}
          >
            {report.label}
          </NavLink>
        ))}
      </nav>

      <div className="report-page-heading">
        <h2>{activeReport.label}</h2>
        <div>
          <span>Auto-refreshes every 30 seconds · green chart palette</span>
          <button
            className="secondary-button"
            disabled={isLoading}
            onClick={() => setRetry((attempt) => attempt + 1)}
            type="button"
          >
            Refresh reports
          </button>
        </div>
      </div>
      {token && ["donations", "payouts", "requests"].includes(reportId) && (
        <section className="report-date-filters" aria-label="Report date filters">
          <label>
            Date range
            <select
              onChange={(event) => setDatePreset(event.target.value)}
              value={datePreset}
            >
              <option value="all">All dates</option>
              <option value="today">Today</option>
              <option value="month">This month</option>
              <option value="custom">Custom period</option>
            </select>
          </label>
          {datePreset === "custom" && (
            <>
              <label>
                From
                <input
                  aria-label="Start date"
                  onChange={(event) => setDateFrom(event.target.value)}
                  type="date"
                  value={dateFrom}
                />
              </label>
              <label>
                Through
                <input
                  aria-label="End date"
                  onChange={(event) => setDateTo(event.target.value)}
                  type="date"
                  value={dateTo}
                />
              </label>
            </>
          )}
          <p aria-live="polite">
            {invalidDateRange
              ? "End date must be on or after the start date."
              : `${filterableRows?.length ?? 0} ${
                  reportId === "donations" ? "donations" : reportId
                } in this range`}
          </p>
        </section>
      )}

      {!token ? (
        <section className="report-state">
          <h2>Admin sign-in required</h2>
          <p>Sign in under Internal operations to view these reports.</p>
          <Link className="primary-button" to="/admin">
            Go to admin sign-in
          </Link>
        </section>
      ) : isLoading && !data ? (
        <section className="report-state" role="status">
          Loading report data…
        </section>
      ) : error && !data ? (
        <section className="report-state" role="alert">
          <h2>Could not load reports</h2>
          <p>{error}</p>
          <button
            className="primary-button"
            onClick={() => setRetry((attempt) => attempt + 1)}
            type="button"
          >
            Retry
          </button>
        </section>
      ) : data ? (
        <>
          {error && (
            <div className="notice error" role="status">
              Could not refresh the latest report data: {error}. Showing the
              last successfully loaded data.
            </div>
          )}
          <Report
            data={filteredData}
            token={token}
            datePreset={datePreset}
            dateFrom={dateFrom}
            dateTo={dateTo}
            retry={retry}
          />
        </>
      ) : null}
    </section>
  );
}
