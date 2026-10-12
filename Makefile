PYTHON ?= python3
PREFIX ?= /usr/local
VERSION := $(shell cat net-sircl/VERSION)

.PHONY: all emu check check-e2e check-e2e-if-available install clean package check-release
all:
	$(MAKE) -C transport-core all
	$(MAKE) -C net-sircl all
	$(MAKE) -C fabric-tool all
emu:
	$(MAKE) -C net-sircl emu
check:
	$(MAKE) -C transport-core check
	$(MAKE) -C net-sircl check
	$(MAKE) -C fabric-tool check PYTHON=$(PYTHON)
	$(PYTHON) -m unittest discover -s tests -v
check-e2e: emu
	$(PYTHON) scripts/check_e2e.py
check-e2e-if-available: emu
	$(PYTHON) scripts/check_e2e.py --if-available
install:
	$(MAKE) -C net-sircl install PREFIX=$(PREFIX) DESTDIR=$(DESTDIR)
	$(MAKE) -C fabric-tool install PREFIX=$(PREFIX) DESTDIR=$(DESTDIR)
	install -m 0644 README.md $(DESTDIR)$(PREFIX)/share/doc/net-sircl/README.md
	install -m 0644 NOTICE $(DESTDIR)$(PREFIX)/share/doc/net-sircl/NOTICE
	install -m 0644 SOURCE.json $(DESTDIR)$(PREFIX)/share/doc/net-sircl/SOURCE.json
	install -d $(DESTDIR)$(PREFIX)/share/doc/net-sircl/docs $(DESTDIR)$(PREFIX)/share/net-sircl/examples
	install -m 0644 docs/*.md $(DESTDIR)$(PREFIX)/share/doc/net-sircl/docs/
	install -m 0644 examples/* $(DESTDIR)$(PREFIX)/share/net-sircl/examples/
	install -d $(DESTDIR)$(PREFIX)/share/doc/net-sircl/examples
	install -m 0644 examples/* $(DESTDIR)$(PREFIX)/share/doc/net-sircl/examples/
	install -D -m 0644 net-sircl/tests/emulation/nccl_e2e.py $(DESTDIR)$(PREFIX)/share/doc/net-sircl/net-sircl/tests/emulation/nccl_e2e.py
	install -d $(DESTDIR)$(PREFIX)/share/doc/net-sircl/fabric-tool
	install -m 0644 fabric-tool/*.md fabric-tool/LICENSE fabric-tool/NOTICE $(DESTDIR)$(PREFIX)/share/doc/net-sircl/fabric-tool/
package:
	$(PYTHON) transport-core/tools/package.py --output dist/net-sircl-$(VERSION).tar.gz
check-release:
	$(PYTHON) scripts/check_release_safety.py .
clean:
	$(MAKE) -C net-sircl clean
	$(MAKE) -C transport-core clean
	$(MAKE) -C fabric-tool clean

.PHONY: help
help:
	@echo "all                    Build transport core, plugin and relay marker"
	@echo "emu                    Build the shared-memory test plugin"
	@echo "check                  Run CPU and release tests"
	@echo "check-e2e              Run single-GPU NCCL tests (prerequisites required)"
	@echo "check-e2e-if-available  Run NCCL tests or explicitly skip missing prerequisites"
	@echo "install                Install plugin, fabric-tool, examples and docs"
	@echo "package                Archive git ls-files into dist/ (Git checkout required)"
	@echo "check-release          Scan tracked source text for private data"
	@echo "clean                  Remove generated component builds"
