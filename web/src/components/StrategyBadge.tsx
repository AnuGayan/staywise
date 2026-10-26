import type { TokenStrategy } from "../api/types";
import { STRATEGY_INFO } from "../lib/format";

export function StrategyBadge({ strategy }: { strategy: TokenStrategy }) {
  const info = STRATEGY_INFO[strategy];
  return (
    <span className={`badge badge-${strategy}`} title={info.description}>
      <span className="badge-dot" aria-hidden="true" />
      {info.label}
    </span>
  );
}
