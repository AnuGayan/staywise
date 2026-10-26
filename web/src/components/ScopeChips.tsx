export interface ScopeChipsProps {
  scopes: readonly string[];
  /** Scopes to emphasise (e.g. newly requested ones). */
  highlight?: readonly string[];
  emptyLabel?: string;
}

export function ScopeChips({ scopes, highlight = [], emptyLabel = "—" }: ScopeChipsProps) {
  if (scopes.length === 0) return <span className="muted">{emptyLabel}</span>;
  const unique = [...new Set(scopes)];
  return (
    <ul className="chip-list">
      {unique.map((scope) => (
        <li key={scope} className={highlight.includes(scope) ? "chip chip-accent" : "chip"}>
          {scope}
        </li>
      ))}
    </ul>
  );
}
