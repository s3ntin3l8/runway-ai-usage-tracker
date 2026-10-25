// How a billing model reads in the UI. The account dialog and the wizard
// offer the same three models, so they read them from one place.

import type { BillingType } from '@/api/types';

/** The full label of a billing model, as the selects show it. */
export const BILLING_TYPE_LABELS: Record<BillingType, string> = {
  unknown: 'Unknown · show cost',
  subscription: 'Subscription · show estimated cost',
  pay_as_you_go: 'Pay as you go · show reported cost when available',
};

/** A short name for a detected model ("Detected: Subscription"). */
export const BILLING_TYPE_SHORT_LABELS: Record<BillingType, string> = {
  unknown: 'Unknown',
  subscription: 'Subscription',
  pay_as_you_go: 'Pay as you go',
};
