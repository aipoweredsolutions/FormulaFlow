import { useEffect, useMemo, useRef, useState } from "react";
import { AgGridReact } from "ag-grid-react";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";
const EMPTY_ANOMALIES = { payout_issues: [], price_alerts: [] };

function formatDashboardUsd(value) {
  const amount = Number(value);
  return `USD ${
    Number.isFinite(amount)
      ? amount.toLocaleString("en-US", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })
      : "0.00"
  }`;
}

function formatBirthMonth(value) {
  if (!value) {
    return "Not recorded";
  }
  const [year, month] = value.slice(0, 7).split("-").map(Number);
  if (!year || !month) {
    return "Not recorded";
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(year, month - 1, 1)));
}

function birthMonthValue(value) {
  const match = /^(\d{4})-(\d{2})(?:-01)?$/.exec(String(value).trim());
  if (!match || Number(match[2]) < 1 || Number(match[2]) > 12) {
    throw new Error("Enter a birth month as YYYY-MM.");
  }
  return `${match[1]}-${match[2]}-01`;
}

function recommendedContainerCount(request, availableBalance) {
  const required = Number(request.containers_per_month) || 1;
  const unitCost = Number(request.total_cost_usd) / required;
  if (!Number.isFinite(unitCost) || unitCost <= 0) {
    return 1;
  }
  const affordable = Math.floor((Number(availableBalance) + 1e-8) / unitCost);
  return Math.min(required, Math.max(1, affordable));
}

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

