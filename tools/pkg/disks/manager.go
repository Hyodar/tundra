package disks

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/Hyodar/tundra/tools/pkg/exec"
)

// KeyProvider supplies encryption keys for disk setup.
type KeyProvider interface {
	Get(ctx context.Context) (string, error)
}

// ManagedDisk tracks the state of a single disk managed by the Manager.
type ManagedDisk struct {
	Name         string
	Config       DiskConfig
	DevicePath   string
	MapperName   string // e.g. crypt_disk_persistent
	MapperDevice string // /dev/mapper/crypt_disk_persistent
	Initialized  bool
}

// Manager coordinates the setup of all configured disks.
type Manager struct {
	disks        map[string]*ManagedDisk
	config       *Config
	keyProvider  KeyProvider            // default key provider
	keyProviders map[string]KeyProvider // per-key-name providers
	luks         *LuksOps
	fs           *FilesystemOps
	runner       exec.Runner

	// findDisk is a hook so tests can inject a fake finder.
	findDisk func(cfg DiskConfig) (string, error)
}

// NewManager creates a Manager from the given configuration.
// keyProvider is the default provider. Use WithKeyProviders to register
// per-key-name providers for multi-key setups.
func NewManager(cfg *Config, keyProvider KeyProvider, runner exec.Runner) *Manager {
	m := &Manager{
		disks:        make(map[string]*ManagedDisk),
		config:       cfg,
		keyProvider:  keyProvider,
		keyProviders: make(map[string]KeyProvider),
		luks:         NewLuksOps(runner),
		fs:           NewFilesystemOps(runner),
		runner:       runner,
	}

	// Default findDisk uses the real DiskFinder factory.
	m.findDisk = func(dcfg DiskConfig) (string, error) {
		finder, err := CreateDiskFinder(dcfg)
		if err != nil {
			return "", err
		}
		return finder.Find()
	}

	for name, dcfg := range cfg.Disks {
		mapperName := "crypt_disk_" + name
		m.disks[name] = &ManagedDisk{
			Name:         name,
			Config:       dcfg,
			MapperName:   mapperName,
			MapperDevice: "/dev/mapper/" + mapperName,
		}
	}

	return m
}

// RegisterKeyProvider associates a named key provider for use with disks that
// reference that key name via encryption_key.
func (m *Manager) RegisterKeyProvider(keyName string, kp KeyProvider) {
	m.keyProviders[keyName] = kp
}

// getKeyProvider returns the key provider for the given key name, falling back
// to the default provider.
func (m *Manager) getKeyProvider(keyName string) KeyProvider {
	if kp, ok := m.keyProviders[keyName]; ok {
		return kp
	}
	return m.keyProvider
}

// SetupAll sets up every configured disk.
func (m *Manager) SetupAll(ctx context.Context) error {
	for name := range m.config.Disks {
		if err := m.SetupDisk(ctx, name); err != nil {
			return fmt.Errorf("setup all: disk %q: %w", name, err)
		}
	}
	return nil
}

// SetupDisk performs the full setup flow for a single named disk.
//
// Decision tree:
//  1. Find the physical device using the configured strategy.
//  2. If encryption_key is set (encrypted disk):
//     a. Check if device is already LUKS.
//     b. Determine whether to format based on the format policy.
//     c. Format path: get key -> FormatLuks -> StoreInitToken -> OpenLuks ->
//     CreateFilesystem -> MountDevice -> CreateMountDirs.
//     d. Mount existing path: get key -> OpenLuks -> MountDevice.
//  3. If encryption_key is empty (plain disk):
//     a. Check if device has a filesystem; format or mount accordingly.
func (m *Manager) SetupDisk(ctx context.Context, name string) error {
	md, ok := m.disks[name]
	if !ok {
		return fmt.Errorf("setup disk: unknown disk %q", name)
	}

	slog.InfoContext(ctx, "setting up disk", "name", name)

	// Step 1: Find the physical device.
	devicePath, err := m.findDisk(md.Config)
	if err != nil {
		return fmt.Errorf("setup disk %q: find device: %w", name, err)
	}
	md.DevicePath = devicePath
	slog.InfoContext(ctx, "found device", "name", name, "device", devicePath)

	if m.isEncrypted(md.Config) {
		return m.setupEncryptedDisk(ctx, md)
	}

	return m.setupPlainDisk(ctx, md)
}

// setupEncryptedDisk handles the LUKS-encrypted disk workflow.
func (m *Manager) setupEncryptedDisk(ctx context.Context, md *ManagedDisk) error {
	isLuks, err := m.luks.IsLuksDevice(ctx, md.DevicePath)
	if err != nil {
		return fmt.Errorf("setup encrypted disk %q: %w", md.Name, err)
	}

	initialized := false
	if isLuks {
		initialized, err = m.luks.IsInitialized(ctx, md.DevicePath)
		if err != nil {
			return fmt.Errorf("setup encrypted disk %q: check init: %w", md.Name, err)
		}
	}
	md.Initialized = initialized

	shouldFormat := m.shouldFormat(md.Config.Format, isLuks, initialized)

	if shouldFormat {
		return m.formatAndMountEncrypted(ctx, md)
	}

	if !isLuks {
		return fmt.Errorf("setup encrypted disk %q: device is not LUKS and format policy is %q", md.Name, md.Config.Format)
	}

	return m.mountExistingEncrypted(ctx, md)
}

