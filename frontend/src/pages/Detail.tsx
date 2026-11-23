import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { fmtDelta, fmtDuration, fmtScore, fmtWhen, RUN_STATUS } from "../format";
import { useApi } from "../hooks";
import type { SampleRow } from "../types";
import { Badge, Bar, Chips, ErrorNotice, ICONS, Icon, Kpi, Loading } from "../ui";

const SCOLS = "64px 110px minmax(0, 1fr) 64px 76px 20px";

export default function DetailPage() {
  const id = Number(useParams().id);
  const navigate = useNavigate();
  const detail = useApi(() => api.result(id), [id]);
  const [filter, setFilter] = useState<"all" | "pass" | "fail">("all");
  const samples = useApi(() => api.samples(id, filter), [id, filter]);
  const [open, setOpen] = useState<number | null>(null);

  if (detail.loading) return <Loading />;
  if (detail.error || !detail.data) return <ErrorNotice error={detail.error || "Không có dữ liệu"} retry={detail.reload} />;
  const d = detail.data;
  const st = RUN_STATUS[d.status];
  const failed = d.status === "failed";
  const deltaColor = d.delta == null ? "var(--text)" : d.delta >= 0 ? "var(--ok)" : "var(--err)";

  const meta = [
    { k: "Nguồn", v: d.source },
    { k: "Giám khảo", v: d.judge || "Chấm tự động" },
    { k: "Rubric", v: d.bench.judge_required ? d.bench.rubric_path || "Rubric mặc định" : d.bench.scoring },
    { k: "Temperature", v: String(d.params.temperature) },
    { k: "Số mẫu", v: String(d.total) },
    { k: "Thời điểm", v: fmtWhen(d.when) },
    { k: "Thời lượng", v: fmtDuration(d.duration_s) },
  ];

  const rerun = () =>
    navigate("/runs/new", { state: { bench_id: d.bench.id, models: [d.model], judge_id: d.judge, threshold: d.threshold, ...d.params, samples: String(d.params.samples) } });

  return (
    <div className="page" style={{ gap: 22 }}>
      <div className="crumbs">
        <Link className="link" to="/">Kết quả</Link>
        <span aria-hidden="true">/</span><span className="mono">R-{d.id}</span>
      </div>

      <div className="page-head">
        <div className="row" style={{ gap: 12 }}>
          <h1 className="title"><span className="mono" style={{ fontSize: 24 }}>{d.model}</span> · {d.bench.name}</h1>
          <Badge text={st.text} tone={st.tone} />
        </div>
        <div className="row">
          <a className="btn" href={api.exportUrl(d.id)} download>Tải kết quả (JSON)</a>
          <button type="button" className="btn btn-primary" onClick={rerun}>Chạy lại cấu hình này</button>
        </div>
      </div>

      <div className="chips">
        {meta.map((m) => (
          <div key={m.k} style={{ padding: "6px 12px", background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13 }}>
            <span className="muted">{m.k}</span> <strong style={{ fontWeight: 600 }}>{m.v}</strong>
          </div>
        ))}
      </div>

      {failed && (
        <div role="alert" className="card stack" style={{ background: "var(--err-bg)", borderColor: "var(--err-border)", color: "var(--err-ink)", gap: 10 }}>
          <strong style={{ fontSize: 15 }}>Lượt chạy thất bại, không có điểm</strong>
          <span>{d.error || "Không rõ nguyên nhân."} {d.done}/{d.total} mẫu đã chạy được giữ trong log.</span>
          {d.error_logs.length > 0 && (
            <div className="code" style={{ whiteSpace: "normal" }}>
              {d.error_logs.map((l, i) => <div key={i}><span className={`lvl-${l.lvl}`}>{l.lvl.padEnd(5, " ")}</span> {l.msg}</div>)}
            </div>
          )}
        </div>
      )}

      {!failed && (
        <>
          <div className="kpis">
            <Kpi label="Điểm tổng" value={fmtScore(d.score)} sub={d.bench.judge_required ? "thang 100, quy đổi từ điểm giám khảo" : "tỉ lệ đúng (%)"} />
            <Kpi label="Số mẫu đạt" value={`${d.passed}/${d.total}`} sub={d.bench.judge_required ? `ngưỡng đạt ≥ ${d.threshold}/10` : d.bench.scoring} />
            <Kpi label="So với lần chạy trước" value={fmtDelta(d.delta)} color={deltaColor}
              sub={d.previous ? `so với R-${d.previous.id} (${fmtScore(d.previous.score)})` : "chưa có lần chạy trước"} />
            <Kpi label="Cần xem lại" value={d.review} color={d.review > 0 ? "var(--weak-ink)" : undefined} sub="mẫu chấm lỗi hoặc quá thời gian" />
          </div>

          <div className="pair">
            <section aria-labelledby="cat-h" className="card">
              <h2 id="cat-h" className="card-title" style={{ marginBottom: 14 }}>Điểm theo nhóm năng lực</h2>
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {d.categories.map((c) => {
                  const weak = c.score < 65;
                  return (
                    <div key={c.name} style={{ display: "grid", gridTemplateColumns: "110px minmax(0, 1fr) 80px", gap: 12, alignItems: "center" }}>
                      <span style={{ fontSize: 13 }}>{c.name}</span>
                      <Bar pct={c.score} color={weak ? "var(--weak)" : undefined} />
                      <span className="right num" style={{ fontSize: 13, fontWeight: 700 }}>
                        {fmtScore(c.score)}{weak && <span style={{ fontWeight: 500, color: "var(--weak-ink)" }}> · yếu</span>}
                      </span>
                    </div>
                  );
                })}
                {d.categories.length === 0 && <p className="muted" style={{ margin: 0 }}>Không có dữ liệu nhóm.</p>}
              </div>
            </section>
            <section aria-labelledby="cmp-h" className="card">
              <h2 id="cmp-h" className="card-title" style={{ marginBottom: 14 }}>So với thí sinh khác cùng bộ bench</h2>
              <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                {d.comparison.map((c) => {
                  const me = c.model === d.model;
                  return (
                    <div key={c.model} style={{ display: "grid", gridTemplateColumns: "130px minmax(0, 1fr) 48px", gap: 12, alignItems: "center" }}>
                      <span className="mono ellipsis" style={{ fontSize: 12.5, fontWeight: me ? 700 : 500 }}>{c.model}</span>
                      <Bar pct={c.score} opacity={me ? 1 : 0.35} />
                      <span className="right num" style={{ fontSize: 13, fontWeight: me ? 700 : 500 }}>{fmtScore(c.score)}</span>
                    </div>
                  );
                })}
              </div>
            </section>
          </div>
        </>
      )}

      <section aria-labelledby="smp-h" className="card flush-bottom">
        <div className="card-head">
          <h2 id="smp-h" className="card-title">Chi tiết từng mẫu</h2>
          <Chips label="Lọc mẫu" value={filter} onChange={(f) => { setFilter(f); setOpen(null); }}
            items={[
              { key: "all", label: `Tất cả (${samples.data?.counts.all ?? "…"})` },
              { key: "pass", label: `Đạt (${samples.data?.counts.pass ?? "…"})` },
              { key: "fail", label: `Trượt (${samples.data?.counts.fail ?? "…"})` },
            ]} />
        </div>
        <div className="table-scroll">
          <div style={{ minWidth: 760 }}>
            <div className="trow thead" style={{ gridTemplateColumns: SCOLS }}>
              <span>Mẫu</span><span>Nhóm</span><span>Đề bài</span><span className="right">Điểm</span><span>Kết luận</span><span />
            </div>
            {samples.loading && !samples.data && <Loading />}
            {samples.data?.items.map((x) => (
              <SampleItem key={x.id} x={x} open={open === x.id} onToggle={() => setOpen(open === x.id ? null : x.id)}
                judgeTitle={d.bench.judge_required ? `Nhận xét của giám khảo · ${d.judge || ""}` : "Kết quả chấm tự động"} />
            ))}
            {samples.data?.items.length === 0 && <p className="muted" style={{ margin: 12 }}>Không có mẫu nào.</p>}
          </div>
        </div>
      </section>
    </div>
  );
}

