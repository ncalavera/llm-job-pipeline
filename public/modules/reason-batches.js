import { screenRequirements } from './derive.js';

export const REASON_GROUPS = [
  ['eligibility', 'Location or language'],
  ['expertise', 'Experience or qualifications'],
];

// These are review prompts, not rejection verdicts. Unknown/preferred/stale
// requirements never justify a batch. Eligibility takes precedence for one group per role.
export function reasonBatch(g, fingerprint) {
  // No score gate: a quoted REQUIRED condition the profile may not meet is a
  // reason to group a role whatever it scores. The old "< 40" rule came from
  // the retired "review low scores by reason" strip, and under the review
  // screen's default score band it left every batch empty.
  if (g.screening_state !== 'ready' || !fingerprint ||
      g.screening_fingerprint !== `${g.posting_fingerprint}:${fingerprint}`) return null;
  const requirements = screenRequirements(g);
  const comparisons = g.screening?.profile_comparison;
  const concerns = (Array.isArray(comparisons) ? comparisons : []).flatMap(c => {
    if (!c) return [];
    const r = Number.isInteger(c.requirement) && requirements[c.requirement];
    if (c.finding !== 'possible_conflict' || !r || r.strength !== 'required' ||
        typeof r.quote !== 'string' || !r.quote.trim()) return [];
    return [{kind:r.kind, quote:r.quote, note:c.note || r.value || ''}];
  });
  for (const [key] of REASON_GROUPS) {
    const kinds = key === 'eligibility' ? ['location','language','authorisation'] : ['skill','domain','education','experience'];
    const reasons = concerns.filter(r => kinds.includes(r.kind));
    if (reasons.length) return {key, reasons};
  }
  return null;
}
