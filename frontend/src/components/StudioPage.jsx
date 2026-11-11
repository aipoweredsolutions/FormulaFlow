import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AgStudio, AgStudioProvider, createWidgets } from "ag-studio-react";
import {
  AgStudioAiModule,
  createAiHarness,
  directLlmRunner,
} from "ag-studio";
import { createOpenAiBackendAdapter } from "./openAiBackendAdapter.js";
import { formulaFlowWidgetDefinitions } from "./StudioCustomWidgets.jsx";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const LICENSE_KEY = import.meta.env.VITE_AG_STUDIO_LICENSE_KEY;
const STUDIO_SOURCES = [
  {
    id: "donations",
    name: "Donations",
    description: "Synthetic PayPal sandbox donation records.",
    endpoint: "/api/admin/studio/donations",
  },
  {
    id: "payouts",
    name: "Payouts",
    description: "Synthetic payout records; recipient details are excluded.",
    endpoint: "/api/admin/studio/payouts",
  },
  {
    id: "requests",
    name: "Requests",
    description: "Synthetic formula support requests without personal details.",
    endpoint: "/api/admin/studio/requests",
  },
  {
    id: "merchants",
    name: "Merchants",
    description: "Synthetic merchant records without PayPal account details.",
    endpoint: "/api/admin/studio/merchants",
  },
  {
    id: "merchant_price_history",
    name: "Merchant Price History",
    description: "Merchant price changes without account details.",
    endpoint: "/api/admin/studio/merchant-price-history",
  },
];

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

