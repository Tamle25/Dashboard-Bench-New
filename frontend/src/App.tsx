import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api";
import { useApi } from "./hooks";
import { ICONS, Icon } from "./ui";
import ResultsPage from "./pages/Results";
import NewRunPage from "./pages/NewRun";
import RunningPage from "./pages/Running";
import DetailPage from "./pages/Detail";
import SourcesPage from "./pages/Sources";
import ModelsPage from "./pages/Models";

interface NavItem {
  to: string;
  label: string;
  icon: string;
  match: (path: string) => boolean;
  badge?: { text: string; err?: boolean } | null;
}

function NavLinkItem({ item, path }: { item: NavItem; path: string }) {
  const on = item.match(path);
  return (
    <Link to={item.to} className="nav-link" aria-current={on ? "page" : undefined}>
      <Icon d={item.icon} />
      <span className="label">{item.label}</span>
      {item.badge && <span className={`nav-badge ${item.badge.err ? "err" : ""}`}>{item.badge.text}</span>}
    </Link>
  );
}

export default function App() {
  const { pathname } = useLocation();
  const { data: stats } = useApi(api.stats, [pathname], 4000);

  const main: NavItem[] = [
    { to: "/", label: "Kết quả", icon: ICONS.results, match: (p) => p === "/" || p.startsWith("/results") },
    { to: "/runs/new", label: "Chạy mới", icon: ICONS.plus, match: (p) => p === "/runs/new" },
    {
      to: stats?.active_run ? `/runs/${stats.active_run.id}` : "/runs/active",
      label: "Đang chạy",
      icon: ICONS.play,
      match: (p) => p.startsWith("/runs/") && p !== "/runs/new",
      badge: stats?.active_run ? { text: `${stats.active_run.pct}%` } : null,
    },
  ];
  const config: NavItem[] = [
    {
      to: "/sources", label: "Nguồn bench", icon: ICONS.source, match: (p) => p.startsWith("/sources"),
      badge: stats?.sources_error ? { text: `${stats.sources_error} lỗi`, err: true } : null,
    },
    {
      to: "/models", label: "Mô hình", icon: ICONS.model, match: (p) => p.startsWith("/models"),
      badge: stats?.models_down ? { text: `${stats.models_down} lỗi`, err: true } : null,
    },
  ];

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <svg width="30" height="30" viewBox="0 0 30 30" aria-hidden="true">
            <rect x="1" y="1" width="28" height="28" rx="8" fill="var(--accent)" />
            <path d="M8 20 L13 13 L17 16.5 L22 9.5" stroke="#fff" strokeWidth="2.2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <div>
            <div className="brand-name">Bench Console</div>
            <div className="brand-sub">Đánh giá mô hình</div>
          </div>
        </div>
        <nav aria-label="Điều hướng chính" className="nav">
          {main.map((it) => <NavLinkItem key={it.label} item={it} path={pathname} />)}
          <div className="nav-section">CẤU HÌNH</div>
          {config.map((it) => <NavLinkItem key={it.label} item={it} path={pathname} />)}
        </nav>
      </aside>
      <main className="main">
        <div className="container">
          <Routes>
            <Route path="/" element={<ResultsPage />} />
            <Route path="/results/:id" element={<DetailPage />} />
            <Route path="/runs/new" element={<NewRunPage />} />
            <Route path="/runs/active" element={<RunningPage />} />
            <Route path="/runs/:id" element={<RunningPage />} />
            <Route path="/sources" element={<SourcesPage />} />
            <Route path="/models" element={<ModelsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
