import type { ReactNode } from "react";

export const ICONS = {
  results: "M4 20h16M7 16v-5M12 16V6M17 16v-8",
  plus: "M12 5v14M5 12h14",
  play: "M7 5l12 7-12 7z",
  source: "M6 3v12M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9a9 9 0 0 1-9 9",
  model: "M7 5h10v14H7zM9 3v2M15 3v2M9 19v2M15 19v2M3 9h4M3 15h4M17 9h4M17 15h4",
  sync: "M20 11a8 8 0 0 0-14.9-3M4 4v4h4M4 13a8 8 0 0 0 14.9 3M20 20v-4h-4",
  ok: "M5 12l5 5 9-10",
  fail: "M6 6l12 12M18 6L6 18",
  wait: "M12 7v5l3 2",
  down: "M6 9l6 6 6-6",
  up: "M6 15l6-6 6 6",
  trash: "M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3",
  edit: "M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4z",
};

export function Icon({ d, size = 18, color = "currentColor", width = 2 }: { d: string; size?: number; color?: string; width?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" style={{ flex: "none" }}>
      <path d={d} stroke={color} strokeWidth={width} fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function PageHead({ title, lead, children }: { title: ReactNode; lead?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1 className="title">{title}</h1>
        {lead && <p className="lead">{lead}</p>}
      </div>
      {children && <div className="row">{children}</div>}
    </div>
  );
}

export function Kpi({ label, value, sub, color }: { label: string; value: ReactNode; sub?: ReactNode; color?: string }) {
  return (
    <div className="kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value" style={color ? { color } : undefined}>{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Badge({ text, tone, dot }: { text: string; tone: string; dot?: boolean }) {
  return (
    <span className={`badge tone-${tone}`}>
      {dot && <span className="dot" />}
      {text}
    </span>
  );
}

export function Chips<K extends string | number>({ items, value, onChange, label }: {
  items: { key: K; label: string }[];
  value: K;
  onChange: (k: K) => void;
  label: string;
}) {
  return (
    <div role="group" aria-label={label} className="chips">
      {items.map((it) => (
        <button key={String(it.key)} type="button" className="chip" aria-pressed={value === it.key} onClick={() => onChange(it.key)}>
          {it.label}
        </button>
      ))}
    </div>
  );
}

export function Bar({ pct, color, opacity, size }: { pct: number; color?: string; opacity?: number; size?: "thin" | "thick" }) {
  return (
    <span className={`bar ${size || ""}`}>
      <span style={{ width: `${Math.max(0, Math.min(100, pct))}%`, background: color, opacity }} />
    </span>
  );
}

export function Loading() {
  return <p className="loading" role="status">Đang tải…</p>;
}

export function ErrorNotice({ error, retry }: { error: string; retry?: () => void }) {
  return (
    <div className="notice err row" role="alert" style={{ justifyContent: "space-between" }}>
      <span>Không tải được dữ liệu: {error}</span>
      {retry && <button type="button" className="btn btn-sm" onClick={retry}>Thử lại</button>}
    </div>
  );
}