function SampleItem({ x, open, onToggle, judgeTitle }: { x: SampleRow; open: boolean; onToggle: () => void; judgeTitle: string }) {
  const tone = x.needs_review ? "warn" : x.passed ? "ok" : "err";
  const verdict = x.needs_review ? "Xem lại" : x.passed ? "Đạt" : "Trượt";
  const box = { background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 10, padding: 14 } as const;
  const label = { fontSize: 12, fontWeight: 600, color: "var(--muted)", marginBottom: 6 } as const;
  return (
    <div style={{ borderBottom: "1px solid var(--border-soft)" }}>
      <button type="button" className="trow" aria-expanded={open} onClick={onToggle}
        style={{ gridTemplateColumns: SCOLS, minHeight: 52, borderBottom: 0, background: open ? "var(--surface-sunken)" : undefined }}>
        <span className="mono muted" style={{ fontSize: 12.5 }}>#{x.sample_id}</span>
        <span style={{ fontSize: 13 }}>{x.category}</span>
        <span className="ellipsis">{x.prompt}</span>
        <span className="right num" style={{ fontWeight: 700 }}>{x.raw_score}</span>
        <span><Badge text={verdict} tone={tone} /></span>
        <Icon d={open ? ICONS.up : ICONS.down} color="var(--muted)" />
      </button>
      {open && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 12, padding: "6px 12px 20px", background: "var(--surface-sunken)" }}>
          <div style={box}><div style={label}>Đề bài</div><div style={{ whiteSpace: "pre-wrap" }}>{x.prompt}</div></div>
          <div style={box}><div style={label}>Trả lời của thí sinh · {x.tokens} tok · {(x.latency_ms / 1000).toFixed(1).replace(".", ",")}s</div><div style={{ whiteSpace: "pre-wrap" }}>{x.output}</div></div>
          <div style={box}><div style={label}>Đáp án / tiêu chí tham chiếu</div><div style={{ whiteSpace: "pre-wrap" }}>{x.reference || "—"}</div></div>
          <div className={`tone-${tone}`} style={{ ...box, border: "1px solid transparent" }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, fontWeight: 600, marginBottom: 6 }}><span>{judgeTitle}</span><span>{x.raw_score}</span></div>
            <div style={{ whiteSpace: "pre-wrap", color: "var(--text)" }}>{x.note || (x.passed ? "Khớp tham chiếu." : "—")}</div>
          </div>
        </div>
      )}
    </div>
  );
}
