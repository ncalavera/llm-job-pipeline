import { createClient } from "@supabase/supabase-js";

let _supabase = null;

export function getSupabase() {
  if (!_supabase) {
    _supabase = createClient(
      process.env.SUPABASE_URL.trim(),
      process.env.SUPABASE_SERVICE_ROLE_KEY.trim(),
    );
  }
  return _supabase;
}

export function getSupabaseUrlPrefix() {
  const url = (process.env.SUPABASE_URL || "").trim();
  try {
    return new URL(url).hostname.split(".")[0];
  } catch {
    return null;
  }
}

export function validateConfig() {
  const warnings = [];
  const expected = (process.env.EXPECTED_SUPABASE_REF || "").trim();
  if (!expected) return warnings;
  const actual = getSupabaseUrlPrefix();
  if (actual && actual !== expected) {
    warnings.push(
      `SUPABASE_URL prefix "${actual}" does not match expected "${expected}"`,
    );
  }
  return warnings;
}
