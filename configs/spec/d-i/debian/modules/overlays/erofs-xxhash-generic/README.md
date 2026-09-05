This overlay directory is copied into the Debian `linux` source tree before `dpkg-buildpackage` runs for the `linux-installer-kernel` rebuild path.

Use it for packaging or kernel-config changes that cannot be expressed only by editing `debian/installer/modules/*`.

Suggested contents when you need deeper changes:

- `debian/` packaging overrides
- kernel config fragments or packaging glue that selects `CONFIG_EROFS_FS`, `CONFIG_XXHASH`, or related options
- documentation for why a target kernel version needs different module or package-list handling

The Build ISO flow can override this directory with an absolute `source_overlay_dir` at prompt time.
