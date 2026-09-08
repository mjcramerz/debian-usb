package app

type RuntimeConfig struct {
	AppName                       string            `json:"app_name"`
	AppVersion                    string            `json:"app_version"`
	ConfigPath                    string            `json:"config_path"`
	DefaultPersistenceSizeGiB     int               `json:"default_persistence_size_gib"`
	DefaultBootPolicy             string            `json:"default_boot_policy"`
	DefaultInstallerPolicy        string            `json:"default_installer_policy"`
	DefaultLiveToram              bool              `json:"default_live_toram"`
	DefaultLiveMemGiB             int               `json:"default_live_mem_gib"`
	DefaultLiveHooks              bool              `json:"default_live_hooks"`
	DefaultLiveArgsHooks          string            `json:"default_live_args_hooks"`
	DefaultPreseedPublicURL       string            `json:"default_preseed_public_url"`
	DefaultPreseedPublicArgs      string            `json:"default_preseed_public_args"`
	DefaultPreseedInternalArgs    string            `json:"default_preseed_internal_args"`
	DefaultPartitionLabels        map[string]string `json:"default_partition_labels"`
	SharedLiveBaseKernelArgs      string            `json:"shared_live_base_kernel_args"`
	BootPolicyBalancedKernelArgs  string            `json:"boot_policy_balanced_kernel_args"`
	BootPolicyPerformanceArgs     string            `json:"boot_policy_performance_kernel_args"`
	BootPolicyHardenedArgs        string            `json:"boot_policy_hardened_kernel_args"`
	InstallerPolicyPreserveArgs   string            `json:"installer_policy_preserve_kernel_args"`
	DefaultLiveKernelExtras       string            `json:"default_live_kernel_extras"`
	DefaultInstallerKernelExtras  string            `json:"default_installer_kernel_extras"`
	DefaultForensicsKernelExtras  string            `json:"default_forensics_kernel_extras"`
	ProfileFallbackLiveKernelArgs map[string]string `json:"profile_fallback_live_kernel_args"`
	ProfileLiveKernelExtras       map[string]string `json:"profile_live_kernel_extras"`
	ProfileForensicsKernelExtras  map[string]string `json:"profile_forensics_kernel_extras"`
	ProfileInstallerKernelExtras  map[string]string `json:"profile_installer_kernel_extras"`
	ProfilePreseedURLs            map[string]string `json:"profile_preseed_urls"`
	ManagedSourceURLs             map[string]string `json:"managed_source_urls"`
	ExtraValues                   map[string]string `json:"extra_values,omitempty"`
}

type ISOOption struct {
	Name       string `json:"name"`
	Path       string `json:"path"`
	Source     string `json:"source"`
	SizeHuman  string `json:"size_human"`
	ModifiedAt string `json:"modified_at"`
}

type Device struct {
	Name       string `json:"name"`
	Path       string `json:"path"`
	Model      string `json:"model"`
	Vendor     string `json:"vendor"`
	Serial     string `json:"serial"`
	Transport  string `json:"transport"`
	SizeBytes  int64  `json:"size_bytes"`
	SizeHuman  string `json:"size_human"`
	UUID       string `json:"uuid"`
	PTUUID     string `json:"ptuuid"`
	Removable  bool   `json:"removable"`
	Mounted    bool   `json:"mounted"`
	SystemDisk bool   `json:"system_disk"`
}

type CreateRequest struct {
	HDMediaPreseedDirs map[string]string
	Preparation        *SourcePreparation

	Profile                     string
	SourceRole                  string
	WriteMode                   string
	ISOPath                     string
	Inspection                  ISOInspection
	SecureBootTrust             string
	UseCustomGrubMenu           bool
	PreserveUpstreamGrubEntries bool
	Preseed                     bool
	OfflinePreseedSourceDir     string
	LiveToram                   bool
	Persistence                 bool
	PersistenceMode             string
	PersistenceSizeGiB          int
	LiveToolGroups              []string
	MenuLabel                   string
	KernelArgs                  string
	KernelPath                  string
	InitrdPath                  string
}

type MultiOSRequest struct {
	SecureBootTrust             string
	UseCustomGrubMenu           bool
	PreserveUpstreamGrubEntries bool
	ProfileOfflinePreseedDirs   map[string]string
	Items                       []MultiOSRequestItem
}

