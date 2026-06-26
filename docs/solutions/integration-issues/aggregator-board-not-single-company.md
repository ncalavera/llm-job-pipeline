---
title: Aggregator job sites belong as a board with real-employer attribution, not a single hand-added company
category: integration-issues
date: 2026-06-26
tags: [job-boards, aggregator, fetchers, org_override, cookie-banner, expired-postings, datadotorg]
component: fetchers / job-boards
symptom: "Vacancy card title and description don't match, apply link is dead, and many roles pile up under one wrong company"
root_cause: "An aggregator site (data.org) was hand-added as one company via /jobs-add vacancy; the page scrape captured the cookie-consent banner as the description and the link pointed at a posting that 301s to the jobs index"
resolution_type: new-fetcher
---

## Problem

A vacancy on the dashboard showed title "AI for Social Good Program Manager" under company `datadotorg`, but the description text was about a **Google.org** role, and the apply link was dead. Nine more roles were jammed under the same `datadotorg` pseudo-company.

## Symptoms

- Card title (from the listing) didn't match the summary/description (a different employer entirely).
- Apply link 301-redirected to `https://data.org/jobs/` (the index) instead of opening the role.
- `full_description` in the DB was cookie-consent boilerplate ("The technical storage or access is strictly necessary…").
- Many unrelated employers' roles all attributed to one company.

## Root cause

`data.org` is a WordPress **aggregator**: each posting links out to the real employer's ATS. It had been hand-added as a single company through `/jobs-add` (vacancy mode), so:

1. The detail-page scrape grabbed the cookie banner as the description instead of the job body.
2. The stored link was the data.org page, which 301s to `/jobs/` once the posting expires.
3. Every role was attributed to the board name, not the real employer.

## What didn't work

- Treating it as a normal company/ATS — aggregators have no single employer and their pages are cookie-walled and short-lived.

## Solution

Build the aggregator as a proper **board** with a dedicated fetcher (`datadotorg_wp`):

- List via the clean wp-json endpoint (`/wp-json/wp/v2/job`, newest-first) — never the cookie-walled HTML index.
- Parse each detail page for the **real employer** (here: the string after "About the organization" in `.c-sidebar__org`), the **external apply URL** (the "Apply Now" anchor to a non-data.org domain), salary/location/deadline, and the structured description container (`.c-single-job__details`) — never the page-wide text.
- Attribute via `org_override = <real employer>` so roles land under the real company, and store the external apply URL so the link works.
- Skip expired postings: if the detail GET resolves to a URL ending in `/jobs` (redirect to index), drop it.

Wire it through `config/defaults.toml` `[boards.datadotorg]`, the dispatch in `fetch_vacancies.py`, and the daily `JOB_BOARDS` set. The existing save layer (`save_board_vacancies`) already does cookie-banner gating (`_gate_description`), employer-canonicalization, and inactive-company skipping — reuse it rather than reinventing.

## Why this works

The board path attributes to the real employer and stores the working apply link, fixing both the mismatch and the dead link at the source. Pulling the description from a specific structured container (not page text) sidesteps the cookie banner. The redirect check stops expired postings from being re-added empty under the board name.

## Prevention

- When a "company" is really an aggregator (its postings link out to other employers' ATS), add it as a board, not a single company. Tell on sight: a `/jobs/<slug>/` WordPress path, a `/jobs/feed/` RSS, or postings whose "Apply" goes to a different domain.
- Prefer a site's JSON/RSS feed (wp-json, `/feed/`) over scraping the rendered HTML — feeds skip the cookie wall.
- Always pull descriptions from a known structured container; if a scrape yields consent-banner text, the selector is wrong.
- Guard against expired postings that 301 to a listing index before saving them.
