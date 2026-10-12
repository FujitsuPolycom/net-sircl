# net-sircl: an NCCL network plugin for DGX Spark cabling.
#
#   make                       libnccl-net-sircl.so against libibverbs (the plugin NCCL loads)
#   make emu                   libnccl-net-sircl-emu.so against the shared-memory verbs stand-in (tests/standin):
#                              NCCL drives it between processes of one host, for tests without a RoCE device
#   make check                 the CPU tests (tests/test_net_sircl.py): no NCCL, GPU, RoCE device or Spark
#   make install PREFIX=/usr/local   installs lib/libnccl-net-sircl.so and share/doc/net-sircl/
#
# Builds need a C11 compiler and, for the plugin, libibverbs headers (rdma-core). NCCL's plugin headers are
# vendored in nccl/ (see NOTICE); no NCCL tree is needed.

VERSION := $(shell cat VERSION)
CC ?= cc
PYTHON ?= python3
PREFIX ?= /usr/local
CFLAGS ?= -std=gnu11 -O2 -g -Wall -Wextra -Werror -pthread
STANDIN := tests/standin
CORE ?= $(firstword $(wildcard ../../transport-core ../transport-core))
CORE_SOURCES := $(CORE)/src/core.c $(CORE)/src/topology.c $(CORE)/src/sysfs.c
CORE_HEADERS := $(wildcard $(CORE)/include/sircl_core/*.h)
CPPFLAGS += -I$(CORE)/include -include .core-build/core_identity.h

all: libnccl-net-sircl.so

libnccl-net-sircl.so: net_sircl.c net_sircl.h $(wildcard nccl/*.h) VERSION $(CORE_SOURCES) $(CORE_HEADERS)
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -shared -DNET_SIRCL_RELEASE='"$(VERSION)"' -I. net_sircl.c $(CORE_SOURCES) -o $@ -libverbs

emu: libnccl-net-sircl-emu.so

libnccl-net-sircl-emu.so: net_sircl.c net_sircl.h $(wildcard nccl/*.h) $(STANDIN)/shm_verbs.c VERSION $(CORE_SOURCES) $(CORE_HEADERS) $(CORE)/src/shm_verbs.c
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -shared -DNET_SIRCL_RELEASE='"$(VERSION)"' -DNET_SIRCL_STANDIN -I. \
	  -I$(STANDIN)/fake_verbs -I$(STANDIN) net_sircl.c $(STANDIN)/shm_verbs.c $(CORE_SOURCES) -o $@

check:
	@test -n "$(CORE)" || { echo "transport-core is required beside net-sircl"; exit 1; }
	$(PYTHON) tests/test_net_sircl.py

install: libnccl-net-sircl.so
	install -D -m 0755 libnccl-net-sircl.so $(DESTDIR)$(PREFIX)/lib/libnccl-net-sircl.so
	install -D -m 0644 README.md $(DESTDIR)$(PREFIX)/share/doc/net-sircl/README.md
	install -D -m 0644 LICENSE $(DESTDIR)$(PREFIX)/share/doc/net-sircl/LICENSE
	install -D -m 0644 NOTICE $(DESTDIR)$(PREFIX)/share/doc/net-sircl/NOTICE
	install -D -m 0644 $(CORE)/NOTICE $(DESTDIR)$(PREFIX)/share/doc/net-sircl/TRANSPORT-CORE-NOTICE
	install -D -m 0644 $(CORE)/VERSION $(DESTDIR)$(PREFIX)/share/doc/net-sircl/TRANSPORT-CORE-VERSION
	install -D -m 0644 nccl/LICENSE.txt $(DESTDIR)$(PREFIX)/share/doc/net-sircl/NCCL-LICENSE.txt

clean:
	rm -f libnccl-net-sircl.so libnccl-net-sircl-emu.so

.PHONY: all emu check install clean

# Source identity is separate from pristine SIRCL proxy and kernel-pack identities.
.core-build/core_identity.h: $(wildcard $(CORE)/src/*.c) $(wildcard $(CORE)/include/*.h) $(wildcard $(CORE)/include/sircl_core/*) $(wildcard $(CORE)/tools/*.py) $(CORE)/PROXY_INPUTS.json $(CORE)/VERSION
	$(PYTHON) $(CORE)/tools/identity.py --output $@
libnccl-net-sircl.so libnccl-net-sircl-emu.so: .core-build/core_identity.h

CPPFLAGS += -DSIRCL_CORE_STATIC