type MultiOSRequestItem struct {
	HDMediaPreseedDirs map[string]string
	Preparation        *SourcePreparation

	Profile            string
	SourceRole         string
	ISOPath            string
	Inspection         ISOInspection
	Preseed            bool
	LiveToram          bool
	Persistence        bool
	PersistenceMode    string
	PersistenceSizeGiB int
	LiveToolGroups     []string
	MenuLabel          string
	KernelArgs         string
	KernelPath         string
	InitrdPath         string
}

type ISOInspection struct {
	Pending bool `json:"pending,omitempty"`

	ISOPath                      string   `json:"iso_path"`
	VolumeID                     string   `json:"volume_id"`
	MediaClass                   string   `json:"media_class"`
	Firmware                     []string `json:"firmware"`
	ManagedSupported             bool     `json:"managed_supported"`
	SupportsPersistence          bool     `json:"supports_persistence"`
	SupportsEncryptedPersistence bool     `json:"supports_encrypted_persistence"`
	ManagedPayloadLayout         string   `json:"managed_payload_layout"`
	TopLevelEntries              []string `json:"top_level_entries"`
	BestLiveTitle                string   `json:"best_live_title"`
	BestInstallerTitle           string   `json:"best_installer_title"`
	Warnings                     []string `json:"warnings"`
}

type CreatePlan struct {
	HDMediaPreseedDirs map[string]string `json:"hd_media_preseed_dirs,omitempty"`
	TargetDevice       *Device           `json:"target_device,omitempty"`

	Preparation *SourcePreparation `json:"source_preparation,omitempty"`

	Title                       string   `json:"title"`
	Profile                     string   `json:"profile"`
	SourceRole                  string   `json:"source_role"`
	WriteMode                   string   `json:"write_mode"`
	ISOPath                     string   `json:"iso_path"`
	Strategy                    string   `json:"strategy"`
	MediaClass                  string   `json:"media_class"`
	Firmware                    []string `json:"firmware"`
	ManagedSupported            bool     `json:"managed_supported"`
	ManagedPayloadLayout        string   `json:"managed_payload_layout"`
	SupportsManaged             bool     `json:"supports_managed"`
	SupportsPersistence         bool     `json:"supports_persistence"`
	SupportsEncrypted           bool     `json:"supports_encrypted_persistence"`
	SecureBootTrust             string   `json:"secure_boot_trust"`
	UseCustomGrubMenu           bool     `json:"use_custom_grub_menu"`
	PreserveUpstreamGrubEntries bool     `json:"preserve_upstream_grub_entries"`
	Preseed                     bool     `json:"preseed"`
	OfflinePreseedSourceDir     string   `json:"offline_preseed_source_dir"`
	LiveToram                   bool     `json:"live_toram"`
	Persistence                 bool     `json:"persistence"`
	PersistenceMode             string   `json:"persistence_mode"`
	PersistenceSizeGiB          int      `json:"persistence_size_gib"`
	LiveToolGroups              []string `json:"live_tool_groups"`
	DefaultPersistenceSizeGiB   int      `json:"default_persistence_size_gib"`
	BootPolicy                  string   `json:"boot_policy"`
	InstallerPolicy             string   `json:"installer_policy"`
	PreseedURL                  string   `json:"preseed_url"`
	MenuLabel                   string   `json:"menu_label"`
	KernelArgs                  string   `json:"kernel_args"`
	KernelPath                  string   `json:"kernel_path"`
	InitrdPath                  string   `json:"initrd_path"`
	ESPLabel                    string   `json:"esp_label"`
	PersistenceFSLabel          string   `json:"persistence_fs_label"`
	PersistencePartLabel        string   `json:"persistence_partlabel"`
	PayloadFSLabel              string   `json:"payload_fs_label"`
	PayloadPartLabel            string   `json:"payload_partlabel"`
	PayloadISOName              string   `json:"payload_iso_name"`
	TopLevelEntries             []string `json:"top_level_entries"`
	Notes                       []string `json:"notes"`
}

type MultiOSPlan struct {
	TargetDevice *Device `json:"target_device,omitempty"`

	SchemaVersion               int               `json:"schema_version"`
	Title                       string            `json:"title"`
	WriteMode                   string            `json:"write_mode"`
	SecureBootTrust             string            `json:"secure_boot_trust"`
	ESPLabel                    string            `json:"esp_label"`
	DataFSLabel                 string            `json:"data_fs_label"`
	DataPartLabel               string            `json:"data_partlabel"`
	UseCustomGrubMenu           bool              `json:"use_custom_grub_menu"`
	PreserveUpstreamGrubEntries bool              `json:"preserve_upstream_grub_entries"`
	Items                       []MultiOSPlanItem `json:"items"`
	Notes                       []string          `json:"notes"`
}

