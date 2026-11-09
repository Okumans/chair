.DEFAULT_GOAL := help

ENV_C3 ?= esp32-c3-supermini
ENV_S3 ?= esp32-s3-bench

# Source and header files to format
CPP_FILES := $(wildcard src/*.cpp include/*.h)

.PHONY: help build build-s3 upload upload-s3 monitor flash lsp compiledb format clean

help: ## Show available shortcut commands
	@echo "Smart Chair — Build & Development Shortcuts"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""

build: ## Build firmware for ESP32-C3 SuperMini
	pio run -e $(ENV_C3)

build-s3: ## Build firmware for ESP32-S3 bench board
	pio run -e $(ENV_S3)

upload: ## Upload firmware to ESP32-C3 SuperMini
	pio run -e $(ENV_C3) -t upload

upload-s3: ## Upload firmware to ESP32-S3 bench board
	pio run -e $(ENV_S3) -t upload

monitor: ## Open serial monitor at 115200 baud
	pio device monitor

flash: ## Upload and open serial monitor
	pio run -e $(ENV_C3) -t upload -t monitor

compiledb: ## Regenerate compile_commands.json for Neovim LSP / clangd
	pio run -e $(ENV_C3) -t compiledb

lsp: compiledb ## Alias for compiledb

format: ## Run clang-format on all C/C++ source and header files
	clang-format -i $(CPP_FILES)

clean: ## Clean PlatformIO build output
	pio run -t clean
