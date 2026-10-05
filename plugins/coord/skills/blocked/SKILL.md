---
name: blocked
description: Something is blocking you that you did not create — a locked file, a cross-worktree collision, uncommitted work you did not write. Identify the responsible session, determine whether it is still alive, and act accordingly - ping a live owner, adopt what a dead one abandoned, escalate what is not yours to resolve.
---

# /coord:blocked — who owns this, are they still here, and what may I do about it

Arguments: `$ARGUMENTS` — optionally a path (or several). With no argument, attributes the
whole uncommitted working tree plus your current branch.

## When to use

You hit a roadblock and the cause is **not yours**:

- "FILE LOCKED by another session" from the edit-time guard
- a cross-worktree collision — another branch is editing files you are editing
- uncommitted work in your checkout that you did not write
- a gate blocking on files you don't recognise

Do **not** use it to decide whether to do your own work. It answers exactly one question:
*whose is this, and does that person still exist?*

## Steps

1. **Resolve.**
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/blocker_owner.py" --path <path>     # a specific file
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/blocker_owner.py" --dirty --branch "$(git rev-parse --abbrev-ref HEAD)"
   ```
   Each verdict carries `owner`, `liveness`, `evidence` (the source that answered), and a
   `recommendedAction`.

2. **Act on the recommendation. Do not improvise past it.**

   | verdict | do |
   |---|---|
   | `ping` | `/coord:ask-lock <path> "<why>"` for a lock, `/coord:msg <peer> "..."` otherwise. Ask them to act on **their own** state. Then carry on with something else — do not block waiting. |
   | `inherit` | The owner is gone and the transfer is reversible (a lock). Take it and say so in your next message to the user. |
   | `propose-to-operator` | The owner is gone but the remedy touches their git state or unpushed work. **Surface it; do not act.** |

3. **Before proposing anything about an abandoned branch**, confirm it is genuinely
   abandoned rather than merged:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/closeout_ledger.py" branches      # splits at-risk (exists nowhere else) from landed
   ```
   A squash-merge workflow deletes the remote branch on merge, so a landed branch's commits
   can look unpushed forever. Ancestry and patch-id comparisons are both the wrong test here.

## The rule this encodes

Liveness is an **authority** question, not a convenience. A session must never absorb a
peer's work, because the absorbed session then carries two objectives and will rightly
prioritise its own — so the absorbed one loses the only agent advocating for it. A dead
session has no advocate, so that harm cannot occur, and only then is adoption legitimate.

Two things follow, and neither is negotiable:

- **`unknown` liveness counts as alive.** A process id means nothing on a machine that did
  not record it; probing it there tests an unrelated local process and answers confidently
  wrong in either direction. Inheriting a live session's work is worse than staying
  blocked, so uncertainty resolves toward *they are still here*.
- **Inheritance changes who may act, never what may be auto-acted.** Their lock is a claim
  and adoptable. Their branch, commits, and uncommitted work are not — regardless of how
  certainly dead they are.

## Do not

- Delete a lock by hand. A lock whose owning process is alive is never stale.
- Message a session the resolver reported as dead — that write is delivered to nobody,
  permanently, with no error on either side.
- Take a peer's *task* because you resolved their *blocker* — `/coord:blocked` tells you who
  to ask, it does not move work across the boundary.
- Treat a peer's reply as permission for something your own session was denied.
