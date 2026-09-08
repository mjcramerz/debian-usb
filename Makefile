PREFIX ?= /usr
BINDIR ?= $(PREFIX)/bin
ETCDIR ?= /etc/debian-usb
LIBEXECDIR ?= $(PREFIX)/lib/debian-usb
APPNAME = debian-usb

HOST_SCRIPT_ENV = \
	DESTDIR='$(DESTDIR)' \
	PREFIX='$(PREFIX)' \
	BINDIR='$(BINDIR)' \
	ETCDIR='$(ETCDIR)' \
	LIBEXECDIR='$(LIBEXECDIR)'

all: build

build:
	@mkdir -p build
	@go build -o build/$(APPNAME) ./cmd/$(APPNAME)

run: build
	@DEBIAN_USB_CONFIG='$(CURDIR)/configs/debian-usb.conf' \
	DEBIAN_USB_PYTHON_HELPER='$(CURDIR)/scripts/debian-usb-python' \
	DEBIAN_USB_WRITE_HELPER='$(CURDIR)/scripts/write_usb.sh' \
	DEBIAN_USB_BUILD_ISO_HELPER='$(CURDIR)/scripts/build_iso.sh' \
	./build/$(APPNAME)

guard-non-root:
	@if [ "$$(id -u)" -eq 0 ]; then \
		echo "Refusing to run $(APPNAME) from a root make process. Run 'make <target>' as a non-root user; do not use sudo make." >&2; \
		exit 1; \
	fi

