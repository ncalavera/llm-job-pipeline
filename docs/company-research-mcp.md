# Optional web research with Parallel Search MCP

Use [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
in an interactive Claude Code session to find employers and read their official
careers pages before adding them to the pipeline. It provides `web_search` and
`web_fetch` with no Parallel account or API key. Anonymous search uses Fast mode;
the free endpoint has rate limits.

This is an optional tool for research you request in your coding agent. It does
not add a Python evidence source or change the daily pipeline's fetchers, scoring
or provider settings. Nightly discovery agents remain file-only, without MCP
access.

## Connect Claude Code

With [Claude Code installed](https://code.claude.com/docs/en/setup), run these
commands from the repository root:

```bash
claude mcp add --transport http --scope local parallel-search https://search.parallel.ai/mcp \
  --header "User-Agent: llm-job-pipeline/parallel-mcp"
claude mcp get parallel-search
```

Local scope keeps this connection private to you in this project. It does not
create a shared `.mcp.json` or modify anyone else's setup. If you already have a
server named `parallel-search`, inspect it with `claude mcp get parallel-search`
and keep your chosen configuration instead of adding it again.

Start a new Claude Code session in this directory, or restart an existing one.
Run `/mcp` to check that `parallel-search` is connected. The server exposes
`web_search` for finding pages and `web_fetch` for reading selected URLs. This
anonymous endpoint does not require a login or an Authorization header. See the
[Claude Code MCP guide](https://code.claude.com/docs/en/mcp) for connection
troubleshooting.

## Find employers, then verify their careers pages

Give the agent the field, location and other criteria you want to search for.
Keep your private profile, application materials and personal contact details
out of tool requests: send only the public search criteria needed for this task.
For example:

> Use Parallel's web_search to find up to three employers matching the field and
> region I supply. Prefer each employer's own website. Use web_fetch on their
> official careers pages. Return the employer name, website, careers URL, and a
> short supporting quote with its source URL for each. Mark missing information
> as unknown. Show me the shortlist before adding anything to the database.

Treat search results as leads. Check the fetched primary text before asserting
remote eligibility, visa sponsorship, location or other employer facts. A search
snippet or an absent statement does not establish those facts. If a page cannot
be fetched, leave the claim unverified and open the official page yourself.

For the first-run [onboarding gate](../.claude/commands/jobs-new.md#onboarding-only-on-an-empty-database),
use this research to build the shortlist. Keep the runbook's public ATS API
validation and user approval before inserting employers. For an individual
approved employer, follow [company mode in `/jobs-add`](../.claude/commands/jobs-add.md#mode-a--add-a-company-default):
confirm the careers URL, detect the ATS, confirm the proposed record and run its
test fetch. Research results alone do not configure a vacancy fetcher or save
company evidence to the database.

If the service is rate limited or unavailable, pause the research and use the
official careers page directly. Existing native ATS fetchers and the daily loop
continue to work without this MCP connection.

## Remove the connection

From the same repository root:

```bash
claude mcp remove parallel-search --scope local
```
