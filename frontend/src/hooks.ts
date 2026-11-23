import { useCallback, useEffect, useRef, useState } from "react";
import { errMsg } from "./api";

/**
 * Tải dữ liệu và (tuỳ chọn) tự tải lại sau `pollMs`. `pollMs` có thể là hàm của dữ liệu
 * hiện tại, ví dụ chỉ poll nhanh khi còn nguồn đang đồng bộ.
 */
export function useApi<T>(load: () => Promise<T>, deps: unknown[], pollMs?: number | ((data: T | undefined) => number | undefined)) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    try {
      const d = await loadRef.current();
      setData(d);
      setError(undefined);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const ms = typeof pollMs === "function" ? pollMs(data) : pollMs;
  useEffect(() => {
    if (!ms) return;
    const t = setTimeout(() => void reload(), ms);
    return () => clearTimeout(t);
  }, [ms, data, error, reload]);

  return { data, error, loading, reload, setData };
}
