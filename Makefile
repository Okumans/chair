.DEFAULT_GOAL := help

ENV_NODE    ?= esp32-c3-supermini
ENV_GATEWAY ?= esp32-s3-gateway

CPP_FILES := $(wildcard src/*.cpp include/*.h)

.PHONY: help build upload monitor flash build-gateway upload-gateway flash-gateway lsp compiledb format clean

help: ## Show available shortcut commands
	@echo -e "Smart Chair Monorepo — Build & Development Shortcuts"
	@echo -e ""
	@echo -e "  \033[33mChair Sensor Node (ESP32-C3 SuperMini):\033[0m"
	@echo -e "    \033[36mmake build\033[0m         Build sensor node firmware"
	@echo -e "    \033[36mmake upload\033[0m        Upload firmware to ESP32-C3"
	@echo -e "    \033[36mmake flash\033[0m         Upload and open monitor on ESP32-C3"
	@echo -e "    \033[36mmake monitor\033[0m       Open serial monitor (115200 baud)"
	@echo -e ""
	@echo -e "  \033[33mDesk Gateway (ESP32-S3 DevKit):\033[0m"
	@echo -e "    \033[36mmake build-gateway\033[0m  Build gateway firmware"
	@echo -e "    \033[36mmake upload-gateway\033[0m Upload firmware to ESP32-S3"
	@echo -e "    \033[36mmake flash-gateway\033[0m  Upload and open monitor on ESP32-S3"
	@echo -e ""
	@echo -e "  \033[33mGeneral / Developer Utilities:\033[0m"
	@echo -e "    \033[36mmake lsp\033[0m           Regenerate compile_commands.json for Neovim"
	@echo -e "    \033[36mmake format\033[0m        Run clang-format on all C/C++ files"
	@echo -e "    \033[36mmake clean\033[0m         Clean PlatformIO build output"
	@echo -e ""

# Chair Sensor Node (C3)
build:
	pio run -e $(ENV_NODE)

upload:
	pio run -e $(ENV_NODE) -t upload

flash:
	pio run -e $(ENV_NODE) -t upload -t monitor

monitor:
	pio device monitor

# Desk Gateway (S3)
build-gateway:
	pio run -e $(ENV_GATEWAY)

upload-gateway:
	pio run -e $(ENV_GATEWAY) -t upload

flash-gateway:
	pio run -e $(ENV_GATEWAY) -t upload -t monitor

# Tooling
compiledb:
	pio run -e $(ENV_NODE) -t compiledb

lsp: compiledb

format:
	clang-format -i $(CPP_FILES)

clean:
	pio run -t clean
