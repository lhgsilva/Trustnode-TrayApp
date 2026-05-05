import React, { useState } from "react";
import { loginAuth, issuePublicPasswordReset, applyPublicPasswordReset } from "../../api";
import "./Login.css";
import "./Login.local.css";
import "./Login.portal.css";
import "./Login.client.css";
import { EyeIcon } from "../Icons/EyeIcon";
import { ThemeIcon } from "../Icons/ThemeIcon";
import trustnodeLogo from "./trustenode-002.png";

export const Login = ({
  currentUser,
  isHostedWebClient,
  edgeLinked,
  theme,
  toggleTheme,
  onLoginSuccess,
}) => {
  const [loginForm, setLoginForm] = useState({ username: "", password: "" });
  const [showLoginPassword, setShowLoginPassword] = useState(false);
  const [rememberWorkstation, setRememberWorkstation] = useState(true);
  const [loginError, setLoginError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginTab, setLoginTab] = useState("login");
  const [forgotResult, setForgotResult] = useState("");
  const [forgotForm, setForgotForm] = useState({
    tenant_id: "",
    username: "",
    reset_token: "",
    new_password: "",
  });
  const [forgotBusy, setForgotBusy] = useState(false);
  const [edgeRegisterForm, setEdgeRegisterForm] = useState({
    activation_code: "",
    admin_username: "admin",
    admin_password: "",
  });
  const [edgeRegisterResult, setEdgeRegisterResult] = useState("");
  const [edgeRegisterBusy, setEdgeRegisterBusy] = useState(false);

  const logoSrc = trustnodeLogo;

  const [logoError, setLogoError] = React.useState(false);

  const handleLogoLoadError = (e) => {
    setLogoError(true);
  };

  const submitLogin = async () => {
    const username = String(loginForm.username || "").trim();
    const password = String(loginForm.password || "");
    if (!username || !password) {
      setLoginError("Enter username and password");
      return;
    }
    setLoginError("");
    setLoginBusy(true);

    try {
      const result = await loginAuth(loginForm.username, loginForm.password);
      if (result?.success) {
        if (rememberWorkstation) {
          try {
            localStorage.setItem("tn_remember_workstation", loginForm.username);
          } catch {
            // ignore localStorage errors
          }
        }
        onLoginSuccess?.(result);
      } else {
        setLoginError(result?.error || "Login failed");
      }
    } catch (err) {
      setLoginError(err?.message || "Network error during login");
    } finally {
      setLoginBusy(false);
    }
  };

  const requestForgotPasswordCode = async () => {
    const username = String(forgotForm.username || "").trim();
    if (!username) {
      setForgotResult("Enter your username first.");
      return;
    }
    setForgotBusy(true);
    setForgotResult("");
    try {
      const res = await issuePublicPasswordReset({
        username,
        tenant_id: String(forgotForm.tenant_id || "").trim(),
        ttl_minutes: 15,
      });
      const token = String(res?.row?.reset_token || "");
      setForgotForm((prev) => ({ ...prev, reset_token: token || prev.reset_token }));
      setForgotResult(token
        ? "Verification code generated. Use this code to set a new password."
        : "Verification code issued. Check portal/admin support channel.");
    } catch (err) {
      setForgotResult(`Code request failed: ${String(err?.message || err)}`);
    } finally {
      setForgotBusy(false);
    }
  };

  const applyForgotPasswordReset = async () => {
    const username = String(forgotForm.username || "").trim();
    const reset_token = String(forgotForm.reset_token || "").trim();
    const new_password = String(forgotForm.new_password || "");
    if (!username || !reset_token || !new_password) {
      setForgotResult("Username, verification code and new password are required.");
      return;
    }
    setForgotBusy(true);
    setForgotResult("");
    try {
      await applyPublicPasswordReset({
        username,
        tenant_id: String(forgotForm.tenant_id || "").trim(),
        reset_token,
        new_password,
      });
      setForgotResult("Password reset completed. You can login now.");
      setLoginForm((prev) => ({ ...prev, username }));
    } catch (err) {
      setForgotResult(`Reset failed: ${String(err?.message || err)}`);
    } finally {
      setForgotBusy(false);
    }
  };

  const submitEdgeRegister = async () => {
    setEdgeRegisterResult("Registering...");
    setEdgeRegisterBusy(true);
    try {
      const response = await fetch("/api/edge/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(edgeRegisterForm),
      });
      const result = await response.json();
      if (result?.success) {
        setEdgeRegisterResult("Edge activated successfully!");
        setEdgeRegisterForm({
          activation_code: "",
          admin_username: "",
          admin_password: "",
        });
      } else {
        setEdgeRegisterResult(result?.error || "Registration failed");
      }
    } catch (err) {
      setEdgeRegisterResult(err?.message || "Network error during registration");
    } finally {
      setEdgeRegisterBusy(false);
    }
  };

  const openEdgeRegisterModal = () => {
    setLoginTab("register");
  };

  if (currentUser) {
    return null;
  }

  const showRegisterTab = !isHostedWebClient && !edgeLinked;
  const isPortalV1Login = (() => {
    try {
      const path = String(window.location?.pathname || "");
      return path.startsWith("/portal/v1");
    } catch {
      return false;
    }
  })();

  return (
    <div className={`login-container theme-${theme} auth-surface-${loginSurface} ${isPortalV1Login ? "auth-shell--v1" : ""}`} data-theme={theme}>
      <div className={`auth-card ${loginTab === "register" ? "activate-mode" : ""}`}>
        <button
          className="theme-toggle-btn"
          onClick={toggleTheme}
          type="button"
          aria-label="Toggle theme"
        >
          <ThemeIcon theme={theme} />
        </button>
        <div className="auth-brand">
          <img
            src={logoSrc}
            alt="Trustnode"
            className="auth-logo"
            onError={handleLogoLoadError}
          />
        </div>

        <div className="auth-tabs">
          <button
            className={`auth-tab ${loginTab === "login" ? "active" : ""}`}
            type="button"
            onClick={() => setLoginTab("login")}
          >
            Login
          </button>
          {showRegisterTab ? (
            <button
              className={`auth-tab ${loginTab === "register" ? "active" : ""}`}
              type="button"
              onClick={openEdgeRegisterModal}
            >
              Activate
            </button>
          ) : null}
        </div>

        {loginTab === "register" && showRegisterTab ? (
          <>
            <h3 className="auth-heading">Activate TrustNode Edge</h3>
            <p className="auth-activate-subtitle">
              Paste your activation code, then choose the administrator account you'll use to sign in to this Edge app.
            </p>

            <label>
              <span>Activation code</span>
              <div className="input-wrapper">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <rect x="3" y="5" width="18" height="14" rx="2" />
                    <path d="M3 10h18" />
                  </svg>
                </div>
                <input
                  placeholder="Paste your activation code"
                  value={edgeRegisterForm.activation_code}
                  onChange={(e) =>
                    setEdgeRegisterForm((p) => ({
                      ...p,
                      activation_code: e.target.value,
                    }))
                  }
                />
              </div>
              <div className="auth-field-help">
                A single string provided with your Edge license.
              </div>
            </label>

            <div className="auth-section-label">
              CREATE ADMINISTRATOR ACCOUNT
            </div>

            <label>
              <span>Admin login</span>
              <div className="input-wrapper">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="8" r="4" />
                    <path d="M4 20c0-4 3.5-7 8-7s8 3 8 7" />
                  </svg>
                </div>
                <input
                  placeholder="e.g. plant-admin"
                  value={edgeRegisterForm.admin_username}
                  onChange={(e) =>
                    setEdgeRegisterForm((p) => ({
                      ...p,
                      admin_username: e.target.value,
                    }))
                  }
                />
              </div>
              <div className="auth-field-help">
                This is the username you'll log in with after activation.
              </div>
            </label>

            <label>
              <span>Admin password</span>
              <div className="input-wrapper">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <rect x="3" y="11" width="18" height="10" rx="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <input
                  type="password"
                  placeholder="Set a password (8+ characters)"
                  value={edgeRegisterForm.admin_password}
                  onChange={(e) =>
                    setEdgeRegisterForm((p) => ({
                      ...p,
                      admin_password: e.target.value,
                    }))
                  }
                />
              </div>
            </label>

            <label>
              <span>Confirm password</span>
              <div className="input-wrapper">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <rect x="3" y="11" width="18" height="10" rx="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <input
                  type="password"
                  placeholder="Re-enter the password"
                />
              </div>
            </label>

            {edgeRegisterResult ? (
              <div
                className={
                  edgeRegisterResult.includes("failed") ? "error" : "lock-note"
                }
              >
                {edgeRegisterResult}
              </div>
            ) : null}

            <button
              className="btn btn-primary auth-submit"
              onClick={submitEdgeRegister}
              disabled={edgeRegisterBusy}
            >
              {edgeRegisterBusy ? "Activating..." : "Activate Edge app ->"}
            </button>
          </>
        ) : (
          <>
            <div className="auth-welcome">
              <div className="auth-welcome-title">Welcome back</div>
              <div className="auth-welcome-subtitle">
                Sign in to access your fleet dashboard, acquisition pipelines and plant-wide insights.
              </div>
            </div>

            <label>
              <span>Username or email</span>
              <div className="input-wrapper">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <circle cx="12" cy="8" r="4" />
                    <path d="M4 20c0-4 3.5-7 8-7s8 3 8 7" />
                  </svg>
                </div>
                <input
                  placeholder="e.g. m.silva@plant.io"
                  value={loginForm.username}
                  onChange={(e) =>
                    setLoginForm({ ...loginForm, username: e.target.value })
                  }
                />
              </div>
            </label>

            <label>
              <div className="pw-field-header">
                <span>Password</span>
                <button
                  className="forgot-link"
                  type="button"
                  onClick={() => setForgotResult("Fill username and click Request Code.")}
                >
                  Forgot password?
                </button>
              </div>
              <div className="pw-input-wrap">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <rect x="3" y="11" width="18" height="10" rx="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <input
                  type={showLoginPassword ? "text" : "password"}
                  placeholder="Enter your password"
                  value={loginForm.password}
                  onChange={(e) =>
                    setLoginForm({ ...loginForm, password: e.target.value })
                  }
                />
                <button
                  className="pw-icon-btn"
                  onClick={() => setShowLoginPassword((v) => !v)}
                  type="button"
                  aria-label="Toggle password visibility"
                >
                  <EyeIcon open={showLoginPassword} />
                </button>
              </div>
            </label>

            <label className="remember-row">
              <input
                type="checkbox"
                checked={rememberWorkstation}
                onChange={(e) => setRememberWorkstation(e.target.checked)}
              />
              <span className="remember-label">Remember this workstation</span>
            </label>

            {forgotResult ? (
              <div className="auth-forgot-box">
                <label>
                  <span>Tenant (optional)</span>
                  <input
                    placeholder="default"
                    value={forgotForm.tenant_id}
                    onChange={(e) =>
                      setForgotForm((p) => ({
                        ...p,
                        tenant_id: e.target.value,
                      }))
                    }
                  />
                </label>
                <label>
                  <span>Username</span>
                  <input
                    value={forgotForm.username}
                    onChange={(e) =>
                      setForgotForm((p) => ({
                        ...p,
                        username: e.target.value,
                      }))
                    }
                  />
                </label>
                <label>
                  <span>Verification Code</span>
                  <input
                    value={forgotForm.reset_token}
                    onChange={(e) =>
                      setForgotForm((p) => ({
                        ...p,
                        reset_token: e.target.value,
                      }))
                    }
                  />
                </label>
                <label>
                  <span>New Password</span>
                  <input
                    type="password"
                    value={forgotForm.new_password}
                    onChange={(e) =>
                      setForgotForm((p) => ({
                        ...p,
                        new_password: e.target.value,
                      }))
                    }
                  />
                </label>
                <div className="row" style={{ gap: 8 }}>
                  <button
                    className="btn btn-primary"
                    type="button"
                    onClick={requestForgotPasswordCode}
                    disabled={forgotBusy}
                    style={{ flex: 1 }}
                  >
                    {forgotBusy ? "Requesting..." : "Request Code"}
                  </button>
                  <button
                    className="btn btn-primary"
                    type="button"
                    onClick={applyForgotPasswordReset}
                    disabled={forgotBusy}
                    style={{ flex: 1 }}
                  >
                    Apply Reset
                  </button>
                </div>
                <div className="lock-note">{forgotResult}</div>
              </div>
            ) : null}

            {loginError ? <div className="error">{loginError}</div> : null}

            <button
              className="btn btn-primary auth-submit"
              onClick={submitLogin}
              disabled={loginBusy}
            >
              {loginBusy ? "Signing in..." : "Sign in to TrustNode portal ->"}
            </button>
          </>
        )}
      </div>
    </div>
  );
};

