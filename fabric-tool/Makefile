PYTHON ?= python3
CC ?= cc
CFLAGS ?= -O2 -Wall -Wextra
PREFIX ?= /usr/local

.PHONY: all check install clean
all: build/fabric-tool-marker

build/fabric-tool-marker: mesh_marker.c
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) -libverbs -lmlx5

check:
	$(PYTHON) -m pytest tests/ -q -rs

install:
	install -d $(DESTDIR)$(PREFIX)/lib/fabric-tool/fabric_tool $(DESTDIR)$(PREFIX)/bin
	install -m 0644 fabric_tool/*.py $(DESTDIR)$(PREFIX)/lib/fabric-tool/fabric_tool/
	install -m 0644 mesh_marker.c README.md DESIGN.md HARDWARE-TEST-PLAN.md LICENSE NOTICE VERSION $(DESTDIR)$(PREFIX)/lib/fabric-tool/
	install -m 0755 fabric-tool $(DESTDIR)$(PREFIX)/bin/fabric-tool

clean:
	rm -rf build
