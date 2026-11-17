---
name: dynamicess-post-pr
description: After a venus-dynamicess pull request has been reviewed or merged, compare what Copilot and the maintainers found with what the dynamicess-pre-pr skill checks, and propose updates to that skill for what it missed. Use when a PR is merged or closed, after a review round with findings, or when the user asks what the reviews found or whether the pre-PR skill needs updating.
---

# venus-dynamicess post-PR check

The goal is that the next PR doesn't get the same finding. Propose changes; don't edit the pre-PR skill without the user's agreement.

## 1. Collect the findings

For the PR, or for every PR merged or closed since the pre-PR skill last changed (`git log -1 --format=%cs -- .claude/skills/dynamicess-pre-pr`):

```
gh api repos/victronenergy/venus-dynamicess/pulls/<n>/comments --paginate \
  --jq '.[] | "--- \(.user.login) \(.path):\(.line // .original_line)\n\(.body)\n"'
gh api repos/victronenergy/venus-dynamicess/pulls/<n>/reviews \
  --jq '.[] | select(.body != "") | "### \(.user.login) \(.state)\n\(.body)\n"'
```

Take the findings from Copilot and from human reviewers. Skip Copilot's overview tables and "resolved since last review" lists; they repeat the inline comments.

## 2. Classify each finding

- **Wrong or out of scope**: the author rejected it with a reason, or it only happens in a setup we don't support. Nothing to change.
- **Caught by `check.py`**: it should have failed before the PR. If it didn't run, say so; nothing to change.
- **Covered by a check in `SKILL.md` but missed**: the check is too vague to catch it. Propose sharper wording, with this PR's case as its example.
- **Not covered**: a new kind of bug. Propose a check only if it can come back in other code in this repo. A one-off mistake gets nothing.
- **Mechanical** (a script could find it without judgement, e.g. a missing file, a pattern in the source): propose it for `check.py` rather than for the text.

Also note findings that came from fixes to an earlier round; they show which checks should be run again after each push.

## 3. Propose

Show a table of findings (PR, `file:line`, one line each, class), then the proposed diff to `.claude/skills/dynamicess-pre-pr/`. Keep the skill short: replace or merge checks rather than add, and drop a check that hasn't found anything in a long time if the user agrees. Test a `check.py` change on the PR's commit that had the bug (`git worktree add --detach <dir> <sha>`, `git submodule update --init`), and on `main`.

After the user agrees: commit on a branch with the `write-commit-message` skill, and name the PRs in the message.
