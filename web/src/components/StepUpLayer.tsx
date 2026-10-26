import type { ConciergeState } from "../state/types";
import { selectActiveStepUp } from "../state/selectors";
import { StepUpDialog, type OpenPopup } from "./StepUpDialog";

export interface StepUpLayerProps {
  state: ConciergeState;
  onDecline: (stepupId: string) => Promise<void>;
  openPopup?: OpenPopup;
}

/**
 * Shows the step-up dialog while a step-up is pending. The dialog disappears
 * only when the matching `stepup_resolved` event (or the turn ending) arrives.
 */
export function StepUpLayer({ state, onDecline, openPopup }: StepUpLayerProps) {
  const active = selectActiveStepUp(state);
  if (!active) return null;
  return (
    <StepUpDialog
      key={active.stepupId}
      stepUp={active}
      onDecline={onDecline}
      {...(openPopup ? { openPopup } : {})}
    />
  );
}