export default function StudioPage() {
  const [token] = useState(
    () => window.sessionStorage.getItem("formulaflow-admin-session") || "",
  );
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [isLoading, setIsLoading] = useState(Boolean(token));
  const [retry, setRetry] = useState(0);
  const widgets = useMemo(
    () => (widgetConfig) =>
      createWidgets({
        additionalTypes: formulaFlowWidgetDefinitions,
        menu: [
          ...widgetConfig.menu,
          {
            label: "FormulaFlow",
            widgetIds: ["aid-pipeline-funnel", "impact-scoreboard"],
          },
        ],
      }),
    [],
  );
  const adapter = useMemo(
    () =>
      createOpenAiBackendAdapter({
        endpoint: `${API_BASE_URL}/api/llm`,
        getAccessToken: () => token,
      }),
    [token],
  );
  const ai = useMemo(
    () =>
      ({ api }) =>
        createAiHarness(api, ({ builtIn, tools }) => {
          const agStudioDefaultAgents = Object.values(builtIn).map(
            (definition) => {
              if (definition.id !== "lead") {
                return directLlmRunner({ ...definition, adapter });
              }

              return directLlmRunner({
                ...definition,
                adapter,
                tools: (context, params) => [
                  ...definition
                    .tools(context, params)
                    .filter((tool) => tool.name !== "delegate_to"),
                  context.tools.delegateTo([
                    "data",
                    "widget",
                    "page",
                    "planning",
                    "opsAnalyst",
                  ]),
                ],
                instructions: (context, params) =>
                  `${definition.instructions(context, params)}

For FormulaFlow operational reviews, delegate to opsAnalyst when asked to
review payouts pending over 48 hours, merchant price increases, or unusual
donation patterns. The analyst can only query/read dashboard data and build
widgets; its findings are suggestions for staff review, never approvals or
payments.`,
              });
            },
          );
          const { studio } = tools;
          let configureCallId = 0;
          const configureFindingWidget = api.defineAiTool({
            name: "configure_finding_widget",
            description:
              "Configure a newly added finding widget using its widget type, id, and JSON configuration.",
            params: (s) =>
              s.object({
                widgetType: s.widgetType(),
                widgetId: s.string(),
                configurationJson: s.string(),
              }),
            execute: async ({ widgetType, widgetId, configurationJson }, ctx) => {
              let configuration;
              try {
                configuration = JSON.parse(configurationJson);
              } catch {
                return ctx.error("configurationJson must contain valid JSON.");
              }
              if (
                typeof configuration !== "object" ||
                configuration === null ||
                Array.isArray(configuration)
              ) {
                return ctx.error("The widget configuration must be a JSON object.");
              }

              const configureTool = studio.configureWidget({
                widgetType,
                widgetId,
              });
              if (!configureTool.execute) {
                return ctx.error("The Studio configure-widget tool is unavailable.");
              }
              return configureTool.execute(
                {
                  toolCallId: `ops-analyst-config-${++configureCallId}`,
                  name: configureTool.name,
                  args: { widgetId, ...configuration },
                },
                ctx,
              );
            },
          });
          const opsAnalyst = directLlmRunner({
            id: "opsAnalyst",
            name: "Operations Analyst",
            description:
              "Finds operational patterns and builds dashboard widgets for staff review.",
            adapter,
            tools: () => [
              studio.viewSchema(),
              studio.viewReport(),
              studio.viewPage(),
              studio.viewWidget(),
              studio.executeQuery(),
              studio.addWidget(),
              studio.positionWidget(),
              configureFindingWidget,
            ],
            instructions: ({ api: studioApi }) => {
              const context = studioApi.getAiContext();
              const { tables, relationships } = context.schema();
              const catalogue = context.catalogue();
              const schemaDescription = tables
                .map(
                  (table) =>
                    `${table.name} (${table.reference}): ${table.fields
                      .map(
                        (field) =>
                          `${field.name} [${field.dataType}; ${field.reference}]`,
                      )
                      .join(", ")}`,
                )
                .join("\n");
              const relationshipDescription = relationships
                .map(
                  (relationship) =>
                    `${relationship.sourceTableId} ${relationship.type} ${relationship.targetTableId}`,
                )
                .join("\n");
              const widgetDescription = catalogue
                .map(
                  (widget) =>
                    `${widget.name} (${widget.type}): ${widget.description}${
                      widget.usage ? ` ${widget.usage}` : ""
                    }${
                      widget.configuration
                        ? ` Configuration: ${widget.configuration}`
                        : ""
                    }`,
                )
                .join("\n");

              return `You are FormulaFlow's opsAnalyst. Monitor the live dashboard data for:
- Payouts still pending more than 48 hours, using payout status and the best available pending/created timestamp.
- Merchant price increases, comparing a merchant's dated price-history values. A single price observation is not proof of an increase.
- Unusual donation patterns, identifying explainable outliers in amount, frequency, or timing relative to the available donation data. Describe these as unusual patterns, not fraud.

Use execute_query to verify each finding against the data before reporting it. Explain every finding in plain language, including the relevant dates/amounts and why it merits attention. For each finding, create a separate useful widget: add it, position it, and configure it with the relevant data and an appropriate title. Use exact field references from the live schema and consult the widget catalogue for valid configurations. If the schema or data cannot support a check, state that limitation instead of guessing.

Every response and every finding/widget title must be clearly labelled exactly: "suggestion for staff review". Your findings are advisory only. You must not approve or reject cases, initiate or confirm payouts, change operational records, or imply that you have done so. You have no tools for those actions.

## Live data schema
${schemaDescription || "No data tables are currently available."}

## Table relationships
${relationshipDescription || "No table relationships are currently available."}

## Available widget types
${widgetDescription || "No widget types are currently available."}`;
            },
          });

          return {
            agents: [...agStudioDefaultAgents, opsAnalyst],
            primary: "lead",
          };
        }),
    [adapter],
  );

  useEffect(() => {
    if (!token) {
      return undefined;
    }

    let active = true;
    let isRefreshing = false;
    let controller;
    const loadSources = async (showLoading = false) => {
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
        const sources = await Promise.all(
          STUDIO_SOURCES.map(async ({ endpoint, ...source }) => {
            const response = await fetch(`${API_BASE_URL}${endpoint}`, {
              headers: { Authorization: `Bearer ${token}` },
              signal: requestController.signal,
            });
            if (!response.ok) {
              throw new Error(await getErrorMessage(response));
            }

            const rows = await response.json();
            if (!Array.isArray(rows)) {
              throw new Error(`The ${source.name} endpoint did not return a list.`);
            }

            return { ...source, data: rows };
          }),
        );
        if (active) {
          setData({ sources });
          setError("");
        }
      } catch (loadError) {
        if (active && !requestController.signal.aborted) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "Could not load Studio data.",
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
        void loadSources();
      }
    };

    void loadSources(true);
    const intervalId = window.setInterval(refreshWhenVisible, 30000);
    window.addEventListener("focus", refreshWhenVisible);
    return () => {
      active = false;
      window.clearInterval(intervalId);
      window.removeEventListener("focus", refreshWhenVisible);
      controller?.abort();
    };
  }, [retry, token]);

  return (
    <main className="studio-page">
      <header className="studio-toolbar">
        <Link to="/admin">Back to Internal operations</Link>
        <span>
          {error && data
            ? `Studio refresh failed: ${error}`
            : "Data auto-refreshes every 30 seconds"}
        </span>
        <strong>DEMO / PAYPAL SANDBOX — SYNTHETIC DATA ONLY</strong>
      </header>

      {!token ? (
        <section className="studio-message">
          <div>
            <h1>Admin sign-in required</h1>
            <p>Sign in to Internal operations to view the analytics studio.</p>
            <Link className="primary-button" to="/admin">
              Go to admin sign-in
            </Link>
          </div>
        </section>
      ) : isLoading ? (
        <section className="studio-message" role="status">
          Loading demo data…
        </section>
      ) : error && !data ? (
        <section className="studio-message" role="alert">
          <div>
            <h1>Could not load Studio data</h1>
            <p>{error}</p>
            <button
              className="primary-button"
              onClick={() => setRetry((attempt) => attempt + 1)}
              type="button"
            >
              Retry
            </button>
          </div>
        </section>
      ) : data ? (
        <div className="studio-canvas">
          <AgStudioProvider
            licenseKey={LICENSE_KEY || undefined}
            modules={[AgStudioAiModule]}
          >
            <AgStudio
              ai={ai}
              data={data}
              mode="edit"
              style={{ height: "100%", width: "100%" }}
              widgets={widgets}
            />
          </AgStudioProvider>
        </div>
      ) : null}
    </main>
  );
}
