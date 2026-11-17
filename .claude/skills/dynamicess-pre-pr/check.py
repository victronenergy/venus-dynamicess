#!/usr/bin/env python3
"""Mechanical pre-PR checks for venus-dynamicess. Prints one PASS/WARN/FAIL/SKIP
line per check; exits 1 if any check fails.

Usage: check.py [base]   (default base: origin/main)
"""
import os
import re
import subprocess
import sys

REPO = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                      text=True, check=True).stdout.strip()
VENV = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                    "dynamicess-pre-pr", "venv")
# s2-python is pinned to what Venus OS ships (recipe python3-s2).
DEPS = ["s2-python==0.8.0", "dbus-fast", "pyflakes"]

failed = False


def report(status, name, detail=""):
	global failed
	failed |= status == "FAIL"
	print(f"{status:4} {name}" + (f": {detail}" if detail else ""))


def run(*cmd, env=None):
	return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env)


def git(*args):
	return run("git", *args).stdout


def changed_lines(base):
	"""{file: set(line numbers added or changed)} for base...HEAD plus the working tree."""
	result, current = {}, None
	for line in git("diff", "-U0", "--ignore-space-at-eol", base, "--", "*.py").splitlines():
		if line.startswith("+++ "):
			current = line[6:] if line != "+++ /dev/null" else None
			if current:
				result.setdefault(current, set())
		elif line.startswith("@@") and current:
			m = re.search(r"\+(\d+)(?:,(\d+))?", line)
			start, count = int(m.group(1)), int(m.group(2) or 1)
			result[current].update(range(start, start + count))
	for f in untracked():
		if f.endswith(".py"):
			result[f] = None  # whole file is new
	return result


def untracked():
	return [l[3:] for l in git("status", "--porcelain", "-uall").splitlines() if l.startswith("?? ")]


def check_untracked():
	files = untracked()
	py = [f for f in files if f.endswith(".py")]
	if py:
		report("FAIL", "untracked", "not in any commit: " + ", ".join(py))
	elif files:
		report("WARN", "untracked", ", ".join(files))
	else:
		report("PASS", "untracked")


def check_syntax():
	files = git("ls-files", "*.py").split() + [f for f in untracked() if f.endswith(".py")]
	files = [f for f in files if not f.startswith("ext/")]
	r = run(sys.executable, "-c",
	        "import sys,ast\nbad=0\nfor f in sys.argv[1:]:\n"
	        "  try: ast.parse(open(f,encoding='utf-8').read(),f)\n"
	        "  except SyntaxError as e: print(f'{f}:{e.lineno}: {e.msg}'); bad=1\n"
	        "sys.exit(bad)", *files)
	report("FAIL" if r.returncode else "PASS", "syntax", r.stdout.strip().replace("\n", "; "))


def check_pyflakes(py, lines):
	targets = [f for f in lines if os.path.exists(os.path.join(REPO, f))]
	if not targets:
		return report("SKIP", "pyflakes", "no Python files changed")
	out = run(py, "-m", "pyflakes", *targets).stdout
	hits = []
	for l in out.splitlines():
		m = re.match(r"(.+?):(\d+):", l)
		if m and (lines.get(m.group(1)) is None or int(m.group(2)) in lines[m.group(1)]):
			hits.append(l)
	if hits:
		report("FAIL", "pyflakes (changed lines)", "\n     " + "\n     ".join(hits))
	else:
		report("PASS", "pyflakes (changed lines)")


def check_eol():
	bad = []
	for l in git("ls-files", "--eol", "*.py").splitlines():
		index, work = l.split()[:2]
		if "crlf" in index or "crlf" in work:
			bad.append(l.split("\t")[-1])
	report("FAIL" if bad else "PASS", "line endings (LF)",
	       "CRLF in " + ", ".join(bad) + " (fix: git add --renormalize . and commit)" if bad else "")


def makefile_list(name):
	text = open(os.path.join(REPO, "Makefile")).read()
	m = re.search(rf"^{name}\s*=(.*?)(?:\n\s*\n)", text, re.S | re.M)
	return set(m.group(1).replace("\\", " ").split()) if m else set()


def check_makefile():
	files, aiovelib = makefile_list("FILES"), makefile_list("AIOVELIB")
	top = {f for f in git("ls-files", "*.py").split() if "/" not in f}
	top |= {f for f in untracked() if f.endswith(".py") and "/" not in f}
	problems = [f"{f} not in FILES" for f in sorted(top - files)]
	used = set()
	for f in top:
		src = open(os.path.join(REPO, f), encoding="utf-8").read()
		used |= {m + ".py" for m in re.findall(r"^\s*(?:from|import)\s+aiovelib\.(\w+)", src, re.M)}
	problems += [f"aiovelib/{m} not in AIOVELIB" for m in sorted(used - aiovelib)]
	report("FAIL" if problems else "PASS", "Makefile install lists", "; ".join(problems))


def check_submodule():
	st = git("submodule", "status")
	if st.startswith("-"):
		report("FAIL", "submodule", "ext/aiovelib not checked out (git submodule update --init)")
	elif st.startswith("+"):
		report("WARN", "submodule", "ext/aiovelib differs from the committed commit: intended bump?")
	else:
		report("PASS", "submodule")


def ensure_venv():
	py = os.path.join(VENV, "bin", "python")
	if os.path.exists(py):
		return py
	if run("uv", "--version").returncode:
		return None
	# --system-site-packages: dynamicess.py imports dbus-python, which is not on PyPI-friendly terms.
	if run("uv", "venv", "-q", "--system-site-packages", "--python", "/usr/bin/python3", VENV).returncode:
		return None
	env = dict(os.environ, VIRTUAL_ENV=VENV)
	if run("uv", "pip", "install", "-q", *DEPS, env=env).returncode:
		return None
	return py


def check_make(py, target):
	env = dict(os.environ, PATH=os.path.dirname(py) + os.pathsep + os.environ["PATH"])
	r = run("make", target, env=env)
	tail = "\n     ".join((r.stdout + r.stderr).strip().splitlines()[-8:])
	report("FAIL" if r.returncode else "PASS", f"make {target}", "\n     " + tail if r.returncode else "")


def check_tests_changed(lines):
	code = [f for f in lines if not f.startswith(("tests/", ".claude/"))]
	tests = [f for f in lines if f.startswith("tests/")]
	if code and not tests:
		report("WARN", "tests", "code changed but no test changed: " + ", ".join(code))
	else:
		report("PASS", "tests changed with code")


def check_merge(base):
	r = run("git", "merge-tree", "--write-tree", "--name-only", base, "HEAD")
	if r.returncode == 1:
		files = r.stdout.splitlines()[1:]
		report("FAIL", f"merges into {base}", "conflicts: " + ", ".join(f for f in files if f))
	elif r.returncode:
		report("SKIP", f"merges into {base}", r.stderr.strip())
	else:
		report("PASS", f"merges into {base}")


def main():
	base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
	if base.startswith("origin/"):
		run("git", "fetch", "-q", "origin", base.split("/", 1)[1])
	lines = changed_lines(base)
	print(f"base {base}; changed Python files: {', '.join(sorted(lines)) or 'none'}")
	check_untracked()
	check_syntax()
	check_eol()
	check_makefile()
	check_submodule()
	check_merge(base)
	check_tests_changed(lines)
	py = ensure_venv()
	if not py:
		report("SKIP", "pyflakes, make test, make testinstall", f"no venv at {VENV} (needs uv)")
	else:
		check_pyflakes(py, lines)
		check_make(py, "test")
		check_make(py, "testinstall")
	sys.exit(1 if failed else 0)


main()
