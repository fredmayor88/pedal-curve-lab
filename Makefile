# Pedal Curve Lab - dev and release tasks.
#
# Meant to be run from Git Bash on Windows (that is where `make` lives on a
# normal Windows box, and `build` can only run on Windows anyway - PyInstaller
# freezes for the platform it runs on, it does not cross-compile).
#
# The program itself needs nothing but a stock Python: standard library,
# tkinter and ctypes. The virtualenv exists for PyInstaller alone, which is
# why there is no requirements.txt to keep in sync.

.DEFAULT_GOAL := help

APP      := pedal-curve-lab
MAIN     := $(APP).py
VENV     := .venv
DIST     := dist
BUILD    := build

# Stated once, in the source. The zip name and the git tag both come from it,
# so a release cannot end up labelled differently from what it contains.
VERSION  := $(shell sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' $(MAIN))
STAGE    := $(DIST)/$(APP)
ZIP      := $(DIST)/$(APP)-v$(VERSION)-win64.zip
TAG      := v$(VERSION)

ifeq ($(OS),Windows_NT)
  VPY := $(VENV)/Scripts/python.exe
else
  VPY := $(VENV)/bin/python
endif

# Falls back to the system python before the venv exists, so `make run` and
# `make test` work on a fresh clone without a setup step - the program has no
# dependencies to install.
PY := $(shell test -x $(VPY) && echo $(VPY) || echo python)

# build is freeze + zip as an ordered chain rather than a recursive $(MAKE):
# on Windows that variable expands to a path containing "(x86)", which the
# shell will not swallow.
.PHONY: help setup run live test freeze build zip release-check release clean distclean

help:
	@echo "Pedal Curve Lab $(VERSION)"
	@echo
	@echo "  make setup    create $(VENV) and install the build tooling"
	@echo "  make run      run from source"
	@echo "  make live     run from source, opening on the Live / Verify tab"
	@echo "  make test     encode/decode and curve-model round-trip checks"
	@echo "  make build    freeze to $(STAGE)/ and zip it (Windows only)"
	@echo "  make release  build, tag $(TAG) and publish it to GitHub"
	@echo "  make clean    remove build output"
	@echo

$(VPY):
	python -m venv $(VENV)
	$(VPY) -m pip install --upgrade --quiet pip
	$(VPY) -m pip install --quiet pyinstaller

setup: $(VPY)
	@$(VPY) -m pip --version
	@$(VPY) -m PyInstaller --version | sed 's/^/pyinstaller /'
	@echo "ready - run 'make run' to start it, 'make build' to package it"

run:
	$(PY) $(MAIN)

live:
	$(PY) $(MAIN) --live

test:
	$(PY) $(MAIN) --selftest

build: zip

# --onedir, not --onefile: a single exe would unpack itself to a temp folder
# on every launch, which is slower and would put the running program somewhere
# other than where it keeps its settings. A plain folder is also something you
# can look inside, which suits a tool whose whole point is being inspectable.
# --windowed so double-clicking it does not raise a console window; the text
# modes write a report file instead of printing (see start_report).
freeze: $(VPY)
	@test "$(OS)" = "Windows_NT" || { echo "build only runs on Windows"; exit 1; }
	rm -rf $(STAGE) $(BUILD)
	$(VPY) -m PyInstaller \
	  --noconfirm --clean --onedir --windowed \
	  --name $(APP) \
	  --distpath $(DIST) --workpath $(BUILD) --specpath $(BUILD) \
	  $(MAIN)
	cp README.md LICENSE $(STAGE)/

zip: freeze
	rm -f $(ZIP)
	cd $(DIST) && powershell -NoProfile -Command \
	  "Compress-Archive -Path '$(APP)' -DestinationPath '$(APP)-v$(VERSION)-win64.zip'"
	@echo
	@ls -lh $(ZIP)
	@echo "unzips to a single $(APP)/ folder - no installer, no dependencies"

# Checks run before the build, not after: they are the fast part, and there is
# no sense freezing an exe for a release that is about to be refused. A
# release that does not match what is pushed is worse than no release, and the
# tag is the one thing here that is awkward to take back.
release-check:
	@test -z "$$(git status --porcelain)" \
	  || { echo "working tree is dirty - commit or ignore everything first"; \
	       git status --short; exit 1; }
	@git fetch --quiet origin
	@test -z "$$(git log origin/$$(git rev-parse --abbrev-ref HEAD)..HEAD 2>/dev/null)" \
	  || { echo "unpushed commits - push first"; exit 1; }
	@git rev-parse -q --verify "refs/tags/$(TAG)" >/dev/null \
	  && { echo "tag $(TAG) already exists - bump APP_VERSION in $(MAIN)"; exit 1; } \
	  || true
	@echo "clean, pushed, $(TAG) is free - building"

release: release-check build
	gh release create $(TAG) $(ZIP) \
	  --title "$(APP) $(TAG)" \
	  --generate-notes
	@echo "published: https://github.com/$$(gh repo view --json nameWithOwner -q .nameWithOwner)/releases/tag/$(TAG)"

clean:
	rm -rf $(DIST) $(BUILD)

distclean: clean
	rm -rf $(VENV)
