import { useEffect, useState } from "react";

/** Load an admin setting, then replace it with each change's response. */
export function useVersioned<T>(load: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    load().then(
      (d) => !cancelled && setData(d),
      (e: unknown) => !cancelled && setError(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      cancelled = true;
    };
  }, [tick]);

  async function change(action: () => Promise<T>): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      setData(await action());
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  return { data, error, busy, change, reload: () => setTick((t) => t + 1) };
}
