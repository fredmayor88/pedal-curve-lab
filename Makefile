# Pedal Curve Lab - dev and release tasks.
#
# Needs make itself (GnuWin32, Chocolatey, Scoop or MSYS2 - it is not part of
# Git for Windows) and Git for Windows, which supplies the shell these recipes
# are written in.
#
# The program itself needs nothing but a stock Python: standard library,
# tkinter and ctypes. The virtualenv exists for PyInstaller alone, which is
# why there is no requirements.txt to keep in sync.

.DEFAULT_GOAL := help

# Make on Windows falls back to cmd.exe unless it finds a POSIX shell on PATH,
# and cmd cannot run a line of what follows. Launched from PowerShell it finds
# nothing, so it is pointed at the sh.exe that Git for Windows ships, and that
# folder is put on PATH as well - otherwise sh starts but sed, rm and cp are
# still missing. The net effect is that `make` works from any shell rather
# than only from Git Bash.
#
# The wildcards are how the spaces in "Program Files" are dodged: make splits
# variables on whitespace, so $(dir ...) and $(firstword ...) would tear these
# paths in half, while a `*` matches the space without ever naming it.
ifeq ($(OS),Windows_NT)
  GITBIN := $(wildcard C:/Program*Files/Git/usr/bin)
  ifeq ($(GITBIN),)
    GITBIN := $(wildcard $(subst \,/,$(LOCALAPPDATA))/Programs/Git/usr/bin)
  endif
  ifneq ($(GITBIN),)
    SHELL := $(GITBIN)/sh.exe
    export PATH := $(GITBIN);$(PATH)
  else
    # Nothing found, but make may have located a shell by itself - it records
    # a full path when it did, and the bare name "sh.exe" when it did not.
    ifeq ($(findstring /,$(SHELL)),)
      $(error no POSIX shell found: install Git for Windows, or run this from a shell that has one)
    endif
  endif
endif

APP      := pedal-curve-lab
MAIN     := $(APP).py
# Used twice over: --icon burns it into the executable for Explorer and the
# taskbar, --add-data ships the file itself so the running window can wear it
# too. They are separate mechanisms and neither implies the other.
#
# Absolute, because --specpath puts the generated spec under build/ and
# PyInstaller resolves data paths relative to the spec, not to where make was
# run. Quoted at each use so a checkout under a path with spaces still works.
ICON     := $(CURDIR)/icon.ico
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
	@echo "  make build    freeze to $(STAGE)/ and zip it"
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
	  --icon "$(ICON)" --add-data "$(ICON);." \
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
