package app

const (
	profileDebian        = "debian"
	profileUbuntuDesktop = "ubuntu-desktop"
	profileUbuntuServer  = "ubuntu-server"
	profileKaliLinux     = "kali-linux"
	profileKaliPurple    = "kali-purple"
	profileTails         = "tails"
)

const (
	writeModeDirect  = "direct"
	writeModeManaged = "managed"
	writeModeMultiOS = "multi-os"
)

const (
	buildISOSchemaVersion = 1
)

const (
	rebuildInstallerISOSchemaVersion = 1
)

const (
	installerModuleSourceStrategyHostKernel = "host-kernel"
	installerModuleSourceStrategySourceUDEB = "source-udeb"
)

const (
	buildISODistroDebian = "debian"
	buildISODistroUbuntu = "ubuntu"
	buildISODistroKali   = "kali-linux"
)

const (
	rebuildInstallerISODistroDebian = "debian"
)

const (
	buildISOInstallerModeNone    = "none"
	buildISOInstallerModeNetinst = "netinst"
	buildISOInstallerModeLive    = "live"
)

const (
	rebuildInstallerISOScopeDI       = "d-i"
	rebuildInstallerISOScopeLiveHost = "live-host"
)

const (
	rebuildInstallerISOActionAddKernelModules = "add-kernel-modules"
	rebuildInstallerISOActionAddUdebPackages  = "add-udeb-packages"
	rebuildInstallerISOActionUpdateKernel     = "update-kernel"
	rebuildInstallerISOActionReplaceKernel    = "replace-kernel"
	rebuildInstallerISOActionAddDebPackages   = "add-deb-packages"
)

const (
	buildISOKernelModeStockDebian = "stock-debian"
	buildISOKernelModePackageStub = "repo-package-stub"
	buildISOKernelModeLocalDebDir = "local-kernel-deb-dir"
	buildISOKernelModeCustomRepo  = "custom-apt-repo"
)

const (
	buildISORootFSFormatNone     = "none"
	buildISORootFSFormatSquashFS = "squashfs"
	buildISORootFSFormatEROFS    = "erofs"
)

const (
	buildISOCleanupModePurgeWorkspace = "purge-workspace"
	buildISOCleanupModeKeepWorkspace  = "keep-workspace"
)

const (
	buildISOEROFSInstallerPolicyWarn    = "warn"
	buildISOEROFSInstallerPolicyRequire = "require"
)

const (
	buildISODirectDIBuildSourceModeAptSource = "apt-source"
	buildISODirectDIBuildSourceModeLocalTree = "local-tree"
)

const (
	plannedExecutionSchemaVersion = 1
	plannedExecutionKindSingle    = "single"
	plannedExecutionKindMultiOS   = "multi-os"
)

const (
	multiOSSourceRolePrimary = "primary"
	multiOSSourceRoleNetinst = "netinst"
	multiOSSourceRoleNetboot = "netboot"
)

const (
	persistenceModeNone      = ""
	persistenceModePlain     = "plain"
	persistenceModeEncrypted = "encrypted"
)

const (
	secureBootTrustMOK        = "mok"
	secureBootTrustFirmwareDB = "firmware-db"
)