type MultiOSPlanItem struct {
	HDMediaPreseedDirs map[string]string  `json:"hd_media_preseed_dirs,omitempty"`
	Preparation        *SourcePreparation `json:"source_preparation,omitempty"`

	ID                      string   `json:"id"`
	Profile                 string   `json:"profile"`
	SourceRole              string   `json:"source_role"`
	Title                   string   `json:"title"`
	ISOPath                 string   `json:"iso_path"`
	MediaClass              string   `json:"media_class"`
	Firmware                []string `json:"firmware"`
	ManagedPayloadLayout    string   `json:"managed_payload_layout"`
	Preseed                 bool     `json:"preseed"`
	LiveToram               bool     `json:"live_toram"`
	Persistence             bool     `json:"persistence"`
	PersistenceMode         string   `json:"persistence_mode"`
	PersistenceSizeGiB      int      `json:"persistence_size_gib"`
	LiveToolGroups          []string `json:"live_tool_groups"`
	PersistenceFSLabel      string   `json:"persistence_fs_label"`
	PersistencePartLabel    string   `json:"persistence_partlabel"`
	PayloadFSLabel          string   `json:"payload_fs_label"`
	PayloadPartLabel        string   `json:"payload_partlabel"`
	PayloadISOName          string   `json:"payload_iso_name"`
	OfflinePreseedSourceDir string   `json:"offline_preseed_source_dir"`
	MenuLabel               string   `json:"menu_label"`
	KernelArgs              string   `json:"kernel_args"`
	KernelPath              string   `json:"kernel_path"`
	InitrdPath              string   `json:"initrd_path"`
	TopLevelEntries         []string `json:"top_level_entries"`
	SupportsEncrypted       bool     `json:"supports_encrypted_persistence"`
	BestLiveTitle           string   `json:"best_live_title"`
	BestInstallerTitle      string   `json:"best_installer_title"`
}

type PlannedExecution struct {
	SchemaVersion    int          `json:"schema_version"`
	RunID            string       `json:"run_id"`
	Kind             string       `json:"kind"`
	Title            string       `json:"title"`
	CreatedAt        string       `json:"created_at"`
	UpdatedAt        string       `json:"updated_at"`
	LastReviewedAt   string       `json:"last_reviewed_at,omitempty"`
	LastExecutedAt   string       `json:"last_executed_at,omitempty"`
	TargetDevicePath string       `json:"target_device_path"`
	SinglePlan       *CreatePlan  `json:"single_plan,omitempty"`
	MultiOSPlan      *MultiOSPlan `json:"multi_os_plan,omitempty"`

	path string
}

