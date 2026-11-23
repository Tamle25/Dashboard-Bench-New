import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, errMsg } from "../api";
import { fmtClock, fmtScore, RUN_STATUS } from "../format";
import type { LogLine, Run } from "../types";
import { Badge, Bar, ErrorNotice, Loading } from "../ui";

const MAX_LINES = 2000;

/** Nghe SSE của một lượt chạy: ảnh chụp tiến độ + từng dòng log. */
function useRunStream(id: number | null) {
  const [run, setRun] = useState<Run>();
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [error, setError] = useState<string>();
  const lastId = useRef(0);

  useEffect(() => {
    if (id == null) return;
    setRun(undefined);
    setLogs([]);
    setError(undefined);
    lastId.current = 0;
    const es = new EventSource(api.streamUrl(id));
    es.addEventListener("progress", (e) => setRun(JSON.parse((e as MessageEvent).data)));
    es.addEventListener("log", (e) => {
      const line: LogLine = JSON.parse((e as MessageEvent).data);
      if (line.id <= lastId.current) return; // bỏ trùng giữa phần phát lại và sự kiện trực tiếp
      lastId.current = line.id;
      setLogs((prev) => (prev.length >= MAX_LINES ? [...prev.slice(-MAX_LINES + 1), line] : [...prev, line]));
    });
    es.addEventListener("end", () => es.close());
    es.onerror = () => {
      // EventSource tự kết nối lại; chỉ báo lỗi nếu chưa nhận được gì.
      setRun((r) => {
        if (!r) setError("Không kết nối được luồng log của lượt chạy.");
        return r;
      });
    };
    return () => es.close();
  }, [id]);

  return { run, logs, error };
}

export default function RunningPage() {
  const params = useParams();
  const navigate = useNavigate();
  const id = params.id ? Number(params.id) : null;
  const [lookup, setLookup] = useState<"loading" | "none">("loading");

  // /runs/active: tìm lượt đang chạy rồi chuyển sang /runs/:id
  useEffect(() => {
    if (id != null) return;
    api.activeRun().then((r) => (r ? navigate(`/runs/${r.id}`, { replace: true }) : setLookup("none"))).catch(() => setLookup("none"));
  }, [id, navigate]);

  if (id == null) {
    if (lookup === "loading") return <Loading />;
    return (
      <div className="card empty">
        <h1>Chưa có lượt chạy nào đang diễn ra</h1>
        <p>Tạo một lượt chạy mới để theo dõi tiến độ và log trực tiếp tại đây.</p>
        <Link className="btn btn-primary" to="/runs/new">Chạy benchmark mới</Link>
      </div>
    );
  }
  return <RunView id={id} />;
}

