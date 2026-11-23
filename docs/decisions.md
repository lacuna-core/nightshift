# Decisions

Significant, hard-to-reverse decisions and why they were made, oldest first.
The log is append-only: to change a decision, add a new entry that names the
one it replaces. Current rules live in `CLAUDE.md` and `README.md`; this file
explains how they came to be.

Keep entries compact: about 120 words, one to three sentences per paragraph.
Record what was chosen and why, and the trade-offs someone would not guess.
Leave out how it is implemented (file names, endpoints, flags, steps): the
code and the other docs say that.

Entry format:

```markdown
## YYYY-MM-DD — Decision as a short statement

**Context:** the problem and the constraints at the time.

**Decision:** what was chosen.

**Consequences:** the trade-offs accepted, good and bad.
```

---

## 2026-11-21 — Track decisions in an append-only log

**Context:** The reasons behind design choices end up scattered across commits, PRs and
chat sessions, and a file that mixes current rules with history goes stale.

**Decision:** One append-only file with a short entry per significant decision, added in
the same change as the decision. A later entry supersedes an earlier one by naming it.
`CLAUDE.md` stays the source of truth for current rules.

**Consequences:** One place to look for the "why". Entries have no status field, so a
superseded entry is only recognizable from the later one. The bar for "significant" has
to stay high.

## 2026-11-21 — A release is numbered from its commits, by a workflow

**Context:** Bumping the version by hand in several files, then tagging, is easy to get
wrong or forget, and Conventional Commits already say what kind of change went in.

**Decision:** A manually started workflow works out the next version from the commits
since the last tag (a feature raises the minor number, anything else the patch number),
and publishes the release, which creates the tag. The version in the package metadata is
a placeholder.

**Consequences:** Nothing to bump or tag by hand, and release notes come for free from
squash-merged PR titles. The installed package does not know its release number. Going
to 1.0 takes a hand-made tag.

## 2026-11-21 — Branches are named after their issue

**Context:** Nightshift creates branches before its agent has seen the code, so it cannot
know the Conventional Commits type of the change that a `<type>/<description>` name needs.
A name that depends on guessing the type is unreliable, and human and agent work should
share one convention.

**Decision:** Every branch is `<issue-num>/<short-kebab-description>`, for agents and
people alike, and every branch belongs to a GitHub issue. The change type stays in the
commit messages and the PR title, where release notes read it.

**Consequences:** Branch names are unique, link straight to their ticket and need no
guessing. Work without an issue now needs one created first, which adds a small step to
quick fixes. Branch names no longer show the kind of change at a glance.

## 2026-11-21 — One instance serves many boards and repos

**Context:** The first spec draft ran one Nightshift per repo, with the config committed
in that repo. Working on several projects meant running and watching several loops.

**Decision:** One instance per machine serves every board and repo listed in a config in
the user's home directory. Repos are listed as paths to local clones, and worktrees are
made from them. Boards and repos are independent lists; the issue says which repo a
ticket belongs to. The agent limit is set per board.

**Consequences:** Repos hold nothing of Nightshift's. Boards never compete for slots, but
nothing caps the machine as a whole: the total is the sum of the limits. The
config is no longer shared through git, so each machine is set up separately. Runs must
be identified by repo as well as issue number.

## 2026-11-21 — Agents run in Herdr through its core

**Context:** Agents need terminals a person can watch and take over, which survive
Nightshift stopping. Herdr has two interfaces: the window's commands, which need the
window open, and its core's CLI, which does not.

**Decision:** Nightshift uses the core's CLI and plain git worktrees, and the window's
commands only to name tabs. Terminals are labelled so that Herdr shows them in the right
space, and agent state is what Claude Code reports to Herdr.

**Consequences:** Nightshift works with the window closed and needs no Claude Code hooks.
Spaces carry Herdr's default, the folder name, and tabs are named only while the window is
open. Herdr's API is a 0.x preview and may break on an update.

## 2026-11-21 — Moving a ticket back to Todo starts over, keeping unsafe leftovers

**Context:** A ticket that goes back to `Todo` still has a branch, a worktree and perhaps
a PR from its earlier run. The next run could continue from them or start clean.

**Decision:** It starts clean, on the next numbered branch. Nightshift closes the old PR
but keeps the branch, and removes the old worktree only if nothing in it can be lost and
nobody is using it. The same rule covers every cleanup: a change on GitHub never destroys
local work, because its local effect is hard to foresee.

**Consequences:** One board move has one meaning, a new run never inherits a bad attempt,
and no commits or uncommitted changes are lost. Old branches, and worktrees that are not
safe to remove, pile up until someone deals with them.

## 2026-11-22 — Usage limits are read from Claude Code's transcript

**Context:** Nightshift must tell an agent stopped by a usage limit from one that needs a
person, and know when the limit resets. Herdr only shows that the agent stopped.

**Decision:** Read the session's transcript, which records the limit and its reset time as
data. Anything in it that is not understood counts as no limit.

**Consequences:** No hooks and no reading of the screen. The format is Claude Code's own and
not documented, so an update can break it; the agent is then flagged as needing a person,
which is safe but wakes nobody up by itself.

## 2026-11-22 — The config file is YAML, read with PyYAML

**Context:** `nightshift.yaml` is written by a command but also edited by hand, and needs
nested lists.

**Decision:** YAML, read and written with PyYAML and validated with Pydantic.

**Consequences:** The usual library, with type stubs and no surprises. It does not keep
comments, so running `init` again drops the ones added by hand.

## 2026-11-22 — What GitHub must be told goes through an outbox

**Context:** A run that fails must also be labelled, commented on and notified about. A crash
or an outage between those steps would lose the signal, and repeating them would double it.

**Decision:** The state change and its labels, comments and notifications are written in one
database transaction, and delivered afterwards until they succeed. Each comment carries a mark
by which a repeated one is recognized. Status changes are not delivered this way: they happen
before the state change, after the ticket was read again.

**Consequences:** No signal is lost or doubled, and an outage only delays it. An undeliverable
comment holds back the later ones of its run, which `status` shows. A notification may be shown
twice if Nightshift is killed right after it.

## 2026-11-22 — Cleanup only reports in its first week, by itself

**Context:** Removing a worktree wrongly is the worst thing Nightshift can do. The spec asks for
a week of report-only on real repos before it may remove anything.

**Decision:** The week is built in. It starts with the first worktree that could be removed on a
machine, and ends without anyone having to change anything.

**Consequences:** No setting to remember, and every new machine gets the same trial. Worktrees
pile up for a week and then go on the first poll after it. Pull requests are closed on starting
over from the first day, because that can be undone.
