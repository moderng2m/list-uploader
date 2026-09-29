export function ErrorLine({ error, onReload }: { error: string | null; onReload?: () => void }) {
  if (!error) return null;
  return (
    <p role="alert" className="error">
      {error}{" "}
      {onReload && (
        <button type="button" className="link" onClick={onReload}>
          Reload
        </button>
      )}
    </p>
  );
}
