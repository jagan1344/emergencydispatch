import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { login } from "../services/api";

export default function Login() {
  const [username, setU] = useState("dispatcher");
  const [password, setP] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const nav = useNavigate();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setError(null);
    try { await login(username, password); nav("/"); }
    catch (err: any) { setError(err.message); }
    finally { setBusy(false); }
  };
  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <div className="brand big"><span className="cross">✚</span><div><b>EMS</b> Command Center</div></div>
        <p className="muted">AI-assisted emergency dispatch &amp; traffic-aware routing (academic simulation)</p>
        <label>Username<input value={username} onChange={(e) => setU(e.target.value)} autoComplete="username" name="username" /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setP(e.target.value)} autoComplete="current-password" name="password" /></label>
        {error && <div className="error-note" role="alert">{error}</div>}
        <button className="primary" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="muted small">Development accounts: admin / dispatcher / viewer (see README).</p>
      </form>
    </div>
  );
}