export default function AdminGrid() {
  const [token, setToken] = useState(
    () => {
      const savedToken = window.sessionStorage.getItem("formulaflow-admin-session");
      if (savedToken === "offline-demo-token") {
        window.sessionStorage.removeItem("formulaflow-admin-session");
        return "";
      }
      return savedToken || "";
    },
  );
  const [adminAccount, setAdminAccount] = useState(null);
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [currentAdminPassword, setCurrentAdminPassword] = useState("");
  const [newAdminPassword, setNewAdminPassword] = useState("");
  const [confirmAdminPassword, setConfirmAdminPassword] = useState("");
  const [authMode, setAuthMode] = useState("login");
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const [isAuthenticating, setIsAuthenticating] = useState(false);
  const [isChangingPassword, setIsChangingPassword] = useState(false);
  const [requests, setRequests] = useState([]);
  const [dashboard, setDashboard] = useState(null);
  const [anomalies, setAnomalies] = useState(EMPTY_ANOMALIES);
  const [vendors, setVendors] = useState([]);
  const [savingSupplierIds, setSavingSupplierIds] = useState(() => new Set());
  const [savingRequestIds, setSavingRequestIds] = useState(() => new Set());
  const [pendingRequestActions, setPendingRequestActions] = useState(
    () => new Set(),
  );
  const [selectedRequest, setSelectedRequest] = useState(null);
  const [purchaseContainers, setPurchaseContainers] = useState(1);
  const [selectedDecision, setSelectedDecision] = useState(null);
  const [reviewNote, setReviewNote] = useState("");
  const [requestHistory, setRequestHistory] = useState(null);
  const [historyRequest, setHistoryRequest] = useState(null);
  const [merchantSelections, setMerchantSelections] = useState({});
  const [selectedLocation, setSelectedLocation] = useState("");
  const [requestSearch, setRequestSearch] = useState("");
  const [gridDensity, setGridDensity] = useState("comfortable");
  const [agentQuestion, setAgentQuestion] = useState("");
  const [agentAnswer, setAgentAnswer] = useState("");
  const [pickupToken, setPickupToken] = useState("");
  const [pickupTokenExpiresAt, setPickupTokenExpiresAt] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isPaying, setIsPaying] = useState(false);
  const [isQuerying, setIsQuerying] = useState(false);
  const [isSavingMerchant, setIsSavingMerchant] = useState(false);
  const [hasLoaded, setHasLoaded] = useState(false);
  const [merchantForm, setMerchantForm] = useState({
    name: "",
    location: "",
    county: "",
    paypal_email: "",
    price_per_container_usd: "",
  });

  useEffect(() => {
    let active = true;
    fetch(`${API_BASE_URL}/api/auth/status`)
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(await getErrorMessage(response));
        }
        return response.json();
      })
      .then((result) => {
        if (active) {
          setRegistrationOpen(result.registration_open);
        }
      })
      .catch((statusError) => {
        if (active) {
          setError(
            statusError instanceof Error
              ? statusError.message
              : "Could not check admin account setup.",
          );
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!token) {
      return undefined;
    }
    let active = true;
    const restoreSession = async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/api/auth/me`, {
          headers: authHeaders(),
        });
        if (!response.ok) {
          window.sessionStorage.removeItem("formulaflow-admin-session");
          if (active) {
            setToken("");
            setHasLoaded(false);
          }
          return;
        }
        const account = await response.json();
        if (active) {
          setAdminAccount(account);
          await loadRequests(undefined, token);
        }
      } catch {
        if (active) {
          setError("Could not restore the admin session. Check that the API is running.");
        }
      }
    };
    void restoreSession();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!hasLoaded || !token) {
      return undefined;
    }
    let active = true;
    const refresh = async () => {
      try {
        await refreshAdminView();
      } catch (refreshError) {
        if (active) {
          setError(
            refreshError instanceof Error
              ? refreshError.message
              : "Could not refresh operational alerts.",
          );
        }
      }
    };
    const intervalId = window.setInterval(refresh, 30000);
    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, [hasLoaded, token]);

  function authHeaders() {
    return { Authorization: `Bearer ${token}` };
  }

  async function submitAuthentication(event) {
    event.preventDefault();
    setIsAuthenticating(true);
    setError("");
    setNotice("");
    try {
      const credentials = { email: authEmail, password: authPassword };
      if (authMode === "register") {
        const registration = await fetch(`${API_BASE_URL}/api/auth/register`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(credentials),
        });
        if (!registration.ok) {
          throw new Error(await getErrorMessage(registration));
        }
      }

      const response = await fetch(`${API_BASE_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(credentials),
      });
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const result = await response.json();
      window.sessionStorage.setItem(
        "formulaflow-admin-session",
        result.access_token,
      );
      setToken(result.access_token);
      setAdminAccount({ id: result.id, email: result.email, role: result.role });
      setAuthPassword("");
      setRegistrationOpen(false);
      setAuthMode("login");
      await loadRequests(undefined, result.access_token);
    } catch (authError) {
      setError(
        authError instanceof Error
          ? authError.message
          : "Could not authenticate this admin account.",
      );
    } finally {
      setIsAuthenticating(false);
    }
  }

  async function logoutAdmin() {
    setIsLoading(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/logout`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
    } catch (logoutError) {
      setError(
        logoutError instanceof Error
          ? `Signed out in this browser; server session revocation failed: ${logoutError.message}`
          : "Signed out in this browser; the server session could not be revoked.",
      );
    } finally {
      window.sessionStorage.removeItem("formulaflow-admin-session");
        setToken("");
      setAdminAccount(null);
      setRequests([]);
      setPickupToken("");
      setPickupTokenExpiresAt("");
      setDashboard(null);
      setHasLoaded(false);
      setIsLoading(false);
    }
  }

  async function changeAdminPassword(event) {
    event.preventDefault();
    if (newAdminPassword !== confirmAdminPassword) {
      setError("New password and confirmation do not match.");
      return;
    }

    setIsChangingPassword(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/change-password`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: currentAdminPassword,
          new_password: newAdminPassword,
        }),
      });
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const result = await response.json();
      window.sessionStorage.setItem(
        "formulaflow-admin-session",
        result.access_token,
      );
      setToken(result.access_token);
      setAdminAccount({ id: result.id, email: result.email, role: result.role });
      setCurrentAdminPassword("");
      setNewAdminPassword("");
      setConfirmAdminPassword("");
      setNotice("Password updated. Other active sessions have been signed out.");
      await loadRequestRows(result.access_token);
      await loadOperationsData(() => true, result.access_token);
    } catch (passwordError) {
      setError(
        passwordError instanceof Error
          ? passwordError.message
          : "Could not update the admin password.",
      );
    } finally {
      setIsChangingPassword(false);
    }
  }

  function sessionHeaders(sessionToken) {
    return { Authorization: `Bearer ${sessionToken}` };
  }

  async function loadOperationsData(
    isCurrent = () => true,
    sessionToken = token,
  ) {
    const [dashboardResponse, anomaliesResponse, vendorsResponse] =
      await Promise.all([
        fetch(`${API_BASE_URL}/api/admin/dashboard`, {
          headers: sessionHeaders(sessionToken),
        }),
        fetch(`${API_BASE_URL}/api/admin/anomalies`, {
          headers: sessionHeaders(sessionToken),
        }),
        fetch(`${API_BASE_URL}/api/admin/vendors`, {
          headers: sessionHeaders(sessionToken),
        }),
      ]);
    for (const response of [
      dashboardResponse,
      anomaliesResponse,
      vendorsResponse,
    ]) {
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
    }
    const [dashboardData, anomalyData, vendorData] = await Promise.all([
      dashboardResponse.json(),
      anomaliesResponse.json(),
      vendorsResponse.json(),
    ]);
    if (!isCurrent()) {
      return;
    }
    setDashboard(dashboardData);
    setAnomalies(anomalyData);
    setVendors(vendorData);
  }

  async function loadRequests(event, sessionToken = token) {
    event?.preventDefault();
    setIsLoading(true);
    setError("");
    setNotice("");
    try {
      await refreshAdminView(sessionToken);
      setHasLoaded(true);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not load operations data.",
      );
      setHasLoaded(false);
    } finally {
      setIsLoading(false);
    }
  }

  async function loadRequestRows(sessionToken = token) {
    const response = await fetch(`${API_BASE_URL}/api/admin/requests`, {
      headers: sessionHeaders(sessionToken),
    });
    if (!response.ok) {
      throw new Error(await getErrorMessage(response));
    }
    setRequests(await response.json());
  }

  async function refreshAdminView(sessionToken = token) {
    await Promise.all([
      loadRequestRows(sessionToken),
      loadOperationsData(() => true, sessionToken),
    ]);
  }

  function setRequestActionPending(requestId, action, isPending) {
    const key = `${requestId}:${action}`;
    setPendingRequestActions((current) => {
      const next = new Set(current);
      if (isPending) {
        next.add(key);
      } else {
        next.delete(key);
      }
      return next;
    });
  }

  async function rematchRequest(requestId) {
    setError("");
    setRequestActionPending(requestId, "match", true);
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/requests/${requestId}/match`,
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({
            merchant_id: Number(merchantSelections[requestId]),
          }),
        },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const updatedRequest = await response.json();
      setRequests((current) =>
        current.map((request) =>
          request.id === requestId ? { ...request, ...updatedRequest } : request,
        ),
      );
      setMerchantSelections((current) => {
        const next = { ...current };
        delete next[requestId];
        return next;
      });
      setNotice(`Request #${requestId} matched with ${updatedRequest.matched_vendor}.`);
      await refreshAdminView();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not match this request to a merchant.",
      );
    } finally {
      setRequestActionPending(requestId, "match", false);
    }
  }

  async function submitDecision(event) {
    event.preventDefault();
    if (!selectedDecision) {
      return;
    }
    setIsLoading(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/requests/${selectedDecision.request.id}/decision`,
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({
            decision: selectedDecision.decision,
            note: reviewNote,
          }),
        },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const updatedRequest = await response.json();
      setRequests((current) =>
        current.map((request) =>
          request.id === updatedRequest.id
            ? { ...request, ...updatedRequest }
            : request,
        ),
      );
      setNotice(
        selectedDecision.decision === "approve"
          ? `Request #${updatedRequest.id} approved. Select a merchant to continue.`
          : `Request #${updatedRequest.id} rejected and recorded.`,
      );
      setSelectedDecision(null);
      setReviewNote("");
      await loadOperationsData();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not record this request decision.",
      );
    } finally {
      setIsLoading(false);
    }
  }

  async function showRequestHistory(request) {
    setError("");
    setRequestActionPending(request.id, "history", true);
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/requests/${request.id}/history`,
        { headers: authHeaders() },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      setHistoryRequest(request);
      setRequestHistory(await response.json());
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not load the request audit history.",
      );
    } finally {
      setRequestActionPending(request.id, "history", false);
    }
  }

  async function submitPayout() {
    if (!selectedRequest) {
      return;
    }
    setIsPaying(true);
    setError("");
    setNotice("");
    setPickupToken("");
    setPickupTokenExpiresAt("");
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/approve/${selectedRequest.id}`,
        {
          method: "POST",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({ containers: purchaseContainers }),
        },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const result = await response.json();
      setRequests((current) =>
        current.map((request) =>
          request.id === result.request_id
            ? {
                ...request,
                status: result.status,
                lifecycle_status: result.lifecycle_status,
                payout_status: result.payout_status,
                payout_batch_id: result.payout_batch_id,
                payout_mode: result.payout_mode,
              }
            : request,
        ),
      );
      setPickupToken(result.pickup_token || "");
      setNotice(
        result.payout_mode === "SIMULATED"
          ? `SIMULATED: USD ${Number(result.amount).toFixed(2)} staged for ${selectedRequest.matched_vendor}; no money was sent.`
          : `USD ${Number(result.amount).toFixed(2)} payout submitted to ${selectedRequest.matched_vendor}.`,
      );
      setSelectedRequest(null);
      await refreshAdminView();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not initiate the payout. Verify PayPal before retrying.",
      );
    } finally {
      setIsPaying(false);
    }
  }

  async function confirmSimulatedPayout(request) {
    setError("");
    setNotice("");
    setIsPaying(true);
    setRequestActionPending(request.id, "confirm", true);
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/payout/${request.id}/simulate-confirmation`,
        { method: "POST", headers: authHeaders() },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      await refreshAdminView();
      setNotice(
        `SIMULATED confirmation recorded for request #${request.id}; no PayPal transfer occurred. Retrieve its pickup token for delivery.`,
      );
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not record the simulated payout confirmation.",
      );
    } finally {
      setIsPaying(false);
      setRequestActionPending(request.id, "confirm", false);
    }
  }

  async function retrievePickupToken(request) {
    setError("");
    setNotice("");
    setPickupToken("");
    setPickupTokenExpiresAt("");
    setRequestActionPending(request.id, "token", true);
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/requests/${request.id}/pickup-token/retrieve`,
        { method: "POST", headers: authHeaders() },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const result = await response.json();
      setPickupToken(result.token);
      setPickupTokenExpiresAt(result.expires_at);
      setNotice(`Pickup token retrieved for request #${request.id}.`);
      await refreshAdminView();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not retrieve the pickup token.",
      );
    } finally {
      setRequestActionPending(request.id, "token", false);
    }
  }

  async function refreshPayout(request) {
    setError("");
    setNotice("");
    setRequestActionPending(request.id, "refresh", true);
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/payout/${request.id}/refresh`,
        {
          method: "POST",
          headers: authHeaders(),
        },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const result = await response.json();
      setRequests((current) =>
        current.map((item) =>
          item.id === result.request_id
            ? {
                ...item,
                status: result.status,
                lifecycle_status: result.lifecycle_status,
                payout_status: result.payout_status,
                payout_batch_id: result.payout_batch_id,
                payout_mode: result.payout_mode,
              }
            : item,
        ),
      );
      setNotice(
        result.payout_status === "SUCCESS"
          ? `PayPal confirmed the payout. Request #${request.id} is paid and ready for pickup. Retrieve its pickup token for delivery.`
          : `PayPal payout status: ${result.payout_status.toLowerCase()}.`,
      );
      await refreshAdminView();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not check the PayPal payout status.",
      );
    } finally {
      setRequestActionPending(request.id, "refresh", false);
    }
  }

  async function submitAgentQuestion(event) {
    event.preventDefault();
    setIsQuerying(true);
    setAgentAnswer("");
    setError("");
    try {
      const response = await fetch(`${API_BASE_URL}/api/admin/agent/query`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ question: agentQuestion }),
      });
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const result = await response.json();
      setAgentAnswer(result.answer);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "The operations assistant could not answer that query.",
      );
    } finally {
      setIsQuerying(false);
    }
  }

  async function saveMerchant(event) {
    event.preventDefault();
    setIsSavingMerchant(true);
    setNotice("");
    setError("");
    try {
      const response = await fetch(
        `${API_BASE_URL}/api/admin/vendors/${encodeURIComponent(merchantForm.name)}`,
        {
          method: "PUT",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify(merchantForm),
        },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      setMerchantForm({
        name: "",
        location: "",
        county: "",
        paypal_email: "",
        price_per_container_usd: "",
      });
      setNotice("Supplier saved. Approved cases can now be matched manually.");
      const requestsResponse = await fetch(`${API_BASE_URL}/api/admin/requests`, {
        headers: authHeaders(),
      });
      if (!requestsResponse.ok) {
        throw new Error(await getErrorMessage(requestsResponse));
      }
      setRequests(await requestsResponse.json());
      await loadOperationsData();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Could not save this supplier.",
      );
    } finally {
      setIsSavingMerchant(false);
    }
  }

  async function saveSupplierCell(event) {
    const { data: supplier, colDef, newValue, oldValue } = event;
    const field = colDef.field;
    if (!field || newValue === oldValue) {
      return;
    }

    setSavingSupplierIds((current) => new Set(current).add(supplier.id));
    setError("");
    setNotice("");
    try {
      const previousName = field === "name" ? oldValue : supplier.name;
      const response = await fetch(
        `${API_BASE_URL}/api/admin/vendors/${encodeURIComponent(previousName)}`,
        {
          method: "PUT",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({
            name: supplier.name,
            location: supplier.location,
            county: supplier.county,
            paypal_email: supplier.paypal_email,
            price_per_container_usd:
              supplier.price_per_container_usd == null ||
              supplier.price_per_container_usd === ""
                ? null
                : Number(supplier.price_per_container_usd),
          }),
        },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const updatedSupplier = await response.json();
      setVendors((current) =>
        current.map((item) =>
          item.id === updatedSupplier.id ? updatedSupplier : item,
        ),
      );
      setNotice(`${updatedSupplier.name} updated.`);
    } catch (saveError) {
      supplier[field] = oldValue;
      event.api.refreshCells({
        rowNodes: [event.node],
        columns: [field],
        force: true,
      });
      setVendors((current) =>
        current.map((item) =>
          item.id === supplier.id ? { ...item, [field]: oldValue } : item,
        ),
      );
      setError(
        saveError instanceof Error
          ? `Supplier changes were not saved: ${saveError.message}`
          : "Supplier changes were not saved.",
      );
    } finally {
      setSavingSupplierIds((current) => {
        const next = new Set(current);
        next.delete(supplier.id);
        return next;
      });
    }
  }

  async function saveRequestCell(event) {
    const { data: request, colDef, newValue, oldValue } = event;
    const field = colDef.field;
    if (!["county", "birth_month"].includes(field) || newValue === oldValue) {
      return;
    }

    setSavingRequestIds((current) => new Set(current).add(request.id));
    setError("");
    setNotice("");
    try {
      const value = field === "birth_month" ? birthMonthValue(newValue) : newValue;
      const response = await fetch(
        `${API_BASE_URL}/api/admin/requests/${request.id}`,
        {
          method: "PATCH",
          headers: { ...authHeaders(), "Content-Type": "application/json" },
          body: JSON.stringify({ [field]: value }),
        },
      );
      if (!response.ok) {
        throw new Error(await getErrorMessage(response));
      }
      const updatedRequest = await response.json();
      setRequests((current) =>
        current.map((item) =>
          item.id === updatedRequest.id ? { ...item, ...updatedRequest } : item,
        ),
      );
      setNotice(
        field === "birth_month"
          ? `Birth month recorded as ${formatBirthMonth(updatedRequest.birth_month)}. This beneficiary is now included in monthly age eligibility tracking.`
          : `County updated to ${updatedRequest.county}.`,
      );
    } catch (saveError) {
      request[field] = oldValue;
      event.api.refreshCells({
        rowNodes: [event.node],
        columns: [field],
        force: true,
      });
      setRequests((current) =>
        current.map((item) =>
          item.id === request.id ? { ...item, [field]: oldValue } : item,
        ),
      );
      setError(
        saveError instanceof Error
          ? `Request details were not saved: ${saveError.message}`
          : "Request details were not saved.",
      );
    } finally {
      setSavingRequestIds((current) => {
        const next = new Set(current);
        next.delete(request.id);
        return next;
      });
    }
  }

  const supplierColumnDefs = useMemo(
    () => [
      {
        field: "name",
        headerName: "Supplier",
        minWidth: 160,
        editable: ({ data }) => !savingSupplierIds.has(data.id),
      },
      {
        field: "location",
        headerName: "Location",
        minWidth: 180,
        editable: ({ data }) => !savingSupplierIds.has(data.id),
      },
      {
        field: "county",
        headerName: "County",
        minWidth: 160,
        editable: ({ data }) => !savingSupplierIds.has(data.id),
        valueFormatter: ({ value }) => value || "Not recorded",
      },
      {
        field: "paypal_email",
        headerName: "PayPal email",
        minWidth: 210,
        editable: ({ data }) => !savingSupplierIds.has(data.id),
      },
      {
        field: "price_per_container_usd",
        headerName: "Price / container (USD)",
        minWidth: 190,
        editable: ({ data }) => !savingSupplierIds.has(data.id),
        cellEditor: "agNumberCellEditor",
        cellEditorParams: { min: 0.01 },
        valueParser: ({ newValue }) => {
          const value = Number(newValue);
          return Number.isFinite(value) ? value : newValue;
        },
        valueFormatter: ({ value }) =>
          value == null ? "—" : Number(value).toFixed(2),
      },
      { field: "currency", headerName: "Currency", width: 110, editable: false },
    ],
    [savingSupplierIds],
  );

  const requestStatusRenderer = (params) => {
    const status = params.value || "Pending";
    const className = {
      "Pending Verification": "status-chip muted",
      Approved: "status-chip ok",
      Rejected: "status-chip danger",
      "Ready for pickup": "status-chip info",
      Collected: "status-chip success",
    }[status] || "status-chip neutral";

    return <span className={className}>{status}</span>;
  };

  const regionRenderer = (params) => (
    <span className="region-badge">{params.value || "Unassigned"}</span>
  );

  const merchantRenderer = (params) => (
    <span className="merchant-badge">{params.value || "Not matched"}</span>
  );

  const paymentRenderer = (params) => {
    const value = params.value;
    if (!value) return <span className="empty-cell">—</span>;

    const className = value === "SUCCESS" || value === "COMPLETED"
      ? "mini-status ok"
      : value === "FAILED" || value === "DENIED"
        ? "mini-status danger"
        : value === "PROCESSING" || value === "PENDING"
          ? "mini-status warning"
          : "mini-status neutral";

    return <span className={className}>{value}</span>;
  };

  const columnDefs = useMemo(
    () => [
      {
        field: "recipient_name",
        headerName: "Beneficiary",
        minWidth: 180,
        cellRenderer: (params) => (
          <div className="beneficiary-cell">
            <span className="beneficiary-avatar">{params.value?.charAt(0)?.toUpperCase() || "?"}</span>
            <div>
              <div className="beneficiary-name">{params.value || "Unknown"}</div>
              <small>#{params.data?.id || "—"}</small>
            </div>
          </div>
        ),
      },
      {
        field: "location",
        headerName: "Region",
        minWidth: 180,
        cellRenderer: regionRenderer,
      },
      {
        field: "county",
        headerName: "County",
        minWidth: 150,
        headerTooltip: "Double-click a county cell to update the requester's county.",
        editable: ({ data }) => !savingRequestIds.has(data.id),
        cellClass: "editable-county-cell",
        valueFormatter: ({ value }) => value || "Not recorded",
      },
      {
        field: "birth_month",
        headerName: "Birth month",
        headerTooltip: "For legacy beneficiaries, double-click and enter YYYY-MM. Only month and year are stored.",
        minWidth: 140,
        editable: ({ data }) =>
          !data.continuity_id && !savingRequestIds.has(data.id),
        cellClass: "editable-county-cell",
        valueFormatter: ({ value }) => formatBirthMonth(value),
      },
      {
        field: "support_month",
        headerName: "Formula month",
        headerTooltip: "The month this request's formula support is for.",
        minWidth: 140,
        valueFormatter: ({ value }) => formatBirthMonth(value),
      },
      {
        field: "containers_per_month",
        headerName: "Packages / month",
        minWidth: 155,
      },
      {
        field: "total_cost_usd",
        headerName: "Value (USD)",
        minWidth: 125,
        valueFormatter: ({ value }) =>
          value == null ? "—" : Number(value).toFixed(2),
      },
      {
        field: "matched_vendor",
        headerName: "Merchant",
        minWidth: 190,
        cellRenderer: merchantRenderer,
        valueFormatter: ({ value }) => value || "Not matched",
      },
      {
        field: "status",
        headerName: "Request status",
        minWidth: 170,
        cellRenderer: requestStatusRenderer,
      },
      {
        field: "payout_status",
        headerName: "Payment status",
        minWidth: 170,
        cellRenderer: paymentRenderer,
        valueFormatter: ({ value }) => value || "—",
      },
      {
        field: "lifecycle_status",
        headerName: "Lifecycle",
        minWidth: 180,
        valueFormatter: ({ value }) =>
          value
            ? value
                .split("_")
                .map((part) => part[0].toUpperCase() + part.slice(1))
                .join(" ")
            : "—",
      },
      {
        colId: "actions",
        headerName: "Actions",
        width: 390,
        minWidth: 390,
        maxWidth: 460,
        flex: 0,
        sortable: false,
        filter: false,
        cellClass: "request-actions-cell",
        cellRenderer: ({ data }) => {
          if (data.status === "Pending Verification") {
            return (
              <div className="request-actions">
                <button
                  className="action-button"
                  onClick={() =>
                    setSelectedDecision({ request: data, decision: "approve" })
                  }
                  type="button"
                >
                  Approve
                </button>
                <button
                  className="reject-button"
                  onClick={() =>
                    setSelectedDecision({ request: data, decision: "reject" })
                  }
                  type="button"
                >
                  Reject
                </button>
                <button
                  className="secondary-button compact-button"
                  disabled={pendingRequestActions.has(`${data.id}:history`)}
                  onClick={() => showRequestHistory(data)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:history`)
                    ? "Loading..."
                    : "History"}
                </button>
              </div>
            );
          }
          if (
            data.status === "Approved" &&
            (!data.matched_vendor ||
              !data.total_cost_usd ||
              data.payout_status === "FAILED")
          ) {
            const currentMerchant = vendors.find(
              (vendor) => vendor.name === data.matched_vendor,
            );
            const selectedMerchant =
              merchantSelections[data.id] ?? (currentMerchant ? String(currentMerchant.id) : "");
            return (
              <div className="request-actions match-actions">
                <select
                  aria-label={`Select merchant for ${data.recipient_name}`}
                  onChange={(event) =>
                    setMerchantSelections((current) => ({
                      ...current,
                      [data.id]: event.target.value,
                    }))
                  }
                  value={selectedMerchant}
                >
                  <option value="">Select merchant</option>
                  {vendors.map((vendor) => (
                    <option key={vendor.id} value={vendor.id}>
                      {vendor.name} · {vendor.location}
                    </option>
                  ))}
                </select>
                <button
                  className="action-button"
                  disabled={
                    !selectedMerchant ||
                    !vendors.length ||
                    pendingRequestActions.has(`${data.id}:match`)
                  }
                  onClick={() => rematchRequest(data.id)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:match`)
                    ? "Matching..."
                    : "Match merchant"}
                </button>
              </div>
            );
          }
          if (data.lifecycle_status === "ready_for_pickup") {
            const expiresAt = data.pickup_token_expires_at
              ? Date.parse(data.pickup_token_expires_at)
              : NaN;
            const tokenAction =
              data.pickup_token_retrieved_at || Number.isNaN(expiresAt)
                ? "No retrievable token"
                : expiresAt <= Date.now()
                  ? "Token expired"
                  : null;
            return (
              <div className="request-actions">
                {tokenAction ? (
                  <span className="payout-state">{tokenAction}</span>
                ) : (
                  <button
                    className="action-button"
                    disabled={pendingRequestActions.has(`${data.id}:token`)}
                    onClick={() => retrievePickupToken(data)}
                    type="button"
                  >
                    {pendingRequestActions.has(`${data.id}:token`)
                      ? "Retrieving..."
                      : "Retrieve token"}
                  </button>
                )}
                <button
                  className="secondary-button compact-button"
                  disabled={pendingRequestActions.has(`${data.id}:history`)}
                  onClick={() => showRequestHistory(data)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:history`)
                    ? "Loading..."
                    : "History"}
                </button>
              </div>
            );
          }
          if (
            data.payout_mode === "SIMULATED" &&
            data.payout_status === "SIMULATED_PENDING"
          ) {
            return (
              <div className="request-actions">
                <button
                  className="action-button"
                  disabled={
                    isPaying ||
                    pendingRequestActions.has(`${data.id}:confirm`)
                  }
                  onClick={() => confirmSimulatedPayout(data)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:confirm`)
                    ? "Confirming..."
                    : "Confirm payout"}
                </button>
                <button
                  className="secondary-button compact-button"
                  disabled={pendingRequestActions.has(`${data.id}:history`)}
                  onClick={() => showRequestHistory(data)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:history`)
                    ? "Loading..."
                    : "History"}
                </button>
              </div>
            );
          }
          if (
            data.lifecycle_status === "payout_pending" &&
            data.payout_mode === "PAYPAL" &&
            data.payout_status !== "FAILED"
          ) {
            const payoutMessage =
              data.payout_status === "SUCCESS"
                ? "PayPal reports success; confirmation pending"
                : data.payout_status === "UNCLAIMED"
                  ? "Unclaimed by recipient"
                  : `PayPal status: ${data.payout_status.toLowerCase()}`;
            return (
              <div className="request-actions">
                <span className="payout-state">{payoutMessage}</span>
                <button
                  className="action-button"
                  disabled={pendingRequestActions.has(`${data.id}:refresh`)}
                  onClick={() => refreshPayout(data)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:refresh`)
                    ? "Checking..."
                    : data.payout_status === "SUCCESS"
                    ? "Verify payout"
                    : "Check PayPal status"}
                </button>
                <button
                  className="secondary-button compact-button"
                  disabled={pendingRequestActions.has(`${data.id}:history`)}
                  onClick={() => showRequestHistory(data)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:history`)
                    ? "Loading..."
                    : "History"}
                </button>
              </div>
            );
          }
          if (data.status === "Approved" && data.matched_vendor && data.total_cost_usd) {
            return (
              <div className="request-actions">
                <button
                  className="action-button"
                  onClick={() => {
                    setPurchaseContainers(
                      recommendedContainerCount(
                        data,
                        dashboard?.available_pool_usd,
                      ),
                    );
                    setSelectedRequest(data);
                  }}
                  type="button"
                >
                  Confirm &amp; Pay
                </button>
                <button
                  className="secondary-button compact-button"
                  disabled={pendingRequestActions.has(`${data.id}:history`)}
                  onClick={() => showRequestHistory(data)}
                  type="button"
                >
                  {pendingRequestActions.has(`${data.id}:history`)
                    ? "Loading..."
                    : "History"}
                </button>
              </div>
            );
          }
          return (
            <div className="request-actions">
              <span className="payout-state">{data.status}</span>
              <button
                className="secondary-button compact-button"
                disabled={pendingRequestActions.has(`${data.id}:history`)}
                onClick={() => showRequestHistory(data)}
                type="button"
              >
                {pendingRequestActions.has(`${data.id}:history`)
                  ? "Loading..."
                  : "History"}
              </button>
            </div>
          );
        },
      },
    ],
    [
      vendors,
      merchantSelections,
      requests,
      selectedLocation,
      isPaying,
      savingRequestIds,
      dashboard?.available_pool_usd,
      pendingRequestActions,
    ],
  );

  const visibleRequests = selectedLocation
    ? requests.filter((request) => request.location === selectedLocation)
    : requests;

  const filteredRequests = useMemo(() => {
    const query = requestSearch.trim().toLowerCase();
    if (!query) return visibleRequests;
    return visibleRequests.filter((request) =>
      [
        request.recipient_name,
        request.location,
        request.matched_vendor,
        String(request.id),
        request.status,
      ]
        .join(" ")
        .toLowerCase()
        .includes(query),
    );
  }, [requestSearch, visibleRequests]);
  const requestLocations = useMemo(
    () => [...new Set(requests.map((request) => request.location).filter(Boolean))].sort(),
    [requests],
  );

  if (!token) {
    const registering = authMode === "register";
    return (
      <section className="content-card admin-login-card">
        <div className="eyebrow">Admin account</div>
        <h1>{registering ? "Create the first admin account" : "Welcome back"}</h1>
        <p className="intro">
          {registering
            ? "The first account is the sole admin. Registration closes as soon as it is created."
            : "Log in with your admin email and password to manage requests and merchant payouts."}
        </p>
        {error && (
          <div className="notice error" role="alert">
            {error}
          </div>
        )}
        {notice && (
          <div className="notice success" role="status">
            {notice}
          </div>
        )}
        <form className="request-form" onSubmit={submitAuthentication}>
          <label>
            Admin email
            <input
              autoComplete="email"
              onChange={(event) => setAuthEmail(event.target.value)}
              required
              type="email"
              value={authEmail}
            />
          </label>
          <label>
            Password
            <input
              autoComplete={registering ? "new-password" : "current-password"}
              minLength={registering ? 12 : 1}
              maxLength={128}
              onChange={(event) => setAuthPassword(event.target.value)}
              required
              type="password"
              value={authPassword}
            />
          </label>
          <button
            className="primary-button"
            disabled={isAuthenticating}
            type="submit"
          >
            {isAuthenticating
              ? registering
                ? "Creating account..."
                : "Logging in..."
              : registering
                ? "Create admin account"
                : "Log in"}
          </button>
        </form>
        {registrationOpen && (
          <button
            className="auth-mode-button"
            onClick={() => {
              setAuthMode(registering ? "login" : "register");
              setError("");
            }}
            type="button"
          >
            {registering
              ? "Already created the first account? Log in"
              : "First time here? Create the initial admin account"}
          </button>
        )}
        {!registrationOpen && !registering && (
          <p className="integration-note">
            Forgot your password? Ask the API operator to run{" "}
            <code>python -m backend.reset_admin_password</code> in the
            backend environment.
          </p>
        )}
      </section>
    );
  }

  const payoutStatusCounts = dashboard?.payout_status_counts || [];
  const totalPayoutAttempts =
    payoutStatusCounts.reduce(
      (total, payout) => total + Number(payout.count || 0),
      0,
    ) || 0;

  return (
    <section className="dashboard">
      <div className="dashboard-heading">
        <div>
          <div className="eyebrow">Internal operations</div>
          <h1>FormulaFlow dashboard</h1>
          <p className="intro">
            Review and decide on cases, then assign a merchant and pay from the pool.
          </p>
          <div
            className={`dashboard-data-status ${
              dashboard?.payout_mode === "SIMULATED" ? "simulated" : "sandbox"
            }`}
            role="status"
          >
            <span aria-hidden="true" />
            {dashboard?.payout_mode === "SIMULATED"
              ? "Payouts simulated · PayPal Sandbox"
              : "Environment · PayPal Sandbox"}
          </div>
        </div>
        <div className="admin-session-bar">
          <span>
            Signed in as <strong>{adminAccount?.email || "Admin"}</strong>
          </span>
          <button
            className="secondary-button"
            disabled={isLoading}
            onClick={logoutAdmin}
            type="button"
          >
            Sign out
          </button>
        </div>
      </div>

      <details className="admin-access">
        <summary>Change admin password</summary>
        <form className="request-form admin-password-form" onSubmit={changeAdminPassword}>
          <label>
            Current password
            <input
              autoComplete="current-password"
              maxLength={128}
              onChange={(event) => setCurrentAdminPassword(event.target.value)}
              required
              type="password"
              value={currentAdminPassword}
            />
          </label>
          <label>
            New password
            <input
              autoComplete="new-password"
              minLength={12}
              maxLength={128}
              onChange={(event) => setNewAdminPassword(event.target.value)}
              required
              type="password"
              value={newAdminPassword}
            />
          </label>
          <label>
            Confirm new password
            <input
              autoComplete="new-password"
              minLength={12}
              maxLength={128}
              onChange={(event) => setConfirmAdminPassword(event.target.value)}
              required
              type="password"
              value={confirmAdminPassword}
            />
          </label>
          <button
            className="primary-button"
            disabled={isChangingPassword}
            type="submit"
          >
            {isChangingPassword ? "Updating password..." : "Update password"}
          </button>
        </form>
      </details>

      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="notice success" role="status">
          {notice}
          {pickupToken && (
            <div className="pickup-token">
              One-time pickup token: <code>{pickupToken}</code>
              {pickupTokenExpiresAt && (
                <span>
                  {" "}
                  Expires {new Date(pickupTokenExpiresAt).toLocaleString()}.
                </span>
              )}
            </div>
          )}
        </div>
      )}

      {dashboard && (
        <>
          <div className="dashboard-section-heading">
            <div>
              <div className="eyebrow">Funding</div>
              <h2>Donations and payouts</h2>
            </div>
            <span>All-time totals · refresh every 30 seconds</span>
          </div>
          <div className="dashboard-metrics funding-metrics">
            <article className="metric-card metric-card-green">
              <span>Remaining program funds</span>
              <strong>{formatDashboardUsd(dashboard.available_pool_usd)}</strong>
              <small>Ledger balance · not PayPal wallet balance</small>
            </article>
            <article className="metric-card metric-card-teal">
              <span>PayPal donations received</span>
              <strong>{formatDashboardUsd(dashboard.donations_total_usd)}</strong>
              <small>Captured donations · all time</small>
            </article>
            <article className="metric-card metric-card-green">
              <span>Supplier payouts sent · PayPal confirmed</span>
              <strong>{formatDashboardUsd(dashboard.paypal_payouts_sent_usd)}</strong>
              <small>Confirmed supplier payments · all time</small>
            </article>
            <article className="metric-card metric-card-violet">
              <span>Supplier payouts awaiting confirmation</span>
              <strong>{formatDashboardUsd(dashboard.paypal_payouts_pending_usd)}</strong>
              <small>Not yet confirmed as sent</small>
            </article>
          </div>
          <p className="integration-note dashboard-funds-note">
            PayPal figures reflect this app’s Sandbox donation captures and
            supplier payout records. Remaining program funds are the tracked
            FormulaFlow ledger balance; they are not a live PayPal account
            balance.
          </p>

          <div className="dashboard-section-heading dashboard-pipeline-heading">
            <div>
              <div className="eyebrow">Request pipeline</div>
              <h2>Cases and family support</h2>
            </div>
            <span>Current request status</span>
          </div>
          <div className="dashboard-metrics pipeline-metrics">
            <article className="metric-card metric-card-gold">
              <span>Requests needing review</span>
              <strong>{dashboard.pending_verification_count}</strong>
              <small>Awaiting an admin decision</small>
            </article>
            <article className="metric-card metric-card-sky">
              <span>Approved awaiting match</span>
              <strong>{dashboard.approved_count}</strong>
              <small>Ready for a supplier assignment</small>
            </article>
            <article className="metric-card metric-card-gold">
              <span>Payouts awaiting confirmation</span>
              <strong>{dashboard.payout_pending_count ?? 0}</strong>
              <small>Do not treat as paid until verified</small>
            </article>
            <article className="metric-card metric-card-teal">
              <span>Requests marked paid</span>
              <strong>{dashboard.paid_count ?? 0}</strong>
              <small>Recorded as paid in the program ledger</small>
            </article>
            <article className="metric-card metric-card-lime">
              <span>Families supported · PayPal confirmed</span>
              <strong>{dashboard.families_supported_count ?? 0}</strong>
              <small>Successful PayPal payouts only</small>
            </article>
            <article className="metric-card metric-card-rose">
              <span>Rejected</span>
              <strong>{dashboard.rejected_count}</strong>
              <small>Cases closed without funding</small>
            </article>
          </div>

          <div className="age-overview-grid">
            <article className="chart-card">
              <div className="eyebrow">Monthly continuity</div>
              <h2>Beneficiary age profile</h2>
              <p className="integration-note">
                Age is calculated from birth month at the start of this month.
                Monthly support is not scheduled from age 24 onward.
              </p>
              <div className="age-cohort-list">
                {(dashboard.age_cohorts || []).map((cohort) => {
                  const total = (dashboard.age_cohorts || []).reduce(
                    (sum, entry) => sum + Number(entry.count || 0),
                    0,
                  );
                  const percentage = total
                    ? (Number(cohort.count || 0) / total) * 100
                    : 0;
                  return (
                    <div className="age-cohort-row" key={cohort.label}>
                      <div className="age-cohort-meta">
                        <span>{cohort.label}</span>
                        <strong>{cohort.count}</strong>
                      </div>
                      <div className="age-cohort-track">
                        <span style={{ width: `${percentage}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            </article>

            <article className="chart-card">
              <div className="eyebrow">24-month cutoff</div>
              <h2>Approaching end of support</h2>
              <p className="integration-note">
                Final eligible month is shown for each child aged 18–23 months.
              </p>
              {dashboard.approaching_age_limit?.length ? (
                <div className="age-cutoff-list">
                  {dashboard.approaching_age_limit.map((beneficiary) => (
                    <div
                      className="age-cutoff-row"
                      key={beneficiary.request_id}
                    >
                      <div>
                        <strong>{beneficiary.recipient_name}</strong>
                        <span>
                          {beneficiary.location} · {beneficiary.age_months} months
                        </span>
                      </div>
                      <div className="age-cutoff-deadline">
                        <strong>
                          {beneficiary.months_remaining}{" "}
                          {beneficiary.months_remaining === 1 ? "month" : "months"}
                        </strong>
                        <span>
                          Last month: {formatBirthMonth(beneficiary.final_eligible_month)}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="age-cutoff-empty">
                  No recorded beneficiaries are within six months of the cutoff.
                </p>
              )}
            </article>
          </div>

          <div className="dashboard-insight-grid">
            <article className="chart-card">
              <div className="eyebrow">Aid pipeline</div>
              <h2>Current request stages</h2>
              <div className="pipeline-rows">
                {[
                  { label: "Submitted", value: dashboard.submitted_count ?? 0, tone: "emerald" },
                  { label: "Approved · awaiting match", value: dashboard.approved_count ?? 0, tone: "sky" },
                  { label: "Payout awaiting confirmation", value: dashboard.payout_pending_count ?? 0, tone: "amber" },
                  { label: "Ready for pickup", value: dashboard.ready_for_pickup_count ?? 0, tone: "violet" },
                  { label: "Collected · families supported", value: dashboard.collected_count ?? 0, tone: "emerald" },
                ].map((stage) => (
                  <div className="pipeline-row" key={stage.label}>
                    <div className="pipeline-meta">
                      <span>{stage.label}</span>
                      <strong>{stage.value ?? 0}</strong>
                    </div>
                    <div className="pipeline-track">
                      <span
                        className={`pipeline-fill ${stage.tone}`}
                        style={{
                          width: `${
                            dashboard.total_requests_count
                              ? (Number(stage.value || 0) /
                                  dashboard.total_requests_count) *
                                100
                              : 0
                          }%`,
                        }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </article>

            <article className="chart-card">
              <div className="eyebrow">Payout reconciliation</div>
              <h2>Recorded payout outcomes</h2>
              <p className="integration-note">
                A submission response marked SUCCESS remains unconfirmed until
                PayPal status refresh or a verified webhook completes it.
              </p>
              {payoutStatusCounts.length ? (
                <div className="region-bars">
                  {payoutStatusCounts.map((entry) => (
                    <div
                      className="region-bar"
                      key={`${entry.payout_mode}-${entry.status}`}
                    >
                      <div className="region-label-row">
                        <span>
                          {entry.status.replaceAll("_", " ")} ·{" "}
                          {entry.payout_mode === "SIMULATED"
                            ? "Simulated"
                            : "PayPal"}
                        </span>
                        <strong>{entry.count}</strong>
                      </div>
                      <div className="region-track">
                        <span
                          style={{
                            width: `${
                              totalPayoutAttempts
                                ? (Number(entry.count) /
                                    totalPayoutAttempts) *
                                  100
                                : 0
                            }%`,
                          }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="integration-note">
                  No payout transactions have been recorded yet.
                </p>
              )}
            </article>
          </div>
        </>
      )}
      {dashboard?.payout_mode === "SIMULATED" && (
        <div className="notice warning" role="status">
          SIMULATED PAYOUT MODE: submissions do not send money to PayPal. An
          admin must explicitly confirm each simulated payout.
        </div>
      )}

      {dashboard?.requests_by_location?.length > 0 && (
        <section className="insight-card">
          <div className="insight-heading">
            <div>
              <div className="eyebrow">Cross-filter</div>
              <h2>Requests by location</h2>
            </div>
            {selectedLocation && (
              <button
                className="secondary-button"
                onClick={() => setSelectedLocation("")}
                type="button"
              >
                Clear filter
              </button>
            )}
          </div>
          <div className="location-chart">
            {dashboard.requests_by_location.map((item) => (
              <button
                aria-pressed={selectedLocation === item.location}
                className={
                  selectedLocation === item.location
                    ? "location-bar selected"
                    : "location-bar"
                }
                key={item.location}
                onClick={() =>
                  setSelectedLocation((current) =>
                    current === item.location ? "" : item.location,
                  )
                }
                type="button"
              >
                <span>{item.location}</span>
                <strong>{item.count}</strong>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="insight-card">
        <div className="eyebrow">Operations assistant</div>
        <h2>Ask about local requests</h2>
        <form className="agent-query" onSubmit={submitAgentQuestion}>
          <input
            aria-label="Ask the operations assistant"
            onChange={(event) => setAgentQuestion(event.target.value)}
            placeholder="Try: How many pending requests are in Kibera?"
            required
            value={agentQuestion}
          />
          <button className="primary-button" disabled={isQuerying} type="submit">
            {isQuerying ? "Checking..." : "Ask"}
          </button>
        </form>
        {agentAnswer && <p className="agent-answer">{agentAnswer}</p>}
      </section>

      {(anomalies.payout_issues.length > 0 ||
        anomalies.price_alerts.length > 0) && (
        <section className="insight-card anomaly-card" role="status">
          <div className="eyebrow">Anomaly monitor</div>
          <h2>Attention needed</h2>
          {anomalies.payout_issues.map((issue) => (
            <p key={`payout-${issue.transaction_id}`}>
              Payout {issue.status.toLowerCase()} for request {issue.request_id}
              {issue.message ? `: ${issue.message}` : "."}
            </p>
          ))}
          {anomalies.price_alerts.map((alert) => (
            <p key={`price-${alert.vendor}`}>
              {alert.vendor} ({alert.location}) price increased{" "}
              {Number(alert.increase_percent).toFixed(0)}% to USD{" "}
              {Number(alert.current_price_usd).toFixed(2)} per container.
            </p>
          ))}
        </section>
      )}

      <section className="insight-card">
        <div className="eyebrow">Supplier catalog</div>
        <h2>Add or update a local supplier</h2>
        <form className="merchant-form" onSubmit={saveMerchant}>
          <input
            aria-label="Supplier name"
            onChange={(event) =>
              setMerchantForm((current) => ({ ...current, name: event.target.value }))
            }
            placeholder="Supplier name"
            required
            value={merchantForm.name}
          />
          <input
            aria-label="Neighborhood and city"
            onChange={(event) =>
              setMerchantForm((current) => ({
                ...current,
                location: event.target.value,
              }))
            }
            placeholder="Neighborhood and city, e.g. Kibera, Nairobi"
            required
            value={merchantForm.location}
          />
          <input
            aria-label="County"
            maxLength={200}
            onChange={(event) =>
              setMerchantForm((current) => ({
                ...current,
                county: event.target.value,
              }))
            }
            placeholder="County"
            required
            value={merchantForm.county}
          />
          <input
            aria-label="Supplier PayPal email"
            onChange={(event) =>
              setMerchantForm((current) => ({
                ...current,
                paypal_email: event.target.value,
              }))
            }
            placeholder="Supplier PayPal email"
            required
            type="email"
            value={merchantForm.paypal_email}
          />
          <input
            aria-label="Price per container in USD"
            min="0.01"
            onChange={(event) =>
              setMerchantForm((current) => ({
                ...current,
                price_per_container_usd: event.target.value,
              }))
            }
            placeholder="Price per container (USD)"
            required
            step="0.01"
            type="number"
            value={merchantForm.price_per_container_usd}
          />
          <button
            className="primary-button"
            disabled={isSavingMerchant}
            type="submit"
          >
            {isSavingMerchant ? "Saving..." : "Save supplier"}
          </button>
        </form>
        <div className="insight-heading supplier-list-heading">
          <div>
            <h3>Supplier directory</h3>
            <p className="integration-note">
              {vendors.length} supplier(s). Double-click a cell to edit; changes
              save to the catalog immediately.
            </p>
          </div>
        </div>
        {vendors.length > 0 ? (
          <div className="grid-shell supplier-grid-shell ag-theme-quartz">
            <AgGridReact
              columnDefs={supplierColumnDefs}
              defaultColDef={{
                resizable: true,
                sortable: true,
                filter: true,
                flex: 1,
              }}
              onCellValueChanged={saveSupplierCell}
              rowData={vendors}
              rowHeight={52}
            />
          </div>
        ) : (
          <div className="empty-state">No suppliers have been added yet.</div>
        )}
        <p className="integration-note">
          Requests are matched to suppliers by shared neighborhood/city terms.
        </p>
      </section>

      {hasLoaded && requests.length === 0 && (
        <div className="empty-state">There are no requests to display.</div>
      )}
      {hasLoaded && requests.length > 0 && (
        <>
          <div className="request-toolbar">
            <label className="request-search">
              <span>Search requests</span>
              <input
                aria-label="Search requests"
                onChange={(event) => setRequestSearch(event.target.value)}
                placeholder="Name, region, merchant, or status"
                type="search"
                value={requestSearch}
              />
            </label>
            <label className="request-region-filter">
              <span>Region</span>
              <select
                onChange={(event) => setSelectedLocation(event.target.value)}
                value={selectedLocation}
              >
                <option value="">All regions</option>
                {requestLocations.map((location) => (
                  <option key={location} value={location}>
                    {location}
                  </option>
                ))}
              </select>
            </label>
            <div className="request-toolbar-end">
              {(requestSearch || selectedLocation) && (
                <button
                  className="clear-request-filters"
                  onClick={() => {
                    setRequestSearch("");
                    setSelectedLocation("");
                  }}
                  type="button"
                >
                  Clear filters
                </button>
              )}
              <fieldset className="density-control">
                <legend>Row spacing</legend>
                <div className="density-toggle">
                  <button
                    aria-pressed={gridDensity === "comfortable"}
                    className={gridDensity === "comfortable" ? "active" : ""}
                    onClick={() => setGridDensity("comfortable")}
                    type="button"
                  >
                    Comfortable
                  </button>
                  <button
                    aria-pressed={gridDensity === "compact"}
                    className={gridDensity === "compact" ? "active" : ""}
                    onClick={() => setGridDensity("compact")}
                    type="button"
                  >
                    Compact
                  </button>
                </div>
              </fieldset>
            </div>
          </div>
          <div className="request-results-summary" aria-live="polite">
            Showing <strong>{filteredRequests.length}</strong> of{" "}
            <strong>{requests.length}</strong> requests
            {selectedLocation && <span> in {selectedLocation}</span>}
            {requestSearch.trim() && <span> matching “{requestSearch.trim()}”</span>}
          </div>
          <div className={gridDensity === "compact" ? "grid-shell premium-grid compact-grid ag-theme-quartz" : "grid-shell premium-grid ag-theme-quartz"}>
            <AgGridReact
              columnDefs={columnDefs}
              defaultColDef={{
                resizable: true,
                sortable: true,
                filter: true,
                minWidth: 120,
                cellStyle: { display: "flex", alignItems: "center" },
              }}
              onCellValueChanged={saveRequestCell}
              rowData={filteredRequests}
              rowHeight={gridDensity === "compact" ? 64 : 76}
              rowSelection={{ mode: "singleRow", enableClickSelection: false }}
              suppressCellFocus
              tooltipShowDelay={250}
              quickFilterText={requestSearch}
            />
          </div>
          <p className="integration-note">
            After a PayPal payout is confirmed, the next month's review is
            added here automatically. Approve the review, match a supplier,
            then use Confirm &amp; Pay to purchase formula. Each month's
            request and payout are recorded separately; payouts are never
            automatic. Price and quantity are fixed when matched.
          </p>
        </>
      )}

      {selectedDecision && (
        <div className="dialog-backdrop">
          <section
            aria-labelledby="review-dialog-title"
            aria-modal="true"
            className="payout-dialog"
            role="dialog"
          >
            <button
              aria-label="Close review dialog"
              className="dialog-close"
              disabled={isLoading}
              onClick={() => {
                setSelectedDecision(null);
                setReviewNote("");
              }}
              type="button"
            >
              ×
            </button>
            <div className="eyebrow">Request review · #{selectedDecision.request.id}</div>
            <h2 id="review-dialog-title">
              {selectedDecision.decision === "approve" ? "Approve request" : "Reject request"}
            </h2>
            <p className="dialog-copy">
              {selectedDecision.request.recipient_name} · {selectedDecision.request.location}
              {" · "}Age {selectedDecision.request.infant_age_months} months
            </p>
            <form className="review-form" onSubmit={submitDecision}>
              <label>
                Review note
                <textarea
                  maxLength={2000}
                  onChange={(event) => setReviewNote(event.target.value)}
                  placeholder={
                    selectedDecision.decision === "reject"
                      ? "Explain why this request is being rejected (required)."
                      : "Optional context for this approval."
                  }
                  required={selectedDecision.decision === "reject"}
                  rows={4}
                  value={reviewNote}
                />
              </label>
              <div className="dialog-actions">
                <button
                  className="secondary-button"
                  disabled={isLoading}
                  onClick={() => {
                    setSelectedDecision(null);
                    setReviewNote("");
                  }}
                  type="button"
                >
                  Cancel
                </button>
                <button
                  className={
                    selectedDecision.decision === "reject"
                      ? "reject-confirm-button"
                      : "primary-button"
                  }
                  disabled={isLoading}
                  type="submit"
                >
                  {isLoading
                    ? "Saving decision..."
                    : selectedDecision.decision === "approve"
                      ? "Approve request"
                      : "Confirm rejection"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}

      {historyRequest && requestHistory && (
        <div className="dialog-backdrop">
          <section
            aria-labelledby="history-dialog-title"
            aria-modal="true"
            className="history-dialog"
            role="dialog"
          >
            <button
              aria-label="Close request history"
              className="dialog-close"
              onClick={() => {
                setHistoryRequest(null);
                setRequestHistory(null);
              }}
              type="button"
            >
              ×
            </button>
            <div className="eyebrow">Complete audit trail · Request #{historyRequest.id}</div>
            <h2 id="history-dialog-title">{historyRequest.recipient_name}</h2>
            <div className="audit-list">
              {requestHistory.map((event) => (
                <article className="audit-entry" key={event.id}>
                  <div className="audit-entry-heading">
                    <strong>{event.action.replaceAll("_", " ")}</strong>
                    <time dateTime={event.created_at}>
                      {new Date(event.created_at).toLocaleString()}
                    </time>
                  </div>
                  <p>
                    By {event.actor}
                    {event.previous_status
                      ? ` · ${event.previous_status} → ${event.new_status}`
                      : event.new_status
                        ? ` · ${event.new_status}`
                        : ""}
                  </p>
                  {event.merchant_name && <p>Merchant: {event.merchant_name}</p>}
                  {event.note && <p className="audit-note">{event.note}</p>}
                </article>
              ))}
              {requestHistory.length === 0 && (
                <p className="integration-note">No audit events are recorded yet.</p>
              )}
            </div>
            <div className="dialog-actions">
              <button
                className="secondary-button"
                onClick={() => {
                  setHistoryRequest(null);
                  setRequestHistory(null);
                }}
                type="button"
              >
                Close
              </button>
            </div>
          </section>
        </div>
      )}

      {selectedRequest && (
        <div className="dialog-backdrop">
          <section
            aria-labelledby="payout-dialog-title"
            aria-modal="true"
            className="payout-dialog"
            role="dialog"
          >
            <button
              aria-label="Close payout dialog"
              className="dialog-close"
              disabled={isPaying}
              onClick={() => setSelectedRequest(null)}
              type="button"
            >
              ×
            </button>
            <div className="eyebrow">Pool-funded merchant payout</div>
            <h2 id="payout-dialog-title">
              {dashboard?.payout_mode === "SIMULATED"
                ? "Stage simulated payout"
                : "Confirm & pay"}
            </h2>
            {dashboard?.payout_mode === "SIMULATED" && (
              <p className="payout-warning">
                SIMULATED ONLY: this stages a demo payout. No money will be
                sent to PayPal; a separate admin confirmation completes the
                simulation.
              </p>
            )}
            <p className="dialog-copy">
              Send USD{" "}
              {(
                (Number(selectedRequest.total_cost_usd || 0) /
                  Number(selectedRequest.containers_per_month || 1)) *
                purchaseContainers
              ).toFixed(2)}{" "}
              to{" "}
              <strong>{selectedRequest.matched_vendor}</strong> for{" "}
              {selectedRequest.recipient_name}.
            </p>
            <label className="payout-quantity-field">
              Containers to fund
              <select
                onChange={(event) =>
                  setPurchaseContainers(Number(event.target.value))
                }
                value={purchaseContainers}
              >
                {Array.from(
                  { length: Number(selectedRequest.containers_per_month) || 1 },
                  (_, index) => index + 1,
                ).map((count) => (
                  <option key={count} value={count}>
                    {count} {count === 1 ? "container" : "containers"}
                    {count === selectedRequest.containers_per_month
                      ? " · full monthly need"
                      : ""}
                  </option>
                ))}
              </select>
              <span>
                The request remains for{" "}
                {selectedRequest.containers_per_month} containers; choose fewer
                now if funds are limited.
              </span>
            </label>
            <p className="payout-warning">
              The backend checks and reserves pool funds before submitting this
              payout. An uncertain PayPal result will remain blocked for review.
            </p>
            <div className="dialog-actions">
              <button
                className="secondary-button"
                disabled={isPaying}
                onClick={() => setSelectedRequest(null)}
                type="button"
              >
                Cancel
              </button>
              <button
                className="primary-button"
                disabled={isPaying}
                onClick={submitPayout}
                type="button"
              >
                {isPaying
                  ? "Submitting payout..."
                  : dashboard?.payout_mode === "SIMULATED"
                    ? "Stage simulated payout"
                    : "Confirm & Pay"}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
