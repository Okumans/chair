.DEFAULT_GOAL := help

# Target environment: `sensor` (ESP32-C3 SuperMini) or `gateway` (ESP32-S3 DevKit)
ENV ?= sensor
PORT ?=

CPP_FILES := $(shell find sensor gateway shared tests tools/mac_address -name '*.cpp' -o -name '*.h' 2>/dev/null)

.PHONY: help build upload monitor flash mac lsp compiledb format format-check lint test check setup-hooks clean

help:
	@echo -e "Smart Chair Monorepo — Build & Development Shortcuts"
	@echo -e ""
	@echo -e "  \033[33mActive Target:\033[0m \033[32m$(ENV)\033[0m"
	@echo -e "  (Toggle with \033[36mENV=sensor\033[0m [default] or \033[36mENV=gateway\033[0m)"
	@echo -e ""
	@echo -e "  \033[36mmake build\033[0m   [ENV=...]   Build firmware"
	@echo -e "  \033[36mmake upload\033[0m  [ENV=...]   Upload firmware"
	@echo -e "  \033[36mmake flash\033[0m   [ENV=...]   Upload and open serial monitor"
	@echo -e "  \033[36mmake mac\033[0m     ENV=sensor|gateway PORT=...   Upload MAC-only firmware & monitor"
	@echo -e "  \033[36mmake monitor\033[0m             Open serial monitor (115200 baud)"
	@echo -e "  \033[36mmake lsp\033[0m     [ENV=...]   Regenerate compile_commands.json for Neovim"
	@echo -e "  \033[36mmake format\033[0m              Format C/C++ and Python files"
	@echo -e "  \033[36mmake check\033[0m               Check formatting, Python lint and native tests"
	@echo -e "  \033[36mmake setup-hooks\033[0m         Install the staged-file pre-commit checks"
	@echo -e "  \033[36mmake clean\033[0m               Clean PlatformIO build output"
	@echo -e ""
	@echo -e "  \033[33mExamples:\033[0m"
	@echo -e "    make build                 # Builds sensor node (default)"
	@echo -e "    make build ENV=gateway     # Builds desk gateway"
	@echo -e "    make flash ENV=gateway     # Flashes gateway & opens monitor"
	@echo -e "    make mac ENV=sensor PORT=/dev/ttyACM0  # MAC-only firmware for C3"
	@echo -e "    make mac ENV=gateway PORT=/dev/ttyACM1 # MAC-only firmware for S3"
	@echo -e "    make lsp                   # Updates LSP database for sensor"
	@echo -e "    make lsp ENV=gateway       # Updates LSP database for gateway"
	@echo -e ""

build:
	pio run -e $(ENV)

upload:
	pio run -e $(ENV) -t upload

flash:
	pio run -e $(ENV) -t upload -t monitor

mac:
	@case "$(ENV)" in \
		sensor|gateway) ;; \
		*) echo "ENV must be sensor or gateway" >&2; exit 1 ;; \
	esac
	@test -n "$(PORT)" || { echo "Set PORT to the board's serial device" >&2; exit 1; }
	pio run -e "mac-$(ENV)" -t upload -t monitor --port "$(PORT)"

monitor:
	pio device monitor

compiledb:
	@python3 -c "import json, pathlib, subprocess; \
	cmds = []; \
	subprocess.run(['pio', 'run', '-e', 'sensor', '-t', 'compiledb'], check=True, stdout=subprocess.DEVNULL); \
	cmds.extend(json.load(open('compile_commands.json'))); \
	subprocess.run(['pio', 'run', '-e', 'gateway', '-t', 'compiledb'], check=True, stdout=subprocess.DEVNULL); \
	cmds.extend(json.load(open('compile_commands.json'))); \
	subprocess.run(['pio', 'run', '-e', 'mac-sensor', '-t', 'compiledb'], check=True, stdout=subprocess.DEVNULL); \
	cmds.extend(json.load(open('compile_commands.json'))); \
	subprocess.run(['pio', 'run', '-e', 'mac-gateway', '-t', 'compiledb'], check=True, stdout=subprocess.DEVNULL); \
	cmds.extend(json.load(open('compile_commands.json'))); \
	cmds.extend({'directory': str(pathlib.Path.cwd()), 'file': str(p.resolve()), 'arguments': ['c++', '-std=c++23', *(['-Itests/radio_fakes'] if p.name == 'radio_usb_test.cpp' else []), '-Itests/fakes', '-Isensor/include', '-Ishared/include', '-Ithird_party/glaze/include', '-c', str(p)]} for p in pathlib.Path('tests').glob('*.cpp')); \
	seen = set(); unique = []; \
	[unique.append(c) for c in cmds if c['file'] not in seen and not seen.add(c['file'])]; \
	json.dump(unique, open('compile_commands.json', 'w'), indent=2); \
	print('Regenerated compile_commands.json for firmware, MAC tools and native tests!')"

lsp: compiledb

format:
	clang-format -i $(CPP_FILES)
	ruff format tools tests

format-check:
	clang-format --dry-run --Werror $(CPP_FILES)
	ruff format --check tools tests

lint:
	sh -n .githooks/pre-commit
	ruff check tools tests

test:
	uv run --no-project --with pyserial==3.5 python -m unittest discover -s tests -v

check: format-check lint test

setup-hooks:
	git config --local core.hooksPath .githooks
	@echo "Pre-commit checks installed. Run make format before staging your changes."

clean:
	pio run -t clean
