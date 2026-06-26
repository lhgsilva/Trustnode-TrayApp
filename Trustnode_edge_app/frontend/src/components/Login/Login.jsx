import React, { useState } from "react";
import {
  loginAuth,
  issuePublicPasswordReset,
  applyPublicPasswordReset,
  emailPasswordReset,
  applyEmailPasswordReset,
  registerControlPlaneEdgeLink,
  registerControlPlaneEdgeLinkLogin,
  getAuthMe,
} from "../../api";
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
  const [showAdminPassword, setShowAdminPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [showForgotNewPassword, setShowForgotNewPassword] = useState(false);
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
    confirm_password: "",
  });
  const [edgeRegisterResult, setEdgeRegisterResult] = useState("");
  const [edgeRegisterBusy, setEdgeRegisterBusy] = useState(false);

  const logoSrc = React.useMemo(() => {
    try {
      const protocol = String(window.location?.protocol || "");
      if (protocol === "file:") return "assets/trustenode-002.png";
      const origin = String(window.location?.origin || "");
      if (origin) return `${origin.replace(/\/+$/, "")}/assets/trustenode-002.png`;
    } catch {}
    return trustnodeLogo;
  }, []);

  const [logoError, setLogoError] = React.useState(false);

  const handleLogoLoadError = (e) => {
    const img = e?.currentTarget;
    if (img && img.src && !img.src.includes("trustenode-002.png")) {
      img.src = trustnodeLogo;
      return;
    }
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
      if (result?.ok || result?.success || result?.token) {
        if (rememberWorkstation) {
          try {
            localStorage.setItem("tn_remember_workstation", loginForm.username);
          } catch {
            // ignore localStorage errors
          }
        }
        const sessionUser =
          result?.user || (await getAuthMe().catch(() => null))?.user || null;
        if (sessionUser?.username) {
          onLoginSuccess?.(sessionUser);
        } else {
          setLoginError("Login succeeded but user session payload is missing");
        }
      } else {
        setLoginError(
          result?.error ||
            result?.detail ||
            result?.message ||
            "Login failed"
        );
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

  // Operator 2026-06-24: email-based forgot-password. Asks the edge
  // to send a reset link to the user's registered email via SMTP.
  // The endpoint always returns ok=True to avoid leaking whether the
  // account exists.
  const requestForgotPasswordEmail = async () => {
    const identifier = String(forgotForm.username || "").trim();
    if (!identifier) {
      setForgotResult("Enter your username or email first.");
      return;
    }
    setForgotBusy(true);
    setForgotResult("");
    try {
      await emailPasswordReset(identifier);
      setForgotResult("If a matching account exists, a reset email has been sent. Check your inbox.");
    } catch (err) {
      setForgotResult(`Email request failed: ${String(err?.message || err)}`);
    } finally {
      setForgotBusy(false);
    }
  };

  // Auto-fill the verification-code field when the user clicked an
  // email reset link (URL carries ?reset_token=...). The Apply Reset
  // button below then drives a single-step email-token apply via the
  // new /api/auth/reset-password endpoint.
  React.useEffect(() => {
    try {
      const sp = new URLSearchParams(window.location.search || "");
      const tok = String(sp.get("reset_token") || "").trim();
      if (tok && !forgotForm.reset_token) {
        setForgotForm((p) => ({ ...p, reset_token: tok }));
        setForgotMode(true);
        setForgotResult("Reset link detected. Enter a new password and click Apply Reset.");
      }
    } catch { /* ignore */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const applyForgotPasswordReset = async () => {
    const username = String(forgotForm.username || "").trim();
    const reset_token = String(forgotForm.reset_token || "").trim();
    const new_password = String(forgotForm.new_password || "");
    if (!reset_token || !new_password) {
      setForgotResult("Reset code and new password are required.");
      return;
    }
    setForgotBusy(true);
    setForgotResult("");
    // Operator 2026-06-24: try the edge-local email-reset path first
    // (no username needed — the token IS the identifier). If that
    // fails, fall back to the existing portal-issued-code flow which
    // requires the username + tenant.
    try {
      await applyEmailPasswordReset(reset_token, new_password);
      setForgotResult("Password reset completed. You can login now.");
      if (username) setLoginForm((prev) => ({ ...prev, username }));
      return;
    } catch (err) {
      // Fall through to portal-code path only if the user provided
      // a username (the portal flow requires one).
      if (!username) {
        setForgotResult(`Reset failed: ${String(err?.message || err)}`);
        setForgotBusy(false);
        return;
      }
    }
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
    const activationCode = String(edgeRegisterForm.activation_code || "").trim();
    const adminUsername = String(edgeRegisterForm.admin_username || "").trim();
    const adminPassword = String(edgeRegisterForm.admin_password || "");
    const confirmPassword = String(edgeRegisterForm.confirm_password || "");
    if (!activationCode) {
      setEdgeRegisterResult("Activation code is required.");
      return;
    }
    if (!adminUsername) {
      setEdgeRegisterResult("Admin login is required.");
      return;
    }
    if (adminPassword.length < 8) {
      setEdgeRegisterResult("Admin password must be at least 8 characters.");
      return;
    }
    if (adminPassword !== confirmPassword) {
      setEdgeRegisterResult("Confirm password must match admin password.");
      return;
    }
    setEdgeRegisterResult("Activating...");
    setEdgeRegisterBusy(true);
    try {
      const payload = {
        activation_code: activationCode,
        // Edge identity is resolved from the activation code binding in control-plane.
        edge_id: "",
        edge_name: "",
        site: "",
        area: "",
        equipment: "",
        admin_username: adminUsername,
        admin_password: adminPassword,
      };
      const result = await registerControlPlaneEdgeLinkLogin(payload);
      if (result?.ok) {
        setEdgeRegisterResult("Edge activated successfully. Admin user created for customer scope.");
        setEdgeRegisterForm({
          activation_code: "",
          admin_username: "",
          admin_password: "",
          confirm_password: "",
        });
        setLoginForm((prev) => ({
          ...prev,
          username: String(payload.admin_username || prev.username || ""),
        }));
        setLoginTab("login");
      } else {
        setEdgeRegisterResult(
          String(result?.detail || result?.error || "Activation failed")
        );
      }
    } catch (err) {
      setEdgeRegisterResult(
        `Edge registration failed: ${String(err?.message || err)}`
      );
    } finally {
      setEdgeRegisterBusy(false);
    }
  };

  const canActivate = (() => {
    const code = String(edgeRegisterForm.activation_code || "").trim();
    const user = String(edgeRegisterForm.admin_username || "").trim();
    const pass = String(edgeRegisterForm.admin_password || "");
    const confirm = String(edgeRegisterForm.confirm_password || "");
    return code.length > 0 && user.length > 0 && pass.length >= 8 && pass === confirm;
  })();

  const openEdgeRegisterModal = () => {
    setLoginTab("register");
  };

  if (currentUser) {
    return null;
  }

  const loginSurface = (() => {
    try {
      const path = String(window.location?.pathname || "").toLowerCase();
      if (path.startsWith("/portal") || path.startsWith("/developer-portal")) return "portal";
      if (isHostedWebClient) return "client";
      return "local";
    } catch {
      return "local";
    }
  })();
  const isPortalV1Login = (() => {
    try {
      const path = String(window.location?.pathname || "");
      return path.startsWith("/portal/v1");
    } catch {
      return false;
    }
  })();
  const isPortalLoginOnly = loginSurface === "portal";
  const showRegisterTab = loginSurface === "local" && !edgeLinked;
  const effectiveLoginTab =
    isPortalLoginOnly && loginTab === "register" ? "login" : loginTab;
  const loginActionLabelBySurface = {
    local: "Sign in to TrustNode Edge",
    portal: "Sign in to TrustNode Portal",
    client: "Sign in to Trusnode Client View",
  };
  const loginActionLabel =
    loginActionLabelBySurface[loginSurface] || "Sign in";

  return (
    <div className={`login-container theme-${theme} auth-surface-${loginSurface} ${isPortalV1Login ? "auth-shell--v1" : ""}`} data-theme={theme}>
      <div className={`auth-card ${effectiveLoginTab === "register" ? "activate-mode" : ""}`}>
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

        <div className={`auth-tabs ${isPortalLoginOnly ? "single-tab" : ""}`}>
          <button
            className={`auth-tab ${effectiveLoginTab === "login" ? "active active-login" : ""}`}
            type="button"
            onClick={() => setLoginTab("login")}
          >
            Login
          </button>
          {showRegisterTab ? (
            <button
              className={`auth-tab ${effectiveLoginTab === "register" ? "active active-activate" : ""}`}
              type="button"
              onClick={openEdgeRegisterModal}
            >
              Activate
            </button>
          ) : null}
        </div>

        {effectiveLoginTab === "register" && showRegisterTab ? (
          <>
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
              <div className="pw-input-wrap">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <rect x="3" y="11" width="18" height="10" rx="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <input
                  type={showAdminPassword ? "text" : "password"}
                  placeholder="Set a password (8+ characters)"
                  value={edgeRegisterForm.admin_password}
                  onChange={(e) =>
                    setEdgeRegisterForm((p) => ({
                      ...p,
                      admin_password: e.target.value,
                    }))
                  }
                />
                <button
                  className="pw-icon-btn"
                  onClick={() => setShowAdminPassword((v) => !v)}
                  type="button"
                  aria-label="Toggle admin password visibility"
                >
                  <EyeIcon open={showAdminPassword} />
                </button>
              </div>
            </label>

            <label>
              <span>Confirm password</span>
              <div className="pw-input-wrap">
                <div className="field-icon">
                  <svg viewBox="0 0 24 24" fill="none">
                    <rect x="3" y="11" width="18" height="10" rx="2" />
                    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  </svg>
                </div>
                <input
                  type={showConfirmPassword ? "text" : "password"}
                  placeholder="Re-enter the password"
                  value={edgeRegisterForm.confirm_password}
                  onChange={(e) =>
                    setEdgeRegisterForm((p) => ({
                      ...p,
                      confirm_password: e.target.value,
                    }))
                  }
                />
                <button
                  className="pw-icon-btn"
                  onClick={() => setShowConfirmPassword((v) => !v)}
                  type="button"
                  aria-label="Toggle confirm password visibility"
                >
                  <EyeIcon open={showConfirmPassword} />
                </button>
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
              disabled={edgeRegisterBusy || !canActivate}
            >
              {edgeRegisterBusy ? "Activating..." : "Activate Edge App"}
            </button>
          </>
        ) : (
          <>
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
                  <div className="pw-input-wrap">
                    <div className="field-icon">
                      <svg viewBox="0 0 24 24" fill="none">
                        <rect x="3" y="11" width="18" height="10" rx="2" />
                        <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                      </svg>
                    </div>
                    <input
                      type={showForgotNewPassword ? "text" : "password"}
                      value={forgotForm.new_password}
                      onChange={(e) =>
                        setForgotForm((p) => ({
                          ...p,
                          new_password: e.target.value,
                        }))
                      }
                    />
                    <button
                      className="pw-icon-btn"
                      onClick={() => setShowForgotNewPassword((v) => !v)}
                      type="button"
                      aria-label="Toggle new password visibility"
                    >
                      <EyeIcon open={showForgotNewPassword} />
                    </button>
                  </div>
                </label>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  <button
                    className="btn btn-primary"
                    type="button"
                    onClick={requestForgotPasswordCode}
                    disabled={forgotBusy}
                    style={{ flex: "1 1 130px" }}
                    title="Generate a one-time code via the cloud portal"
                  >
                    {forgotBusy ? "Requesting..." : "Request Code"}
                  </button>
                  <button
                    className="btn btn-secondary"
                    type="button"
                    onClick={requestForgotPasswordEmail}
                    disabled={forgotBusy}
                    style={{ flex: "1 1 130px" }}
                    title="Email a reset link to the address registered on this user"
                  >
                    {forgotBusy ? "Sending..." : "Email reset link"}
                  </button>
                  <button
                    className="btn btn-primary"
                    type="button"
                    onClick={applyForgotPasswordReset}
                    disabled={forgotBusy}
                    style={{ flex: "1 1 130px" }}
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
              {loginBusy ? "Signing in..." : String(loginActionLabel || "Sign in").replace(/[-–—>]+/g, " ").trim()}
            </button>
          </>
        )}
      </div>
    </div>
  );
};



