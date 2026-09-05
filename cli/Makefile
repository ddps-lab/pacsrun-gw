# Building the sdist and wheel for PyPI.
#
# WHY A MAKEFILE FOR TWO COMMANDS. Because one of them is a copy that MUST happen first and is easy
# to forget. setuptools will not read a readme or a licence outside this directory (it raises
# DistutilsOptionError and names the path), and the repository keeps one of each at its root. So
# the build begins by bringing them here, and both copies are gitignored so they cannot drift from
# the originals -- they do not exist except during a build.
#
# The publish workflow runs exactly these targets, so a release and a local build produce the same
# artefacts from the same steps.

.PHONY: dist clean check

dist: clean
	cp ../README.md ./README.md
	cp ../LICENSE ./LICENSE
	python -m build

# `twine check` reads the built metadata the way PyPI will and refuses a description that would
# render broken. It costs a second and catches the whole class of "the page looks wrong" problems
# after upload, when the version number is already spent and cannot be reused.
check: dist
	python -m twine check dist/*

clean:
	rm -rf dist build ./README.md ./LICENSE *.egg-info
