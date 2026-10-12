CC ?= cc
AR ?= ar
PYTHON ?= python3
BUILD ?= build
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -fPIC -Wall -Wextra -Werror -pthread
CPPFLAGS += -Iinclude -include $(BUILD)/core_identity.h
SOURCES = core topology sysfs fabric
OBJECTS = $(addprefix $(BUILD)/,$(addsuffix .o,$(SOURCES)))
all: $(BUILD)/libsircl-core.a $(BUILD)/verbs_dl.o
$(BUILD):
	mkdir -p $@
$(BUILD)/%.o: src/%.c $(wildcard include/sircl_core/*.h) | $(BUILD)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@
$(BUILD)/libsircl-core.a: $(OBJECTS)
	$(AR) rcs $@ $^
$(BUILD)/test_core: tests/test_core.c $(BUILD)/libsircl-core.a
	$(CC) $(CPPFLAGS) $(CFLAGS) $^ -o $@
$(BUILD)/test_verbs: tests/test_verbs.c src/shm_verbs.c | $(BUILD)
	$(CC) $(CPPFLAGS) -Itests/fake_verbs $(CFLAGS) $(filter %.c,$^) -o $@
check: all $(BUILD)/test_core $(BUILD)/test_verbs
	$(BUILD)/test_core
	$(BUILD)/test_verbs
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
clean:
	rm -rf $(BUILD)
.PHONY: all check clean

# Source identity is separate from pristine SIRCL proxy and kernel-pack identities.
$(BUILD)/core_identity.h: $(wildcard ./src/*.c) $(wildcard ./include/*.h) $(wildcard ./include/sircl_core/*) $(wildcard ./tools/*.py) ./PROXY_INPUTS.json ./VERSION
	$(PYTHON) ./tools/identity.py --output $@
$(OBJECTS) $(BUILD)/verbs_dl.o $(BUILD)/test_verbs: $(BUILD)/core_identity.h