type BuildISOPlan struct {
	SchemaVersion                  int      `json:"schema_version"`
	Distro                         string   `json:"distro"`
	OutputDir                      string   `json:"output_dir"`
	ImageName                      string   `json:"image_name"`
	Suite                          string   `json:"suite"`
	Architecture                   string   `json:"architecture"`
	ArchiveAreas                   []string `json:"archive_areas"`
	MirrorBootstrap                string   `json:"mirror_bootstrap"`
	MirrorChroot                   string   `json:"mirror_chroot"`
	MirrorBinary                   string   `json:"mirror_binary"`
	MirrorBinarySecurity           string   `json:"mirror_binary_security"`
	MirrorDebianInstaller          string   `json:"mirror_debian_installer"`
	KeyringPackages                []string `json:"keyring_packages"`
	InstallerMode                  string   `json:"installer_mode"`
	IncludeInstallerLauncher       bool     `json:"include_installer_launcher"`
	IncludeNonFreeFirmware         bool     `json:"include_non_free_firmware"`
	BasePackages                   []string `json:"base_packages"`
	ExtraChrootPackages            []string `json:"extra_chroot_packages"`
	ExtraBinaryPackages            []string `json:"extra_binary_packages"`
	LiveModuleSpecPath             string   `json:"live_module_spec_path"`
	LiveDebSpecPath                string   `json:"live_deb_spec_path"`
	LiveUdebSpecPath               string   `json:"live_udeb_spec_path"`
	DIModuleSpecPath               string   `json:"di_module_spec_path"`
	DIDebSpecPath                  string   `json:"di_deb_spec_path"`
	DIUdebSpecPath                 string   `json:"di_udeb_spec_path"`
	LocalDebDir                    string   `json:"local_deb_dir"`
	LocalUdebDir                   string   `json:"local_udeb_dir"`
	InstallerKernelRebuildEnabled  bool     `json:"installer_kernel_rebuild_enabled"`
	InstallerKernelSourceISOPath   string   `json:"installer_kernel_source_iso_path"`
	InstallerKernelModules         []string `json:"installer_kernel_modules"`
	InstallerKernelConfigEntries   []string `json:"installer_kernel_config_entries"`
	InstallerAutoUDEBPackages      []string `json:"installer_auto_udeb_packages"`
	UDEBRebuildSpecPath            string   `json:"udeb_rebuild_spec_path"`
	UDEBRebuildSourceOverlayDir    string   `json:"udeb_rebuild_source_overlay_dir"`
	PreseedPath                    string   `json:"preseed_path"`
	InstallerIncludeDir            string   `json:"installer_include_dir"`
	LiveIncludeDir                 string   `json:"live_include_dir"`
	BinaryIncludeDir               string   `json:"binary_include_dir"`
	BootloaderOverrideDir          string   `json:"bootloader_override_dir"`
	InstallerDistribution          string   `json:"installer_distribution"`
	InstallerBootAppend            string   `json:"installer_boot_append"`
	LiveBootAppend                 string   `json:"bootappend_live"`
	ISOApplication                 string   `json:"iso_application"`
	ISOPreparer                    string   `json:"iso_preparer"`
	ISOPublisher                   string   `json:"iso_publisher"`
	ISOVolume                      string   `json:"iso_volume"`
	EROFSInstallerPolicy           string   `json:"erofs_installer_component_policy"`
	EROFSInstallerComponents       []string `json:"erofs_installer_components"`
	DirectDIBuildEnabled           bool     `json:"direct_di_build_enabled"`
	DirectDIBuildSourceMode        string   `json:"direct_di_build_source_mode"`
	DirectDIBuildSourceTree        string   `json:"direct_di_build_source_tree"`
	DirectDIBuildSourcePackage     string   `json:"direct_di_build_source_package"`
	DirectDIBuildTargets           []string `json:"direct_di_build_targets"`
	DirectDIBuildDepPackages       []string `json:"direct_di_build_dep_packages"`
	DirectDIBuildReallyCleanBefore bool     `json:"direct_di_build_reallyclean_before"`
	DirectDIBuildReallyCleanAfter  bool     `json:"direct_di_build_reallyclean_after"`
	KernelMode                     string   `json:"kernel_mode"`
	KernelPackageStub              string   `json:"kernel_package_stub"`
	KernelFlavours                 []string `json:"kernel_flavours"`
	KernelDebDir                   string   `json:"kernel_deb_dir"`
	CustomAPTRepo                  string   `json:"custom_apt_repo"`
	CustomBinaryAPTRepo            string   `json:"custom_binary_apt_repo"`
	CustomAPTRepoKeyPath           string   `json:"custom_apt_repo_key_path"`
	CustomAPTRepoPin               string   `json:"custom_apt_repo_pin"`
	RootFSFormat                   string   `json:"rootfs_format"`
	EROFSCompressor                string   `json:"erofs_compressor"`
	EROFSExtraArgs                 string   `json:"erofs_extra_args"`
	FilesystemModuleEntries        []string `json:"filesystem_module_entries"`
	InitramfsModules               []string `json:"initramfs_modules"`
	KernelInspectionModules        []string `json:"kernel_inspection_modules"`
	KernelTargetVersion            string   `json:"kernel_target_version"`
	KernelModuleTreeDir            string   `json:"kernel_module_tree_dir"`
	KernelDownloadIfMissing        bool     `json:"kernel_download_if_missing"`
	KernelConfigSymbols            []string `json:"kernel_config_symbols"`
	ModuleAliasCandidates          []string `json:"module_alias_candidates"`
	LiveToolGroups                 []string `json:"live_tool_groups"`
	StorageToolPackages            []string `json:"storage_tool_packages"`
	CleanupMode                    string   `json:"cleanup_mode"`
}