function RunView({ id }: { id: number }) {
  const navigate = useNavigate();
  const { run, logs, error } = useRunStream(id);
  const [logFilter, setLogFilter] = useState<"all" | "warn">("all");
  const [autoscroll, setAutoscroll] = useState(true);
  const [actionError, setActionError] = useState<string>();
  const [now, setNow] = useState(Date.now());
  const logEl = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoscroll && logEl.current) logEl.current.scrollTop = logEl.current.scrollHeight;
  }, [logs, autoscroll, logFilter]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  if (error) return <ErrorNotice error={error} />;
  if (!run) return <Loading />;

  const st = RUN_STATUS[run.status];
  const isRunning = run.status === "running" || run.status === "queued";
  const totalAll = run.entries.reduce((a, e) => a + e.total, 0);
  const doneAll = run.entries.reduce((a, e) => a + e.done, 0);
  const pct = totalAll ? Math.round((doneAll / totalAll) * 100) : 0;
  const elapsed = run.started_at ? Math.max(1, (now - new Date(run.started_at).getTime()) / 1000) : 0;
  const eta = doneAll ? Math.round((elapsed / doneAll) * (totalAll - doneAll)) : 0;
  const etaText = isRunning ? (doneAll ? `còn khoảng ${eta} giây` : "đang ước tính thời gian…") : run.status === "done" ? "đã hoàn tất" : st.text.toLowerCase();
  const shown = logFilter === "all" ? logs : logs.filter((l) => l.lvl === "WARN" || l.lvl === "ERROR");
  const firstDone = run.entries.find((e) => e.status === "done") || run.entries[0];

  async function stop() {
    setActionError(undefined);
    try {
      await api.cancelRun(run!.id);
    } catch (e) {
      setActionError(errMsg(e));
    }
  }
  async function restart() {
    setActionError(undefined);
    try {
      const r = await api.createRun({ ...run!.params, bench_id: run!.bench.id, models: run!.entries.map((e) => e.model), judge_id: run!.judge, threshold: run!.threshold });
      navigate(`/runs/${r.id}`);
    } catch (e) {
      setActionError(errMsg(e));
    }
  }

  const config = [
    { k: "Bộ bench", v: run.bench.name },
    { k: "Nguồn", v: run.source },
    { k: "Giám khảo", v: run.judge || "Chấm tự động" },
    { k: "Số mẫu mỗi thí sinh", v: String(run.entries[0]?.total || run.params.samples) },
    { k: "Luồng song song", v: String(run.params.concurrency) },
    { k: "Temperature", v: String(run.params.temperature) },
  ];

  return (
    <div className="page" style={{ gap: 20 }}>
      <div className="page-head">
        <div>
          <div className="row" style={{ gap: 12 }}>
            <h1 className="title">Lượt chạy R-{run.id}</h1>
            <Badge text={st.text} tone={st.tone} dot />
          </div>
          <p className="lead">{run.bench.name} · bắt đầu lúc {fmtClock(run.started_at || run.created_at)} · {etaText}</p>
        </div>
        <div className="row">
          {isRunning && <button type="button" className="btn btn-danger" onClick={stop}>Dừng lượt chạy</button>}
          {!isRunning && <button type="button" className="btn" onClick={restart}>Chạy lại</button>}
          {run.status === "done" && firstDone && (
            <Link className="btn btn-primary" to={`/results/${firstDone.id}`}>Xem chi tiết kết quả</Link>
          )}
        </div>
      </div>
      {actionError && <p role="alert" className="notice err">{actionError}</p>}
      {run.error && <p role="alert" className="notice err">{run.error}</p>}

      <section aria-label="Tiến độ" className="card stack" style={{ gap: 16 }}>
        <div className="card-head" style={{ alignItems: "baseline" }}>
          <div className="row" style={{ alignItems: "baseline", gap: 12 }}>
            <span className="num" style={{ fontSize: 34, fontWeight: 700 }}>{pct}%</span>
            <span className="muted">{doneAll} / {totalAll} mẫu</span>
          </div>
          <div className="row" style={{ gap: 16, fontSize: 13 }}>
            <span style={{ color: "var(--warn)", fontWeight: 600 }}>{run.warns} cảnh báo</span>
            <span style={{ color: "var(--err)", fontWeight: 600 }}>{run.errs} lỗi</span>
          </div>
        </div>
        <div role="progressbar" aria-label="Tiến độ tổng" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
          <Bar pct={pct} size="thick" />
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))", gap: 12 }}>
          {config.map((c) => (
            <div key={c.k} style={{ padding: "10px 12px", background: "var(--bg)", borderRadius: 8, minWidth: 0 }}>
              <div className="muted" style={{ fontSize: 12 }}>{c.k}</div>
              <div style={{ fontWeight: 600, fontSize: 13, marginTop: 2, overflowWrap: "anywhere" }}>{c.v}</div>
            </div>
          ))}
        </div>
      </section>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 16 }}>
        {run.entries.map((e) => {
          const ep = e.total ? Math.round((e.done / e.total) * 100) : 0;
          const est = RUN_STATUS[e.status];
          return (
            <div key={e.id} className="card stack" style={{ padding: "16px 18px", gap: 10 }}>
              <div className="card-head" style={{ alignItems: "baseline" }}>
                <span className="mono" style={{ fontWeight: 600, fontSize: 13 }}>{e.model}</span>
                <span className="muted num" style={{ fontSize: 13 }}>{e.done}/{e.total || "…"}</span>
              </div>
              <Bar pct={ep} size="thin" color={e.status === "failed" ? "var(--err)" : undefined} />
              <div className="card-head" style={{ fontSize: 13 }}>
                <span className="muted">{e.status === "done" ? "Điểm" : "Tỉ lệ đạt tạm thời"}</span>
                <span className="num" style={{ fontWeight: 700 }}>
                  {e.status === "done" ? fmtScore(e.score) : e.done ? `${fmtScore((e.passed / e.done) * 100)}%` : "—"}
                </span>
              </div>
              {e.status === "failed" && <Badge text={est.text} tone={est.tone} />}
            </div>
          );
        })}
      </div>

      <section aria-labelledby="log-h" className="log">
        <div className="log-head">
          <h2 id="log-h" className="log-title">Log trực tiếp <span>· {logs.length} dòng</span></h2>
          <div className="row" style={{ gap: 8 }}>
            {(["all", "warn"] as const).map((k) => (
              <button key={k} type="button" className="log-chip" aria-pressed={logFilter === k} onClick={() => setLogFilter(k)}>
                {k === "all" ? "Tất cả" : "Cảnh báo & lỗi"}
              </button>
            ))}
            <label className="log-toggle">
              <input type="checkbox" checked={autoscroll} onChange={() => setAutoscroll((a) => !a)} style={{ margin: 0, width: 16, height: 16 }} />
              Tự cuộn
            </label>
          </div>
        </div>
        <div ref={logEl} role="log" aria-label="Log lượt chạy" className="log-body">
          {shown.map((l) => (
            <div key={l.id} className="log-line">
              <span className="t">{fmtClock(l.t)}</span>
              <span className={`lvl lvl-${l.lvl}`}>{l.lvl}</span>
              <span className="msg">{l.msg}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