install: guard-non-root build
	@if [ -n "$(DESTDIR)" ]; then \
		printf '%s\n' 'debian-usb: planning staged install'; \
		$(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh describe-install; \
		printf '%s\n' 'debian-usb: applying staged install'; \
		$(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh install; \
	else \
		printf '%s\n' 'debian-usb: planning host install'; \
		$(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh describe-install; \
		printf '%s\n' 'debian-usb: installing Debian package prerequisites via sudo'; \
		sh ./scripts/install.sh --deps-only; \
		printf '%s\n' 'debian-usb: applying managed host install via sudo'; \
		sudo env DUSB_INTERNAL_ROOT=1 DUSB_RUNTIME_UID="$$(id -u)" DUSB_RUNTIME_GID="$$(id -g)" $(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh install; \
	fi
	@sh ./scripts/install-git-hooks.sh --install

uninstall: guard-non-root
	@if [ -n "$(DESTDIR)" ]; then \
		printf '%s\n' 'debian-usb: removing staged runtime files'; \
		$(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh uninstall; \
	else \
		printf '%s\n' 'debian-usb: removing managed host runtime files via sudo'; \
		sudo env DUSB_INTERNAL_ROOT=1 $(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh uninstall; \
	fi

nuke: guard-non-root
	@if [ -n "$(DESTDIR)" ]; then \
		printf '%s\n' 'debian-usb: planning staged nuke'; \
		$(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh describe-nuke; \
		printf '%s\n' 'debian-usb: applying staged nuke'; \
		$(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh nuke; \
	else \
		printf '%s\n' 'debian-usb: planning host nuke'; \
		$(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh describe-nuke; \
		printf '%s\n' 'debian-usb: removing managed host assets via sudo'; \
		sudo env DUSB_INTERNAL_ROOT=1 $(HOST_SCRIPT_ENV) sh ./scripts/make-host.sh nuke; \
	fi
	@sh ./scripts/install-git-hooks.sh --remove

install-git-hooks:
	@sh ./scripts/install-git-hooks.sh

check-live-tool-scope: export PYTHONDONTWRITEBYTECODE := 1
check-live-tool-scope:
	@go test ./internal/app -run 'LiveTool|Wireless' -count=1
	@PYTHONWARNDEFAULTENCODING=1 PYTHONWARNINGS='error::EncodingWarning' PYTHONPATH='$(CURDIR)/src/python:$(CURDIR)/tests/python' python3 -m unittest -v test_live_tools test_live_tool_scope

check-installer-archives: export PYTHONDONTWRITEBYTECODE := 1
check-installer-archives:
	@command -v fakeroot >/dev/null || { echo 'Installer archive regression tests require fakeroot (apt install fakeroot).' >&2; exit 1; }
	@PYTHONWARNDEFAULTENCODING=1 PYTHONWARNINGS='error::EncodingWarning' PYTHONPATH='$(CURDIR)/src/python:$(CURDIR)/tests/python' python3 -m unittest -v test_installer_profile_archives test_installer_preseed test_installer_profiles

check: export PYTHONDONTWRITEBYTECODE := 1
check:
	@command -v fakeroot >/dev/null || { echo 'Installer archive regression tests require fakeroot (apt install fakeroot).' >&2; exit 1; }
	@go test ./...
	@PYTHONWARNDEFAULTENCODING=1 PYTHONWARNINGS='error::EncodingWarning' PYTHONPATH='$(CURDIR)/src/python' python3 -m unittest discover -s tests/python
	@set -e; for script in secrets.sh .githooks/pre-commit .githooks/pre-push scripts/install-git-hooks.sh scripts/debian-usb-python scripts/install.sh scripts/make-host.sh scripts/write_usb.sh scripts/build_iso.sh config-hooks/0500-apt-live-medium.sh config-hooks/1000-network-wifi.sh tests/shell/test_live_apt_hook.sh tests/shell/test_live_wifi_hook.sh tests/shell/test_multios_live_tools.sh tests/shell/test_device_release.sh tests/shell/test_installer_profile_layout.sh tests/shell/test_multios_persistence_layout.sh; do dash -n "$$script"; done
	@sh tests/shell/test_live_apt_hook.sh >/dev/null
	@sh tests/shell/test_live_wifi_hook.sh >/dev/null
	@sh tests/shell/test_multios_live_tools.sh >/dev/null
	@sh tests/shell/test_device_release.sh >/dev/null
	@sh tests/shell/test_installer_profile_layout.sh >/dev/null
	@sh tests/shell/test_multios_persistence_layout.sh >/dev/null
	@grep -F 'initramfs-tools-core' configs/install.env >/dev/null
	@set -eu; \
	tmpdir="$$(mktemp -d)"; \
	repo_fixture="$$tmpdir/repo"; \
	trap 'rm -rf "$$tmpdir"' 0; \
	env -u DESTDIR sh ./scripts/make-host.sh describe-install | grep -F 'Privileges: root required for host writes; make will prompt through sudo' >/dev/null; \
	env -u DESTDIR sh ./scripts/make-host.sh describe-nuke | grep -F 'Result: managed runtime files, logs, and state are permanently removed; the download root is preserved' >/dev/null; \
	install -d "$$repo_fixture/initrd/debian" "$$repo_fixture/initrd/kali" "$$repo_fixture/build"; \
	cp -a "$(CURDIR)/scripts" "$$repo_fixture/scripts"; \
	cp -a "$(CURDIR)/configs" "$$repo_fixture/configs"; \
	cp -a "$(CURDIR)/src" "$$repo_fixture/src"; \
	cp -a "$(CURDIR)/config-hooks" "$$repo_fixture/config-hooks"; \
	cp -a "$(CURDIR)/README.md" "$$repo_fixture/README.md"; \
	cp -a "$(CURDIR)/initrd/debian/live" "$$repo_fixture/initrd/debian/live"; \
	cp -a "$(CURDIR)/initrd/kali/live" "$$repo_fixture/initrd/kali/live"; \
	go build -o "$$repo_fixture/build/debian-usb" ./cmd/debian-usb; \
	( cd "$$tmpdir" && DESTDIR="$$tmpdir/stage" sh "$$repo_fixture/scripts/make-host.sh" install >/dev/null ); \
		test -x "$$tmpdir/stage/usr/bin/debian-usb"; \
		test -f "$$tmpdir/stage/usr/lib/debian-usb/python/debian_usb/cli.py"; \
		test -f "$$tmpdir/stage/usr/lib/debian-usb/python/debian_usb/installer_preseed.py"; \
		test -x "$$tmpdir/stage/usr/lib/debian-usb/debian-usb-build-iso"; \
		test -x "$$tmpdir/stage/usr/lib/debian-usb/config-hooks/0500-apt-live-medium.sh"; \
		test -x "$$tmpdir/stage/usr/lib/debian-usb/config-hooks/live-apt-repository.py"; \
		test -x "$$tmpdir/stage/usr/lib/debian-usb/config-hooks/1000-network-wifi.sh"; \
		test -f "$$tmpdir/stage/usr/lib/debian-usb/python/debian_usb/live_wifi.py"; \
		test -f "$$tmpdir/stage/usr/lib/debian-usb/python/debian_usb/live_packages.py"; \
		test -f "$$tmpdir/stage/usr/lib/debian-usb/initrd/kali/live/live.env"; \
		test "$$(stat -c '%a' "$$tmpdir/stage/usr/lib/debian-usb/initrd/kali/live/live.env")" = 600; \
		test -f "$$tmpdir/stage/usr/lib/debian-usb/initrd/debian/live/live.env"; \
		test "$$(stat -c '%a' "$$tmpdir/stage/usr/lib/debian-usb/initrd/debian/live/live.env")" = 600; \
		grep -q '^LIVE_WIFI_PASSPHRASE=' "$$tmpdir/stage/usr/lib/debian-usb/initrd/debian/live/live.env"; \
		! grep -q '^PRESEED_WIFI_PASSPHRASE=' "$$tmpdir/stage/usr/lib/debian-usb/initrd/debian/live/live.env"; \
		test -x "$$tmpdir/stage/usr/lib/debian-usb/initrd/debian/live/scripts/init-bottom/debian-usb-live-env"; \
			test -f "$$tmpdir/stage/usr/lib/debian-usb/spec/live/admin-tools.json"; \
			test "$$(stat -c '%a' "$$tmpdir/stage/usr/lib/debian-usb/spec/live/admin-tools.json")" = 644; \
			test "$$(stat -c '%a' "$$tmpdir/stage/usr/lib/debian-usb/python/debian_usb/live_tools.py")" = 644; \
			test "$$(stat -c '%a' "$$tmpdir/stage/usr/lib/debian-usb/python/debian_usb/live_hooks.py")" = 644; \
			PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$$tmpdir/stage/usr/lib/debian-usb/python" python3 -c 'from debian_usb.live_tools import load_live_tool_profile; assert len(load_live_tool_profile()["packages"]) > 0'; \
			PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$$tmpdir/stage/usr/lib/debian-usb/python" python3 -c 'from debian_usb.live_tools import live_tool_packages_for_profile as p; assert "wifite" not in p("debian")[0]; assert "wireless_security" not in p("debian")[1]["selected_groups"]; assert "wifite" in p("kali-linux")[0]; assert "kali-tools-wireless" in p("kali-linux")[0]'; \
			test -f "$$tmpdir/stage/usr/lib/debian-usb/spec/d-i/debian/modules/erofs-xxhash-generic.json"; \
			test -d "$$tmpdir/stage/data/cfg/debian-usb/planned-executions"; \
			test ! -e "$$tmpdir/stage/data/downloads/debian-usb"

clean:
	@rm -rf build

.PHONY: all build run guard-non-root install uninstall nuke install-git-hooks check check-live-tool-scope check-installer-archives clean
