import { ReactNode } from "react";
import { SEVERITY_COLOR } from "../services/format";

export function SeverityBadge({ s }: { s: string | null | undefined }) {
  if (!s) return <span className="badge muted">—</span>;
  return <span className="badge" style={{ background: SEVERITY_COLOR[s] }}>{s}</span>;
}

export function StatusBadge({ s }: { s: string }) {
  return <span className={`badge status s-${s.toLowerCase()}`}>{s.replace(/_/g, " ")}</span>;
}

export function Kpi({ label, value, sub, tone, testId }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string; testId?: string }) {
  return (
    <div className={`kpi ${tone || ""}`} data-testid={testId}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Panel({ title, actions, children, className }: { title: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`panel ${className || ""}`}>
      <header><h2>{title}</h2>{actions && <div className="panel-actions">{actions}</div>}</header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function Bar({ value, max = 1, color }: { value: number; max?: number; color?: string }) {
  const w = Math.max(0, Math.min(100, (value / max) * 100));
  return <div className="bar"><div style={{ width: `${w}%`, background: color }} /></div>;
}

export function ErrorNote({ error }: { error: string | null }) {
  return error ? <div className="error-note" role="alert">{error}</div> : null;
}
