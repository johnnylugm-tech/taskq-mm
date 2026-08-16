// Bug-hunt workflow — Phase 1 of 2 (review-only).
//
// Flow:
//   1. Build / refresh the CRG graph for the main source tree.
//   2. Survey high-criticality flows + communities the CRG surfaces.
//   3. Static analysis pass: large functions, dead-code candidates,
//      suspiciously-cyclic imports.
//   4. Cross-reference each CRG finding against the actual source
//      text (read-only) — no edits.
//   5. Synthesise a single markdown bug report at 08-config/bug_report.md.
//
// Scope is fixed to 03-development/src/taskq_api (production code only);
// tests, migrations and tooling are excluded.
//
// Run with: `claude /workflow bug_hunt_workflow.js`

export const meta = {
    name: 'bug-hunt-crg',
    description: 'CRG-guided adversarial bug hunt across the main source tree.',
    phases: [
        { title: 'Graph build' },
        { title: 'CRG survey' },
        { title: 'Static analysis' },
        { title: 'Targeted bug hunt' },
        { title: 'Synthesis' },
    ],
};

phase('Graph build');
log('Building / refreshing the CRG knowledge graph for 03-development/src/taskq_api');
const graphStats = await agent(
    'Run `mcp__code-review-graph__build_or_update_graph_tool` with repo_root=/Users/johnny/projects/taskq-mm, postprocess="full". ' +
    'Then call `mcp__code-review-graph__list_graph_stats_tool` with the same repo_root. ' +
    'Report back: (a) total nodes / edges / files, (b) any files the parser refused to ingest, ' +
    '(c) whether Base.metadata + task_tags_table declarations were detected as model artefacts.',
    { label: 'build graph', phase: 'Graph build', agentType: 'general-purpose' }
);
log(`Graph stats: ${graphStats}`);

phase('CRG survey');
log('Asking CRG which flows + communities are highest-value to inspect');
const survey = await agent(
    'Use these CRG tools (all take repo_root=/Users/johnny/projects/taskq-mm):\n' +
    '  - `mcp__code-review-graph__list_flows_tool` sort_by=criticality limit=15\n' +
    '  - `mcp__code-review-graph__list_communities_tool` sort_by=size detail_level="minimal"\n' +
    '  - `mcp__code-review-graph__get_architecture_overview_tool`\n' +
    '  - `mcp__code-review-graph__get_minimal_context_tool` task="adversarial bug hunt"\n' +
    'For each top flow, run `mcp__code-review-graph__get_flow_tool` include_source=true.\n' +
    'Return a JSON object with keys: high_criticality_flows[], interesting_communities[], ' +
    'red_flags[] (anything the overview highlights as suspicious).',
    { label: 'CRG survey', phase: 'CRG survey', agentType: 'general-purpose' }
);

phase('Static analysis');
log('Hardening the survey with static analysis');
const staticFindings = await agent(
    'Static sweep of /Users/johnny/projects/taskq-mm/03-development/src/taskq_api with these tools:\n' +
    '  - `mcp__code-review-graph__find_large_functions_tool` min_lines=40\n' +
    '  - `mcp__code-review-graph__semantic_search_nodes_tool` for: "race condition", "transient", "off-by-one", "TOCTOU", "leak", "timeout".\n' +
    '  - `mcp__code-review-graph__refactor_tool` mode=dead_code file_pattern="03-development/src/" to surface unused symbols.\n' +
    '  - `mcp__code-review-graph__detect_changes_tool` base=HEAD~1 to see what the latest commit touched.\n' +
    'For every finding, capture: file:line, snippet (≤6 lines), and a one-line hypothesis. ' +
    'Do NOT propose fixes — only flag. Output as JSON array.',
    { label: 'static analysis', phase: 'Static analysis', agentType: 'general-purpose' }
);

phase('Targeted bug hunt');
log('CRG-guided deep dive into the highest-risk modules');
const bugHunt = await agent(
    'You are an adversarial reviewer. Re-read the following files in /Users/johnny/projects/taskq-mm/03-development/src/taskq_api:\n' +
    '  - service/runner.py        (async subprocess executor — NFR-03 / FR-08)\n' +
    '  - service/tasks.py         (business rules — FR-01)\n' +
    '  - service/auth.py          (authn/authz — FR-03 / FR-04 / R4)\n' +
    '  - service/ratelimit.py     (per-token bucket — FR-05 / R12)\n' +
    '  - repository/session.py    (transaction boundary — FR-06 / NFR-03)\n' +
    '  - repository/task_repo.py  (cursor pagination — FR-01 / NFR-01)\n' +
    '  - repository/rate_repo.py  (row-level lock — FR-05 / R12)\n' +
    '  - repository/key_repo.py   (hashing — FR-03 / NFR-02)\n' +
    '  - api/tasks.py             (handler glue — NFR-11)\n' +
    '  - api/health.py            (migration check — FR-09)\n' +
    '  - errors.py                (problem+json — FR-10 / NFR-04)\n' +
    'For each file, list *concrete* bugs or hardening gaps with: file:line, the offending snippet, ' +
    'why it is wrong, and a reproducer or test case that would catch it. Skip style nits. ' +
    'Score each finding as P0 (data loss / auth / security) / P1 (correctness) / P2 (hardening). ' +
    'Output as a markdown report — write to /Users/johnny/projects/taskq-mm/08-config/bug_report.md.\n' +
    'Cross-reference the CRG findings you already received in this session.',
    { label: 'bug hunt', phase: 'Targeted bug hunt', agentType: 'general-purpose' }
);

phase('Synthesis');
log('Combining CRG + static + manual findings into a final report');
const synthesis = await agent(
    'You received three earlier reports this session:\n' +
    '  1. graph_stats    (file paths etc.)\n' +
    '  2. survey         (high-criticality flows, communities, red_flags)\n' +
    '  3. static_findings (file:line + snippet + hypothesis)\n' +
    '  4. The markdown report written by the bug-hunt agent.\n' +
    'Merge them into a final report at /Users/johnny/projects/taskq-mm/08-config/bug_report.md:\n' +
    '  - Keep the bug-hunt markdown body intact.\n' +
    '  - Prepend a "## CRG navigation" section summarising the most critical flows / communities the graph flagged.\n' +
    '  - Append a "## Coverage map" table: row per finding → file:line → CRG node id → category.\n' +
    '  - End with a one-paragraph verdict (PASS / NEEDS-FIX / FAIL) and a numbered recommendation list.\n' +
    'Return only the final path + byte size of the report.',
    { label: 'synthesis', phase: 'Synthesis', agentType: 'general-purpose' }
);

return { report: '08-config/bug_report.md', phases: 5 };