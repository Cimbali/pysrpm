VERSION:=$(shell python3 -c 'with open("$(dir $(lastword $(MAKEFILE_LIST)))pysrpm/__init__.py") as f: exec(f.read()) ; print(__version__)')
RELEASE:=v${VERSION}
SOURCES:=setup.py setup.cfg $(wildcard pysrpm/*.py) $(wildcard pysrpm/presets/*.conf)
PACKAGES:=dist/pysrpm-${VERSION}.tar.gz dist/pysrpm-${VERSION}-py3-none-any.whl

default: lint test

test:
	@pytest --color=yes

dist/pysrpm-${VERSION}.tar.gz: $(SOURCES)
	@python3 -m build --sdist

dist/pysrpm-${VERSION}-py3-none-any.whl: $(SOURCES)
	@python3 -m build --wheel

build: ${PACKAGES}
	@twine check $^

release: ${PACKAGES}
	@# Checks: packages, tag doesn’t exist
	@twine check $^
	@(echo no tags; git tag -l) | grep -qsv -xF "${RELEASE}"
	@# Work: upload packages, create tag, upload tag
	@twine upload $^
	@git tag "${RELEASE}"
	@git push origin --tags

lint-docs: lint-code
	@flake8 pysrpm --select=D

lint-code:
	@flake8 pysrpm --extend-ignore=D

lint: lint-code lint-docs

clean:
	@rm -rf tests/*_package/{build,dist,*.egg-info,PKG-INFO}
	@rm -rf dist/* build/*

.PHONY: lint-docs lint-code lint test build release clean default
