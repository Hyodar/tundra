package disks

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/Hyodar/tundra/tools/pkg/exec"
	"github.com/Hyodar/tundra/tools/pkg/owner"
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

	// ownerWait is how long SyncOwners waits for a volume's device to appear.
	ownerWait time.Duration
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
		ownerWait:    30 * time.Second,
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
//     c. Format path: get key -> FormatLuks -> OpenLuks -> CreateFilesystem ->
//     StoreInitToken -> MountDevice -> CreateMountDirs, closing on any failure.
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

	initialized, holds := false, ""
	if isLuks {
		initialized, holds, err = m.luks.InitToken(ctx, md.DevicePath)
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

	// The init token is not authenticated: this stops a disk attached in another
	// volume's place (a swapped LUN), not the host; the key still decides.
	if holds != "" && holds != md.Name {
		return fmt.Errorf("disk %q: %s holds volume %q, so it is not opened\nfix: attach volume %q's own disk", md.Name, md.DevicePath, holds, md.Name)
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

	pinned := ""
	if md.Config.Owner != "" {
		if pinned, err = owner.ReadFile(md.Config.Owner); err != nil {
			return fmt.Errorf("disk %q: %w", md.Name, err)
		}
		if pinned == "" {
			return fmt.Errorf("disk %q: no signed delivery named an owner this boot, so the volume is not created", md.Name)
		}
	}

	if err := m.luks.FormatLuks(ctx, md.DevicePath, key); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if pinned != "" {
		if err := m.luks.WriteOwner(ctx, md.DevicePath, pinned); err != nil {
			return fmt.Errorf("disk %q: %w", md.Name, err)
		}
	}

	if err := m.luks.OpenLuks(ctx, md.DevicePath, md.MapperName, key); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if mounted, err := m.fillVolume(ctx, md, pinned); err != nil {
		m.closeVolume(ctx, md, mounted)
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	md.Initialized = true
	slog.InfoContext(ctx, "encrypted disk formatted and mounted", "name", md.Name, "device", md.DevicePath)
	return nil
}

// fillVolume makes the filesystem on md's open mapping, and only then writes the init
// token, so a kill before mkfs leaves a volume the next boot formats again; it mounts it,
// creates its directories and writes the owner's copy. It says whether it mounted.
func (m *Manager) fillVolume(ctx context.Context, md *ManagedDisk, pinned string) (bool, error) {
	if err := m.fs.CreateFilesystem(ctx, md.MapperDevice); err != nil {
		return false, err
	}
	if err := m.luks.StoreInitToken(ctx, md.DevicePath, md.Name); err != nil {
		return false, err
	}
	if err := m.fs.MountDevice(ctx, md.MapperDevice, md.Config.MountAt); err != nil {
		return false, err
	}
	if err := CreateMountDirs(md.Config.MountAt, md.Config.EffectiveDirs()); err != nil {
		return true, err
	}
	if pinned == "" {
		return true, nil
	}
	if err := owner.WriteCopy(filepath.Join(md.Config.MountAt, owner.VolumeCopy), pinned); err != nil {
		return true, err
	}
	if err := owner.WriteFile(md.Config.Owner+owner.ConfirmedSuffix, pinned); err != nil {
		return true, err
	}
	return true, writeHeaderOwner(md.Config.Owner, pinned)
}

// mountExistingEncrypted opens and mounts an already-formatted LUKS device; an owned
// volume opens only for the owner its header pins.
func (m *Manager) mountExistingEncrypted(ctx context.Context, md *ManagedDisk) error {
	pinned := ""
	if md.Config.Owner != "" {
		var err error
		if pinned, err = m.reconcile(ctx, md); err != nil {
			return fmt.Errorf("disk %q: %w", md.Name, err)
		}
		if pinned == "" {
			return fmt.Errorf("disk %q: no signed delivery named an owner this boot, so the volume stays closed", md.Name)
		}
	}

	key, err := m.getEncryptionKey(ctx, md)
	if err != nil {
		return fmt.Errorf("disk %q: get encryption key: %w", md.Name, err)
	}

	if err := m.luks.OpenLuks(ctx, md.DevicePath, md.MapperName, key); err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if err := m.fs.MountDevice(ctx, md.MapperDevice, md.Config.MountAt); err != nil {
		m.closeVolume(ctx, md, false)
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}

	if pinned != "" {
		if err := m.checkCopy(ctx, md, pinned); err != nil {
			m.closeVolume(ctx, md, true)
			return err
		}
	}

	slog.InfoContext(ctx, "encrypted disk mounted", "name", md.Name, "device", md.DevicePath)
	return nil
}

// closeVolume unmounts (when mounted) and closes md after a failure, so a volume that did
// not pass every check is never left open.
func (m *Manager) closeVolume(ctx context.Context, md *ManagedDisk, mounted bool) {
	if mounted {
		if err := m.fs.UnmountDevice(ctx, md.Config.MountAt); err != nil {
			slog.ErrorContext(ctx, "not unmounted", "disk", md.Name, "err", err)
		}
	}
	if err := m.luks.CloseLuks(ctx, md.MapperName); err != nil {
		slog.ErrorContext(ctx, "not closed", "disk", md.Name, "err", err)
	}
}

// checkCopy compares the header's owner with the copy inside the volume, just mounted and
// before anything that waits for it starts: a volume without a copy (made before copies)
// takes the header's, a copy that names the key a rotation this boot replaced takes the
// new one, and either confirms the owner for the boot (owner.ConfirmedSuffix). Any other
// difference says so on the console and leaves the mismatch file the endpoint and the
// probe report; the caller closes the volume again on any error.
func (m *Manager) checkCopy(ctx context.Context, md *ManagedDisk, header string) error {
	path := filepath.Join(md.Config.MountAt, owner.VolumeCopy)
	copied, err := owner.ReadFile(path)
	if err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}
	from, err := owner.ReadFile(md.Config.Owner + owner.FromSuffix)
	if err != nil {
		return fmt.Errorf("disk %q: %w", md.Name, err)
	}
	confirmed := md.Config.Owner + owner.ConfirmedSuffix
	switch {
	case copied == header:
		return owner.WriteFile(confirmed, header)
	case copied == "", copied == from:
		if copied == "" {
			slog.InfoContext(ctx, "the volume had no copy of its owner (made before copies): wrote the header's",
				"disk", md.Name, "owner", owner.Fingerprint(header))
		}
		if err := owner.WriteCopy(path, header); err != nil {
			return err
		}
		return owner.WriteFile(confirmed, header)
	}
	message := owner.MismatchMessage(md.Name)
	slog.ErrorContext(ctx, message, "header", owner.Fingerprint(header), "volume", owner.Fingerprint(copied))
	if console, err := os.OpenFile("/dev/console", os.O_WRONLY, 0); err == nil {
		fmt.Fprintln(console, message)
		console.Close()
	}
	if err := os.WriteFile(md.Config.Owner+owner.MismatchSuffix, []byte(message+"\n"), 0o644); err != nil {
		slog.ErrorContext(ctx, "mismatch not recorded", "disk", md.Name, "err", err)
	}
	return fmt.Errorf("%s: the header names %s, the volume %s, so it stays closed",
		message, owner.Fingerprint(header), owner.Fingerprint(copied))
}

// rotateCopy updates the copy of a volume that is open (its copy is there) to key, which
// then confirms key for the boot.
func rotateCopy(md *ManagedDisk, key string) error {
	path := filepath.Join(md.Config.MountAt, owner.VolumeCopy)
	if _, err := os.Stat(path); err != nil {
		return nil
	}
	if err := owner.WriteCopy(path, key); err != nil {
		return err
	}
	return owner.WriteFile(md.Config.Owner+owner.ConfirmedSuffix, key)
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

// reconcile brings together md's owner token and the boot's owner file: the header wins
// while the file is empty (at boot), the file wins when the header has no token (a volume
// made before tokens) or when it rotates from the header's key (the .from file names it).
// Any other difference is refused. It returns the owner, "" for none, and records it as the
// header's (owner.HeaderSuffix) once the header and an open volume's copy carry it.
func (m *Manager) reconcile(ctx context.Context, md *ManagedDisk) (string, error) {
	found, err := m.reconcileHeader(ctx, md)
	if err != nil {
		return "", err
	}
	return found, writeHeaderOwner(md.Config.Owner, found)
}

// writeHeaderOwner records key as what the header of the volume owned through path names:
// an empty file for none.
func writeHeaderOwner(path, key string) error {
	if key != "" {
		return owner.WriteFile(path+owner.HeaderSuffix, key)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path+owner.HeaderSuffix, nil, 0o644)
}

func (m *Manager) reconcileHeader(ctx context.Context, md *ManagedDisk) (string, error) {
	path := md.Config.Owner
	boot, err := owner.ReadFile(path)
	if err != nil {
		return "", err
	}
	token, err := m.luks.ReadOwner(ctx, md.DevicePath)
	if err != nil {
		return "", err
	}
	switch {
	case boot == "" && token == "":
		return "", nil
	case boot == "":
		slog.InfoContext(ctx, "owner read from the header", "disk", md.Name, "owner", owner.Fingerprint(token))
		return token, owner.WriteFile(path, token)
	case token == boot:
		return boot, nil
	case token == "":
		return boot, m.luks.WriteOwner(ctx, md.DevicePath, boot)
	}
	from, err := owner.ReadFile(path + owner.FromSuffix)
	if err != nil {
		return "", err
	}
	if from != token {
		return "", fmt.Errorf("the volume is owned by %s, not %s, the key that delivered this boot",
			owner.Fingerprint(token), owner.Fingerprint(boot))
	}
	if err := m.luks.WriteOwner(ctx, md.DevicePath, boot); err != nil {
		return "", err
	}
	return boot, rotateCopy(md, boot)
}

// SyncOwners reconciles each owned disk's header with its owner file: before the attested
// endpoint starts (the header's owner becomes the boot's) and each time the endpoint pins or
// rotates one (the file's reaches a header that exists). A disk not attached within
// ownerWait, or not LUKS yet, is left alone: disk-setup writes the token as it creates it.
func (m *Manager) SyncOwners(ctx context.Context) error {
	var failed []string
	for name, md := range m.disks {
		if md.Config.Owner == "" {
			continue
		}
		device, err := m.waitDisk(md.Config)
		if err != nil {
			slog.InfoContext(ctx, "owned volume not attached", "disk", name, "err", err)
			continue
		}
		md.DevicePath = device
		if isLuks, _ := m.luks.IsLuksDevice(ctx, device); !isLuks {
			continue
		}
		if _, err := m.reconcile(ctx, md); err != nil {
			slog.ErrorContext(ctx, "owner not reconciled", "disk", name, "err", err)
			failed = append(failed, fmt.Sprintf("%s: %v", name, err))
		}
	}
	if len(failed) > 0 {
		return fmt.Errorf("owners: %s", strings.Join(failed, "; "))
	}
	return nil
}

func (m *Manager) waitDisk(cfg DiskConfig) (string, error) {
	deadline := time.Now().Add(m.ownerWait)
	for {
		device, err := m.findDisk(cfg)
		if err == nil || !time.Now().Before(deadline) {
			return device, err
		}
		time.Sleep(time.Second)
	}
}

// WatchOwners reconciles the owners once, calls ready, then reconciles again whenever an
// owner file (or its .from) changes, checking every interval, until ctx ends. A failed
// reconciliation is logged and leaves the header as it was: disk-setup refuses to open a
// volume whose header and boot owner differ.
func (m *Manager) WatchOwners(ctx context.Context, interval time.Duration, ready func()) error {
	if err := m.SyncOwners(ctx); err != nil {
		slog.ErrorContext(ctx, "owners", "err", err)
	}
	seen := m.ownerFiles()
	ready()
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
		now := m.ownerFiles()
		if now == seen {
			continue
		}
		if err := m.SyncOwners(ctx); err != nil {
			slog.ErrorContext(ctx, "owners", "err", err)
		}
		seen = m.ownerFiles()
	}
}

// ownerFiles is what every owner file and its .from hold, in one string.
func (m *Manager) ownerFiles() string {
	var parts []string
	for name, md := range m.disks {
		if md.Config.Owner == "" {
			continue
		}
		for _, path := range []string{md.Config.Owner, md.Config.Owner + owner.FromSuffix} {
			data, _ := os.ReadFile(path)
			parts = append(parts, name+"\x00"+path+"\x00"+string(data))
		}
	}
	sort.Strings(parts)
	return strings.Join(parts, "\x01")
}
