export interface SuggestedPrompt {
  id: string;
  step: number;
  title: string;
  text: string;
}

/** The live-demo script, in order. */
export const SUGGESTED_PROMPTS: readonly SuggestedPrompt[] = [
  {
    id: "search",
    step: 1,
    title: "Search rooms",
    text: "Find me a sea-view room in Galle for 14–16 November under $200",
  },
  {
    id: "book",
    step: 2,
    title: "Book & pay deposit",
    text: "Book the Lighthouse Deluxe and pay the $90 deposit with my saved card",
  },
  {
    id: "front-desk",
    step: 3,
    title: "Message front desk",
    text: "Let the front desk know I'll arrive around 11pm",
  },
  {
    id: "upgrade",
    step: 4,
    title: "Upgrade & prepay",
    text: "Upgrade me to the Ocean Suite and prepay the full $1,240 now",
  },
  {
    id: "loyalty",
    step: 5,
    title: "Loyalty balance",
    text: "What's my loyalty points balance?",
  },
];