// shouldFormat determines whether the disk should be formatted based on the
// format policy and the current state.
func (m *Manager) shouldFormat(policy string, isLuks, initialized bool) bool {
	switch policy {
	case "always":
		return true
	case "on_initialize":
		return !initialized
	case "on_fail":
		// Format only if it is not already a valid LUKS device.
		return !isLuks
	case "never":
		return false
	default:
		return false
	}
}

// formatAndMountEncrypted runs the full format path for an encrypted disk.
func (m *Manager) formatAndMountEncrypted(ctx context.Context, md *ManagedDisk) error {
	key, err := m.getEncryptionKey(ctx, md)
	if err != nil {
		return fmt.Errorf("disk %q: get encryption key: %w", md.Name, err)
	}

	if err := m.luks.FormatLuks(ctx, md.DevicePath, key); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if err := m.luks.StoreInitToken(ctx, md.DevicePath, md.Name); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if err := m.luks.OpenLuks(ctx, md.DevicePath, md.MapperName, key); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if err := m.fs.CreateFilesystem(ctx, md.MapperDevice); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if err := m.fs.MountDevice(ctx, md.MapperDevice, md.Config.MountAt); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	dirs := md.Config.EffectiveDirs()
	if err := CreateMountDirs(md.Config.MountAt, dirs); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	md.Initialized = true
	slog.InfoContext(ctx, "encrypted disk formatted and mounted", "name", md.Name, "device", md.DevicePath)
	return nil
}

// mountExistingEncrypted opens and mounts an already-formatted LUKS device.
func (m *Manager) mountExistingEncrypted(ctx context.Context, md *ManagedDisk) error {
	key, err := m.getEncryptionKey(ctx, md)
	if err != nil {
		return fmt.Errorf("disk %q: get encryption key: %w", md.Name, err)
	}

	if err := m.luks.OpenLuks(ctx, md.DevicePath, md.MapperName, key); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if err := m.fs.MountDevice(ctx, md.MapperDevice, md.Config.MountAt); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	slog.InfoContext(ctx, "encrypted disk mounted", "name", md.Name, "device", md.DevicePath)
	return nil
}

// setupPlainDisk handles non-encrypted disk setup.
func (m *Manager) setupPlainDisk(ctx context.Context, md *ManagedDisk) error {
	// For plain disks, probe whether a filesystem already exists by trying
	// to mount. This prevents on_initialize from reformatting on every run.
	hasFS := false
	if md.Config.Format == "on_initialize" || md.Config.Format == "on_fail" {
		err := m.fs.MountDevice(ctx, md.DevicePath, md.Config.MountAt)
		if err == nil {
			hasFS = true
			// Unmount so the normal flow can decide.
			_ = m.fs.UnmountDevice(ctx, md.Config.MountAt)
		}
	}

	shouldFormat := m.shouldFormat(md.Config.Format, hasFS, hasFS)

	if shouldFormat {
		if err := m.fs.CreateFilesystem(ctx, md.DevicePath); err != nil {
			return fmt.Errorf("disk %q: %w", md.Name, err)
		}
	}

	if err := m.fs.MountDevice(ctx, md.DevicePath, md.Config.MountAt); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	dirs := md.Config.EffectiveDirs()
	if err := CreateMountDirs(md.Config.MountAt, dirs); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	md.Initialized = true
	slog.InfoContext(ctx, "plain disk mounted", "name", md.Name, "device", md.DevicePath)
	return nil
}

// GetDisk returns the ManagedDisk for the given name, or nil if not found.
func (m *Manager) GetDisk(name string) *ManagedDisk {
	return m.disks[name]
}

func (m *Manager) isEncrypted(cfg DiskConfig) bool {
	return cfg.EncryptionKey != "" || cfg.EncryptionKeyPath != ""
}

func (m *Manager) getEncryptionKey(ctx context.Context, md *ManagedDisk) (string, error) {
	if md.Config.EncryptionKeyPath != "" {
		data, err := os.ReadFile(md.Config.EncryptionKeyPath)
		if err != nil {
			return "", fmt.Errorf("read key file %s: %w", md.Config.EncryptionKeyPath, err)
		}
		return strings.TrimRight(string(data), "\n"), nil
	}

	if md.Config.EncryptionKey == "" {
		return "", fmt.Errorf("no encryption key source configured")
	}

	return m.getKeyProvider(md.Config.EncryptionKey).Get(ctx)
}
