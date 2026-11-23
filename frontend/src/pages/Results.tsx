import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { fmtDelta, fmtScore, fmtWhen, RUN_STATUS } from "../format";
import { useApi } from "../hooks";
import { Badge, Bar, Chips, ErrorNotice, Kpi, Loading, PageHead } from "../ui";

const COLS = "84px minmax(0, 1.3fr) minmax(0, 1.3fr) minmax(0, 1fr) 64px 64px 104px 112px";

export default function ResultsPage() {
  const navigate = useNavigate();
  const stats = useApi(api.stats, []);
  const benches = useApi(api.benches, []);
  const [lbBench, setLbBench] = useState<number>();
  const [filter, setFilter] = useState<number | "all">("all");

  useEffect(() => {
    if (lbBench == null && benches.data?.length) setLbBench(benches.data[0].id);
  }, [benches.data, lbBench]);

  const board = useApi(() => (lbBench ? api.leaderboard(lbBench) : Promise.resolve([])), [lbBench]);
  const results = useApi(() => api.results(filter === "all" ? undefined : filter), [filter], 10000);

  const s = stats.data;
  const lbB = benches.data?.find((b) => b.id === lbBench);

  if (benches.data && benches.data.length === 0) {
    return (
      <div className="page">
        <PageHead title="Kết quả benchmark" />
        <div className="card empty">
          <h2>Chưa có bộ bench nào</h2>
          <p>Kết nối một repo chứa file bench.yaml để bắt đầu.</p>
          <Link className="btn btn-primary" to="/sources">Kết nối repo bench</Link>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      <PageHead title="Kết quả benchmark" lead="Tổng hợp mọi lượt chạy. Chọn một dòng để xem chi tiết từng mẫu và nhận xét của giám khảo.">
        <Link className="btn btn-primary" to="/runs/new">Chạy benchmark mới</Link>
      </PageHead>

      {stats.error && <ErrorNotice error={stats.error} retry={stats.reload} />}
      <div className="kpis">
        <Kpi label="Lượt chạy" value={s?.entries ?? "—"} sub={s ? `${s.entries_done} hoàn tất` : ""} />
        <Kpi label="Bộ bench" value={s?.benches ?? "—"} sub={s ? `từ ${s.sources_ok} repo · ${s.benches_with_judge} cần giám khảo` : ""} />
        <Kpi label="Thí sinh đã đánh giá" value={s?.models_evaluated ?? "—"} sub="mô hình khác nhau" />
        <Kpi label="Lượt lỗi" value={s?.entries_failed ?? "—"} sub="cần kiểm tra và chạy lại" />
      </div>

      <section aria-labelledby="lb-h" className="card">
        <div className="card-head" style={{ alignItems: "baseline" }}>
          <h2 id="lb-h" className="card-title">Bảng xếp hạng theo bộ bench</h2>
          {lbB && <span className="muted" style={{ fontSize: 13 }}>{lbB.scoring} · điểm cao nhất của mỗi thí sinh</span>}
        </div>
        <div style={{ marginTop: 14 }}>
          {benches.data && lbBench != null && (
            <Chips label="Chọn bộ bench" items={benches.data.map((b) => ({ key: b.id, label: b.name }))} value={lbBench} onChange={setLbBench} />
          )}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 16 }}>
          {board.loading && !board.data ? <Loading /> : null}
          {board.data?.map((r, i) => (
            <button key={r.entry_id} type="button" className="trow" onClick={() => navigate(`/results/${r.entry_id}`)}
              style={{ gridTemplateColumns: "24px minmax(150px, 240px) minmax(0, 1fr) 56px", gap: 14, minHeight: 52, borderBottom: 0, borderRadius: 8 }}>
              <span className="muted num" style={{ fontSize: 13 }}>{i + 1}</span>
              <span style={{ minWidth: 0 }}>
                <span className="mono" style={{ display: "block", fontWeight: 600, fontSize: 13 }}>{r.model}</span>
                <span className="muted" style={{ display: "block", fontSize: 12 }}>
                  {r.judge ? `Giám khảo ${r.judge}` : "Chấm tự động"} · R-{r.entry_id} · {fmtWhen(r.when)}
                </span>
              </span>
              <Bar pct={r.score} opacity={i === 0 ? 1 : 0.4} />
              <span className="right num" style={{ fontWeight: 700 }}>{fmtScore(r.score)}</span>
            </button>
          ))}
          {board.data?.length === 0 && <p className="muted" style={{ margin: "8px 10px" }}>Chưa có lượt chạy hoàn tất cho bộ bench này.</p>}
        </div>
      </section>

      <section aria-labelledby="runs-h" className="card flush-bottom">
        <div className="card-head">
          <h2 id="runs-h" className="card-title">Lịch sử lượt chạy</h2>
          {benches.data && (
            <Chips<number | "all"> label="Lọc theo bộ bench" value={filter} onChange={setFilter}
              items={[{ key: "all", label: "Tất cả" }, ...benches.data.map((b) => ({ key: b.id, label: b.name }))]} />
          )}
        </div>
        {results.error && <ErrorNotice error={results.error} retry={results.reload} />}
        <div className="table-scroll">
          <div style={{ minWidth: 940 }}>
            <div className="trow thead" style={{ gridTemplateColumns: COLS }}>
              <span>Mã</span><span>Bộ bench</span><span>Thí sinh</span><span>Giám khảo</span>
              <span className="right">Điểm</span><span className="right">Δ</span><span>Trạng thái</span><span>Thời điểm</span>
            </div>
            {results.loading && !results.data && <Loading />}
            {results.data?.map((r) => {
              const st = RUN_STATUS[r.status];
              const deltaColor = r.delta == null ? "var(--muted)" : r.delta >= 0 ? "var(--ok)" : "var(--err)";
              const go = () => navigate(r.status === "running" || r.status === "queued" ? `/runs/${r.run_id}` : `/results/${r.id}`);
              return (
                <button key={r.id} type="button" className="trow" style={{ gridTemplateColumns: COLS }} onClick={go}>
                  <span className="mono muted" style={{ fontSize: 12.5 }}>R-{r.id}</span>
                  <span style={{ fontWeight: 500 }}>{r.bench.name}</span>
                  <span className="mono" style={{ fontSize: 13 }}>{r.model}</span>
                  <span className="mono" style={{ fontSize: 13, color: "var(--text-2)" }}>{r.judge || "—"}</span>
                  <span className="right num" style={{ fontWeight: 700 }}>{fmtScore(r.score)}</span>
                  <span className="right num" style={{ fontWeight: 600, fontSize: 13, color: deltaColor }}>{fmtDelta(r.delta)}</span>
                  <span><Badge text={st.text} tone={st.tone} /></span>
                  <span className="muted num" style={{ fontSize: 13 }}>{fmtWhen(r.when)}</span>
                </button>
              );
            })}
            {results.data?.length === 0 && <p className="muted" style={{ margin: 12 }}>Chưa có lượt chạy nào.</p>}
          </div>
        </div>
      </section>
    </div>
  );
}
