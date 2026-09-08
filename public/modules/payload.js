// Only long detail text is deferred. IDs, statuses, summaries and filter facts stay.
export const DETAIL_FIELDS = {
  vacancy: ['full_description', 'llm_reasoning'],
  archive: ['full_description', 'llm_reasoning', 'llm_summary', 'snippet'],
  company: ['description', 'executive_summary', 'mission_verdict', 'fit_risks', 'fit_strengths', 'fit_approach', 'experience_reasoning', 'fit_evidence'],
};

export function compactRecord(record, kind) {
  const copy = { ...record, _detailKind: kind };
  for (const key of DETAIL_FIELDS[kind]) delete copy[key];
  return copy;
}

export function compactSnapshot(payload) {
  return { ...payload,
    groups: (payload.groups || []).map(r => compactRecord(r, 'vacancy')),
    archived_groups: (payload.archived_groups || []).map(r => compactRecord(r, 'archive')),
    companies: (payload.companies || []).map(r => compactRecord(r, 'company')),
  };
}
