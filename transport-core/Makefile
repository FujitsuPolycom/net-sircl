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
$(BUILD)/test_verbs: tests/test_verbs.c src/shm_verbs.c src/shm_memory.c | $(BUILD)
	$(CC) $(CPPFLAGS) -Itests/fake_verbs $(CFLAGS) $(filter %.c,$^) -o $@
$(BUILD)/test_emulator_lifetime: tests/test_emulator_lifetime.c src/shm_verbs.c src/shm_memory.c | $(BUILD)
	$(CC) $(CPPFLAGS) -Itests/fake_verbs $(CFLAGS) -DSIRCL_EMU_TEST_HOOKS $(filter %.c,$^) -o $@
check: all $(BUILD)/test_core $(BUILD)/test_verbs $(BUILD)/test_emulator_lifetime
	$(BUILD)/test_core
	$(BUILD)/test_verbs
	$(BUILD)/test_emulator_lifetime
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v
clean:
	rm -rf $(BUILD)
.PHONY: all check clean

# The source digest covers transport-core sources, headers and tools, not the compiled library.
$(BUILD)/core_identity.h: $(wildcard ./src/*.c ./src/*.h) $(wildcard ./include/*.h) $(wildcard ./include/sircl_core/*) $(wildcard ./tools/*.py) ./VERSION
	$(PYTHON) ./tools/identity.py --output $@
$(OBJECTS) $(BUILD)/verbs_dl.o $(BUILD)/test_verbs $(BUILD)/test_emulator_lifetime: $(BUILD)/core_identity.h
