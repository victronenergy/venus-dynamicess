---
name: dynamicess-pre-pr
description: Pre-PR review for venus-dynamicess (the Dynamic ESS service on the GX). Use before any git push, before opening or updating a pull request, or when the user asks whether a branch is ready. Runs the mechanical checks (syntax, line endings, Makefile install lists, tests, testinstall) and then reviews the diff for what Copilot keeps finding here - special setpoint values, best-candidate selection loops, requests outside a device's range, changes that never get resent, flags derived from the wrong quantity - and reports one list of findings.
---

# venus-dynamicess pre-PR review

Run before a branch is pushed or a PR is opened or updated. Report findings; don't push, and don't fix anything the user hasn't agreed to. On later pushes, run at least steps 1, 3 and 5 on what changed since the last push: fixes to review findings bring new findings of their own (PR #5: a `break` added to fix one finding relied on an order the list didn't have).

## 1. Mechanical checks

```
python3 .claude/skills/dynamicess-pre-pr/check.py [base]   # base defaults to origin/main
```

It checks untracked `.py` files, syntax of every file, CRLF (`.gitattributes` wants LF), that every module and used aiovelib module is in the Makefile's `FILES`/`AIOVELIB` (else it is not installed on the GX), the submodule, mergeability, pyflakes on changed lines, `make test` and `make testinstall`. The first run creates a venv in `~/.cache/dynamicess-pre-pr/` with uv. Every FAIL is a finding.

If the branch has a PR, read its threads too (`gh api repos/victronenergy/venus-dynamicess/pulls/<n>/comments`, `gh pr view <n> --json reviews`). Unresolved comments are findings.

## 2. Scope

Read `git diff <base>...HEAD` and the uncommitted changes in full, with the callers of every changed function. Most bugs Copilot found were in how a changed function interacts with values set elsewhere (`globals.py`, `loop()` in `dynamicess.py`, the system description from the RM).

## 3. Logic checks

For each changed function that picks, computes or sends a value:

- **Special values.** Find every special value the input can have, such as `-1` in a `Configurable` default or minimum (`C_EV_EMERGENCY_SETPOINT` = `-1` means "charge at minimum power"; the grid and battery limits also default to `-1`), `None`, `0`, and requests larger than any device can do (`CHARGE_NOW` asks 22,080 W). Follow each through the new code: is it still handled as before, or now treated as a number?
- **Selection loops.** A loop that picks the best candidate must compare each one by the result it would really deliver (after clamping or applying a factor), keep the current one on a tie, and never overwrite unconditionally. If it relies on order (`break` on first match, "last one wins"), check the list really has that order: dicts and `transitions` keep insertion order, not power order.
- **Ranges.** A target outside a device's `[min, max]` is clamped and compared, not skipped. Below-min and above-max both need a case.
- **Send only on change.** A guard like `if new_mode.id != active.id` must compare everything that is sent (mode and factor, setpoint, limits). Otherwise a change within the same mode is never sent.
- **Derived flags.** Flags such as `abnormal_condition`, "stopped" or "charging" are derived from the physical result (watts), not from an intermediate parameter: a factor of 0 is the bottom of a range, not 0 W.
- **Parallel branches.** When a function has branches per model or device type (discrete vs range OMBC, VE.Bus vs Multi RS, `ess_device`), every special case above must behave the same in each branch, unless the difference is intended and commented.
- **State.** For new state (flags in `gx_flags`, the active mode, timers), list every path that sets or clears it: connect, disconnect, timeout, reconnect, DESS off, the RM leaving D-Bus.

## 4. Tests

New behaviour gets a test in `tests/` that fails without the change, using the fakes in `tests/test_evcs_delegate.py`. For selection logic, a table of concrete cases: the special values from step 3, a target above every range, a target between two ranges, two candidates in reverse order. If no test is practical, say why in the report.

## 5. Docs, commit and code

- README and comments say what the code doesn't: no tables that copy enums, `Configurable` defaults or D-Bus paths from `globals.py`/`dynamicess.py`; name the file instead (maintainer review on PR #3).
- Commit messages: the `write-commit-message` skill.
- Code: the `write-elegant-code` skill applies (this repo is read by people). Flag duplicated logic, dead code the change left behind, and unused imports on changed lines.

## 6. Generic review

Run the `code-review` skill on the branch diff, asking it to check step 3 explicitly. Merge its findings with the ones above.

## 7. Report

One list, most severe first. For each: `file:line`, a concrete scenario (input, what happens, what should), and a proposed fix. Then one line naming the checks that were clean, and anything skipped and why. Ask before fixing.

After the PR is reviewed or merged, run the `dynamicess-post-pr` skill to update this one with what it missed.

If Copilot rounds don't converge, sort the new findings by origin (added by the last round's fixes, unsupported setups, old code nobody touched) and propose a stop rule instead of fixing one by one.
