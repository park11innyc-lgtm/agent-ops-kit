# How the routing table is built (and kept honest)

`task_routes` in `settings_v1.json` says which model and effort to spawn a sub-agent on for each class of work.
It is a claim about the world, so it carries evidence, and the evidence has rules.

1. **Primary sources only.** Read the vendor's own model pages, effort docs and release notes, plus at most one
   independent benchmark aggregator. A search snippet is a lead, not a source.
2. **Start from raw rows.** Record each score next to the page it came from. Never copy a number from an earlier
   summary (including your own last report).
3. **Split verified from unverified.** The evidence file has two sections: facts read on a primary page, and
   claims you could not confirm (a 404, a snippet only, two pages that disagree). Unverified claims never change a route.
4. **Effort before model.** Try a stronger effort level on the current model before routing up a tier; it is often
   the cheaper lever.
5. **Cheap tiers only for checkable output.** A small model gets a task only when a machine can check its result
   (a SQL sweep, a grep, a listing). Judgment, visual and creative work never route down.
6. **Escalate on failure.** A failed or blocked step goes to the next model up the ladder. If the top fails,
   return a workaround plan, never a bare "blocked".
7. **Date it.** The evidence file is named with its date. Re-run the sweep when a new model ships; diff the new
   table against the old before adopting it.

Evidence file template: `reports/MODEL_ROUTING_EVIDENCE_<date>.md` with `Source:`, `Table:`, `Key facts:` and `Unverified:` lines.

**Outside verifier:** the vendor's published pages and benchmark tables, not the agent's own judgment.
