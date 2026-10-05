---
name: worktree-overlap
description: Detect cross-worktree merge collisions BEFORE they happen - which active worktree branches changed the same files (and will conflict when both fold back into the main branch). The worktree-model replacement for edit-time file locks, since each worktree edits its own physical copies and the real collision is at merge time. Run before starting a worktree, before merging, or to decide merge order.
---

Show which worktrees will collide at merge time. Arguments: $ARGUMENTS (`--json` optional).

Reads every worktree branch from the shared git object store and reports pairwise file
overlap (committed divergence from the main branch, plus uncommitted edits). This is the
early-warning the per-file lock system can't give in the worktree model: file locks guard
one shared checkout, but worktrees edit separate physical copies, so collisions only surface
at merge — this surfaces them first.

### Steps
1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/worktree_overlap.py" $ARGUMENTS`.
2. Show the output. Interpret:
   - **No overlaps** → merges will be conflict-free; merge in any order.
   - **Overlaps** → real merge conflicts coming. Either:
     - merge the overlapping worktrees **sequentially**, rebasing each on the prior, or
     - **re-scope** one worktree so the two stop touching the same files (one concern per
       worktree).
   - **Overlap with the main branch's own uncommitted changes** → commit or clean the main
     checkout first.

### When to run
- **Before** spinning up a new worktree — pick a scope that doesn't overlap active ones.
- **Before** merging a worktree back — decide the merge order.
- Periodically during parallel work — catch divergence early while it's cheap to re-scope.

### Pairs with
- `/coord:adjacency` (the filtered, addressed version of this same signal — notifies the
  specific peers you collide with, rather than a full pairwise report)
- `/coord:closeout branches` (confirm a branch is genuinely abandoned, not merged, before
  acting on it)
