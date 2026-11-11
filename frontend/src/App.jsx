import { lazy, Suspense } from "react";
import { BrowserRouter, Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import AdminDashboard from "./components/AdminDashboard.jsx";
import DonorPortal from "./components/DonorPortal.jsx";
import LandingPage from "./components/LandingPage.jsx";
import RecipientForm from "./components/RecipientForm.jsx";
import StatusPage from "./components/StatusPage.jsx";

const StudioPage = lazy(() => import("./components/StudioPage.jsx"));
const ReportsPage = lazy(() => import("./components/ReportsPage.jsx"));

function ApplicationShell({ children }) {
  return (
    <main className="app-shell">
      <header className="site-header">
        <Link className="brand" to="/" aria-label="FormulaFlow AI home">
          <span className="brand-mark" aria-hidden="true">
            F
          </span>
          <span>FormulaFlow AI</span>
        </Link>
        <nav className="view-switch" aria-label="Application views">
          <NavLink
            className={({ isActive }) =>
              isActive ? "nav-button active" : "nav-button"
            }
            to="/donate"
          >
            Donate
          </NavLink>
          <NavLink
            className={({ isActive }) =>
              isActive ? "nav-button active" : "nav-button"
            }
            to="/request"
          >
            Request support
          </NavLink>
          <NavLink
            className={({ isActive }) =>
              isActive ? "nav-button active" : "nav-button"
            }
            to="/status"
          >
            Check status
          </NavLink>
          <NavLink
            className={({ isActive }) =>
              isActive ? "nav-button active" : "nav-button"
            }
            to="/admin"
          >
            Internal operations
          </NavLink>
          <NavLink
            className={({ isActive }) =>
              isActive ? "nav-button active" : "nav-button"
            }
            to="/reports/overview"
          >
            Reports
          </NavLink>
          <NavLink
            className={({ isActive }) =>
              isActive ? "nav-button active" : "nav-button"
            }
            to="/studio"
          >
            Studio
          </NavLink>
        </nav>
      </header>
      {children}
      <footer className="site-footer">
        FormulaFlow connects families with local formula support.
      </footer>
    </main>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<LandingPage />} />
        <Route
          path="/donate"
          element={
            <ApplicationShell>
              <DonorPortal />
            </ApplicationShell>
          }
        />
        <Route
          path="/request"
          element={
            <ApplicationShell>
              <RecipientForm />
            </ApplicationShell>
          }
        />
        <Route
          path="/status"
          element={
            <ApplicationShell>
              <StatusPage />
            </ApplicationShell>
          }
        />
        <Route
          path="/admin"
          element={
            <ApplicationShell>
              <AdminDashboard />
            </ApplicationShell>
          }
        />
        <Route
          path="/reports/:reportId?"
          element={
            <Suspense
              fallback={
                <main className="studio-page">
                  <section className="studio-message" role="status">
                    Loading reports…
                  </section>
                </main>
              }
            >
              <ApplicationShell>
                <ReportsPage />
              </ApplicationShell>
            </Suspense>
          }
        />
        <Route
          path="/studio"
          element={
            <Suspense
              fallback={
                <main className="studio-page">
                  <section className="studio-message" role="status">
                    Loading analytics studio…
                  </section>
                </main>
              }
            >
              <StudioPage />
            </Suspense>
          }
        />
        <Route path="*" element={<Navigate replace to="/" />} />
      </Routes>
    </BrowserRouter>
  );
}
