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