type BuildISOResult struct {
	RunID                      string   `json:"run_id"`
	ISOPath                    string   `json:"iso_path"`
	WorkspaceDir               string   `json:"workspace_dir"`
	LogPath                    string   `json:"log_path"`
	ManifestPath               string   `json:"manifest_path"`
	InstallerAuditPath         string   `json:"installer_audit_path"`
	UDEBRebuildManifestPath    string   `json:"udeb_rebuild_manifest_path"`
	InstallerLocalUdebRepoPath string   `json:"installer_localudeb_repo_path"`
	DirectDIBuildManifestPath  string   `json:"direct_di_build_manifest_path"`
	Warnings                   []string `json:"warnings"`
}

type BuildISOKernelInspectRequest struct {
	KernelVersion         string   `json:"kernel_version"`
	ModuleNames           []string `json:"module_names"`
	ModuleAliasCandidates []string `json:"module_alias_candidates"`
	ConfigSymbols         []string `json:"config_symbols"`
	ModuleTreeDir         string   `json:"module_tree_dir"`
	DownloadIfMissing     bool     `json:"download_if_missing"`
}

type BuildISOKernelInspectModule struct {
	Name  string `json:"name"`
	Found bool   `json:"found"`
	Path  string `json:"path"`
}

type BuildISOKernelInspectConfigSymbol struct {
	Symbol string `json:"symbol"`
	Value  string `json:"value"`
	Line   string `json:"line"`
}

type BuildISOKernelInspectResult struct {
	KernelVersion       string                              `json:"kernel_version"`
	ModuleTreeDir       string                              `json:"module_tree_dir"`
	ModuleCount         int                                 `json:"module_count"`
	ModuleTreeSearched  []string                            `json:"module_tree_searched"`
	ConfigPath          string                              `json:"config_path"`
	ConfigPathsSearched []string                            `json:"config_paths_searched"`
	DownloadAttempted   bool                                `json:"download_attempted"`
	DownloadUsed        bool                                `json:"download_used"`
	DownloadCacheDir    string                              `json:"download_cache_dir"`
	DownloadedPackages  []string                            `json:"downloaded_packages"`
	Modules             []BuildISOKernelInspectModule       `json:"modules"`
	ConfigSymbols       []BuildISOKernelInspectConfigSymbol `json:"config_symbols"`
	Notes               []string                            `json:"notes"`
}

type RebuildInstallerISOInspection struct {
	SourcePath             string   `json:"source_path"`
	SourceType             string   `json:"source_type"`
	VolumeID               string   `json:"volume_id"`
	MediaClass             string   `json:"media_class"`
	Architecture           string   `json:"architecture"`
	Firmware               []string `json:"firmware"`
	BestLiveTitle          string   `json:"best_live_title"`
	BestInstallerTitle     string   `json:"best_installer_title"`
	InstallerKernelPath    string   `json:"installer_kernel_path"`
	InstallerInitrdPath    string   `json:"installer_initrd_path"`
	InstallerKernelVersion string   `json:"installer_kernel_version"`
	LiveKernelPath         string   `json:"live_kernel_path"`
	LiveInitrdPath         string   `json:"live_initrd_path"`
	LiveRootFSPath         string   `json:"live_rootfs_path"`
	Warnings               []string `json:"warnings"`
}

type RebuildInstallerISOPlan struct {
	SchemaVersion          int      `json:"schema_version"`
	Distro                 string   `json:"distro"`
	SourceISOPath          string   `json:"source_iso_path"`
	OutputDir              string   `json:"output_dir"`
	ImageName              string   `json:"image_name"`
	Scope                  string   `json:"scope"`
	Action                 string   `json:"action"`
	Architecture           string   `json:"architecture"`
	InstallerKernelModules []string `json:"installer_kernel_modules"`
	InstallerUdebPackages  []string `json:"installer_udeb_packages"`
	LiveDebPackages        []string `json:"live_deb_packages"`
	TargetKernelVersion    string   `json:"target_kernel_version"`
}

type RebuildInstallerISOResult struct {
	RunID              string   `json:"run_id"`
	ISOPath            string   `json:"iso_path"`
	WorkspaceDir       string   `json:"workspace_dir"`
	LogPath            string   `json:"log_path"`
	ManifestPath       string   `json:"manifest_path"`
	StagedUdebRepoPath string   `json:"staged_udeb_repo_path"`
	ModifiedPaths      []string `json:"modified_paths"`
	Warnings           []string `json:"warnings"`
}
