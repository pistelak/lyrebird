.DEFAULT_GOAL := help
.NOTPARALLEL:

PYTHON ?= python3
ENGINE_PYTHON := .venv/bin/python
SWIFT_FORMAT := .build/tools/bin/swift-format
XCODEGEN := .build/tools/bin/xcodegen
SWIFT_SOURCES := menubar/Lyrebird menubar/LyrebirdTests acceptance/FixtureApp/FixtureApp

.PHONY: help setup setup-engine setup-app doctor check check-engine check-app check-shell \
	format format-engine format-app lint-engine lint-app types test test-engine test-app \
	build-fixture acceptance lock check-locks require-engine

help:
	@echo 'make setup         Install locked Python dependencies and pinned Swift tools'
	@echo 'make doctor        Check the local toolchain without installing anything'
	@echo 'make check         Lint, types, engine tests, app tests and fixture build'
	@echo 'make check-engine  Python formatting, lint, types and tests'
	@echo 'make check-app     Swift lint, app build/tests and fixture build'
	@echo 'make format        Format Python and Swift sources'
	@echo 'make test-engine   Fast Python tests (TEST_ARGS="-k reset" to select tests)'
	@echo 'make acceptance    Real simulator/network checks; opt-in, see CONTRIBUTING.md'
	@echo 'make lock          Regenerate hashed requirements after editing *.in'

setup: setup-engine setup-app

setup-engine:
	$(PYTHON) -c 'import sys; sys.version_info >= (3, 12) or sys.exit("Lyrebird setup requires Python 3.12+; see .tool-versions or set PYTHON=/path/to/python3")'
	$(PYTHON) -m venv --clear engine/.venv
	cd engine && $(ENGINE_PYTHON) -m pip install --require-hashes -r requirements-dev.txt
	cd engine && $(ENGINE_PYTHON) -m pip check

setup-app:
	bash scripts/setup-tools.sh

doctor: require-engine
	@$(PYTHON) --version
	@cd engine && $(ENGINE_PYTHON) -m pip check
	@cd engine && $(ENGINE_PYTHON) -m ruff --version && $(ENGINE_PYTHON) -m mypy --version
	@bash scripts/check-tools.sh
	@xcodebuild -version
	@xcrun swift --version

check: check-shell check-engine check-app

check-shell:
	bash -n bin/lyrebird scripts/setup-tools.sh scripts/check-tools.sh scripts/check-swift-format.sh menubar/scripts/verify-version.sh

check-engine: check-locks lint-engine types test-engine

require-engine:
	@test -x engine/$(ENGINE_PYTHON) || { echo 'Python environment missing; run make setup-engine' >&2; exit 1; }

check-locks: require-engine
	engine/$(ENGINE_PYTHON) scripts/check-locks.py

lint-engine: require-engine
	cd engine && $(ENGINE_PYTHON) -m ruff check .
	cd engine && $(ENGINE_PYTHON) -m ruff format --check .
	engine/$(ENGINE_PYTHON) -m ruff check --config engine/pyproject.toml scripts/check-locks.py
	engine/$(ENGINE_PYTHON) -m ruff format --check --config engine/pyproject.toml scripts/check-locks.py

types: require-engine
	cd engine && $(ENGINE_PYTHON) -m mypy

test: test-engine test-app

test-engine: require-engine
	cd engine && $(ENGINE_PYTHON) -m pytest tests/ -q $(TEST_ARGS)

check-app: lint-app test-app build-fixture

lint-app:
	bash scripts/check-tools.sh
	$(SWIFT_FORMAT) lint --strict --recursive --configuration .swift-format $(SWIFT_SOURCES)
	bash scripts/check-swift-format.sh

test-app:
	bash scripts/check-tools.sh
	cd menubar && ../$(XCODEGEN) generate
	cd menubar && xcodebuild -quiet -project Lyrebird.xcodeproj -scheme Lyrebird \
		-configuration Debug -derivedDataPath .build -destination 'platform=macOS' build test

build-fixture:
	bash scripts/check-tools.sh
	cd acceptance/FixtureApp && ../../$(XCODEGEN) generate
	cd acceptance/FixtureApp && xcodebuild -quiet -project FixtureApp.xcodeproj -scheme FixtureApp \
		-destination 'generic/platform=iOS Simulator' -derivedDataPath .build build

format: format-engine format-app

format-engine: require-engine
	cd engine && $(ENGINE_PYTHON) -m ruff check --fix-only .
	cd engine && $(ENGINE_PYTHON) -m ruff format .
	engine/$(ENGINE_PYTHON) -m ruff check --fix-only --config engine/pyproject.toml scripts/check-locks.py
	engine/$(ENGINE_PYTHON) -m ruff format --config engine/pyproject.toml scripts/check-locks.py

format-app:
	bash scripts/check-tools.sh
	$(SWIFT_FORMAT) format --in-place --recursive --configuration .swift-format $(SWIFT_SOURCES)

acceptance: require-engine
	bash scripts/check-tools.sh
	PATH="$(CURDIR)/.build/tools/bin:$$PATH" engine/$(ENGINE_PYTHON) -m pytest \
		-c engine/pyproject.toml -m acceptance engine/tests/acceptance -q $(TEST_ARGS)

lock: require-engine
	cd engine && $(ENGINE_PYTHON) -m uv pip compile --universal --python-version 3.12 \
		--generate-hashes --no-header --no-emit-index-url --no-emit-find-links \
		requirements.in -o requirements.txt
	cd engine && $(ENGINE_PYTHON) -m uv pip compile --universal --python-version 3.12 \
		--generate-hashes --no-header --no-emit-index-url --no-emit-find-links \
		-c requirements.txt requirements-dev.in -o requirements-dev.txt
	engine/$(ENGINE_PYTHON) scripts/check-locks.py --stamp
