.DEFAULT_GOAL := help

# Default target environment
# Override on command line: `make build ENV=gateway`
# or export in your shell: `export ENV=gateway`
ENV ?= esp32-c3-supermini

# Friendly aliases: sensor -> esp32-c3-supermini, gateway -> esp32-s3-gateway
ifeq ($(ENV),sensor)
  override ENV := esp32-c3-supermini
endif
ifeq ($(ENV),gateway)
  override ENV := esp32-s3-gateway
endif

CPP_FILES := $(wildcard src/*.cpp include/*.h)

.PHONY: help build upload monitor flash lsp compiledb format clean

help:
	@echo -e "Smart Chair — Build & Development Shortcuts"
	@echo -e ""
	@echo -e "  \033[33mActive Target:\033[0m \033[32m$(ENV)\033[0m"
	@echo -e "  (Change with \033[36mENV=sensor\033[0m [default] or \033[36mENV=gateway\033[0m)"
	@echo -e ""
	@echo -e "  \033[36mmake build\033[0m   [ENV=...]   Build firmware"
	@echo -e "  \033[36mmake upload\033[0m  [ENV=...]   Upload firmware"
	@echo -e "  \033[36mmake flash\033[0m   [ENV=...]   Upload and open serial monitor"
	@echo -e "  \033[36mmake monitor\033[0m             Open serial monitor (115200 baud)"
	@echo -e "  \033[36mmake lsp\033[0m     [ENV=...]   Regenerate compile_commands.json for Neovim"
	@echo -e "  \033[36mmake format\033[0m              Run clang-format on all C/C++ files"
	@echo -e "  \033[36mmake clean\033[0m               Clean PlatformIO build output"
	@echo -e ""
	@echo -e "  \033[33mExamples:\033[0m"
	@echo -e "    make build                 # Builds sensor (default)"
	@echo -e "    make build ENV=gateway     # Builds S3 gateway"
	@echo -e "    make flash ENV=gateway     # Flashes S3 gateway & monitors"
	@echo -e ""

build:
	pio run -e $(ENV)

upload:
	pio run -e $(ENV) -t upload

flash:
	pio run -e $(ENV) -t upload -t monitor

monitor:
	pio device monitor

compiledb:
	pio run -e $(ENV) -t compiledb

lsp: compiledb

format:
	clang-format -i $(CPP_FILES)

clean:
	pio run -t clean
