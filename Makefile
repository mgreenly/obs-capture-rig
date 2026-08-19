SWIFTC := swiftc -O
TOOLS  := caps sig adev probe

all: $(addprefix bin/,$(TOOLS))

bin/%: tools/%.swift
	@mkdir -p bin
	$(SWIFTC) $< -o $@ 2>/dev/null

clean:
	rm -rf bin

probe: all
	@echo "=== video devices ==="; ./bin/caps
	@echo; echo "=== device state ==="; ./bin/sig
	@echo; echo "=== audio inputs ==="; ./bin/adev

.PHONY: all clean probe
