package disks

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// errFake is a reusable sentinel error for tests.
var errFake = fmt.Errorf("fake error")

// staticKeyProvider always returns the same key.
type staticKeyProvider struct {
	key string
	err error
}

func (p *staticKeyProvider) Get(_ context.Context) (string, error) {
	return p.key, p.err
}

// newTestManager creates a Manager wired up with a mock runner and test
// helpers. The findDisk hook is overridden so no real device lookup occurs.
func newTestManager(t *testing.T, cfg *Config, kp KeyProvider, mr *mockRunner, devicePath string) *Manager {
	t.Helper()

	mgr := NewManager(cfg, kp, mr)
	mgr.findDisk = func(_ DiskConfig) (string, error) {
		return devicePath, nil
	}

	return mgr
}

// setupMountsFile creates a temporary /proc/mounts replacement for the test.
func setupMountsFile(t *testing.T, content string) {
	t.Helper()
	tmpDir := t.TempDir()
	mountsPath := filepath.Join(tmpDir, "mounts")
	if err := os.WriteFile(mountsPath, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
	old := ProcMountsPath
	ProcMountsPath = mountsPath
	t.Cleanup(func() { ProcMountsPath = old })
}

func TestManager_SetupDisk_EncryptedFormat(t *testing.T) {
	// Scenario: encrypted disk, format policy "on_initialize", device is
	// not LUKS yet. Should go through the full format path.
	ctx := context.Background()
	tmpDir := t.TempDir()
	mountPoint := filepath.Join(tmpDir, "persistent")

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "on_initialize",
				EncryptionKey: "key_persistent",
				MountAt:       mountPoint,
				Dirs:          []string{"ssh", "data"},
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "test-key-123"}

	// cryptsetup isLuks -> not LUKS
	mr.on("", fmt.Errorf("not LUKS"), "cryptsetup", "isLuks", "/dev/sdb")
	// cryptsetup luksFormat
	mr.on("", nil, "cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "/dev/sdb")
	// cryptsetup token import (init token)
	mr.on("", nil, "cryptsetup", "token", "import", "--token-id", "1", "/dev/sdb")
	// cryptsetup luksOpen
	mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_persistent")
	// mkfs.ext4
	mr.on("", nil, "mkfs.ext4", "-F", "/dev/mapper/crypt_disk_persistent")
	// mount
	mr.on("", nil, "mount", "/dev/mapper/crypt_disk_persistent", mountPoint)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdb")
	err := mgr.SetupDisk(ctx, "persistent")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	md := mgr.GetDisk("persistent")
	if !md.Initialized {
		t.Error("expected disk to be marked as initialized")
	}
	if md.DevicePath != "/dev/sdb" {
		t.Errorf("device path = %q, want /dev/sdb", md.DevicePath)
	}

	// Verify directories were created.
	for _, d := range []string{"ssh", "data"} {
		path := filepath.Join(mountPoint, d)
		if _, err := os.Stat(path); err != nil {
			t.Errorf("expected dir %q to exist: %v", d, err)
		}
	}
}

// TestFormatWritesTheInitTokenAfterMkfs: a volume is marked initialized only once it has
// a filesystem, and a format that fails after the open closes the mapping.
func TestFormatWritesTheInitTokenAfterMkfs(t *testing.T) {
	ctx := context.Background()
	initToken := "cryptsetup token import --token-id 1 /dev/sdb"
	cases := []struct {
		name   string
		fails  string // the command that fails, "" for none
		tokens bool   // the init token is written
	}{
		{"a format that completes", "", true},
		{"mkfs fails", "mkfs.ext4", false},
		{"the mount fails", "mount", true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			mountPoint := filepath.Join(t.TempDir(), "persistent")
			cfg := &Config{Disks: map[string]DiskConfig{"persistent": {
				Strategy: "largest", Format: "on_initialize", EncryptionKey: "k", MountAt: mountPoint,
			}}}
			mr := newMockRunner()
			failing := func(name string) error {
				if name == c.fails {
					return fmt.Errorf("%s failed", name)
				}
				return nil
			}
			mr.on("", fmt.Errorf("not LUKS"), "cryptsetup", "isLuks", "/dev/sdb")
			mr.on("", nil, "cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "/dev/sdb")
			mr.on("", nil, "cryptsetup", "token", "import", "--token-id", "1", "/dev/sdb")
			mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_persistent")
			mr.on("", failing("mkfs.ext4"), "mkfs.ext4", "-F", "/dev/mapper/crypt_disk_persistent")
			mr.on("", failing("mount"), "mount", "/dev/mapper/crypt_disk_persistent", mountPoint)
			mr.on("", nil, "cryptsetup", "luksClose", "crypt_disk_persistent")
			setupMountsFile(t, "")
			err := newTestManager(t, cfg, &staticKeyProvider{key: "k"}, mr, "/dev/sdb").SetupDisk(ctx, "persistent")
			if (err != nil) != (c.fails != "") {
				t.Fatalf("SetupDisk = %v, want a failure: %v", err, c.fails != "")
			}
			var ran []string
			for _, call := range mr.calls {
				ran = append(ran, strings.Join(call.args, " "))
			}
			tokened, mkfs := -1, -1
			for i, cmd := range ran {
				if cmd == initToken {
					tokened = i
				}
				if strings.HasPrefix(cmd, "mkfs.ext4") {
					mkfs = i
				}
			}
			if (tokened >= 0) != c.tokens || (tokened >= 0 && tokened < mkfs) {
				t.Fatalf("the init token is not written right after a working mkfs: %v", ran)
			}
			if closed := ran[len(ran)-1] == "cryptsetup luksClose crypt_disk_persistent"; closed != (c.fails != "") {
				t.Fatalf("the mapping is closed %v after %v", closed, ran)
			}
		})
	}
}

func TestManager_SetupDisk_EncryptedMountExisting(t *testing.T) {
	// Scenario: encrypted disk, format "on_initialize", device is LUKS and
	// initialized. Should just open and mount.
	ctx := context.Background()
	tmpDir := t.TempDir()
	mountPoint := filepath.Join(tmpDir, "persistent")

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "on_initialize",
				EncryptionKey: "key_persistent",
				MountAt:       mountPoint,
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "test-key"}

	// cryptsetup isLuks -> is LUKS
	mr.on("", nil, "cryptsetup", "isLuks", "/dev/sdb")
	// Token export (initialized)
	tok := luksToken{Type: "tundra-init", Keyslots: []any{}, Initialized: "true", DiskName: "persistent"}
	tokData, _ := json.Marshal(tok)
	mr.on(string(tokData), nil, "cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
	// luksOpen
	mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_persistent")
	// mount
	mr.on("", nil, "mount", "/dev/mapper/crypt_disk_persistent", mountPoint)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdb")
	err := mgr.SetupDisk(ctx, "persistent")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestManager_SetupDisk_RefusesAnotherVolumesDisk(t *testing.T) {
	// Scenario: the device found for "persistent" is initialized as volume "logs" (a disk
	// attached at another volume's LUN): it is not opened, whatever the key.
	ctx := context.Background()
	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "on_initialize",
				EncryptionKey: "key_persistent",
				MountAt:       filepath.Join(t.TempDir(), "persistent"),
			},
		},
	}
	mr := newMockRunner()
	mr.on("", nil, "cryptsetup", "isLuks", "/dev/sdc")
	tok := luksToken{Type: "tundra-init", Keyslots: []any{}, Initialized: "true", DiskName: "logs"}
	tokData, _ := json.Marshal(tok)
	mr.on(string(tokData), nil, "cryptsetup", "token", "export", "--token-id", "1", "/dev/sdc")

	mgr := newTestManager(t, cfg, &staticKeyProvider{key: "test-key"}, mr, "/dev/sdc")
	err := mgr.SetupDisk(ctx, "persistent")
	if err == nil || !strings.Contains(err.Error(), `holds volume "logs"`) {
		t.Fatalf("err = %v, want the other volume's disk refused", err)
	}
	for _, call := range mr.calls {
		if call.args[1] == "luksOpen" {
			t.Fatal("opened another volume's disk")
		}
	}
}

func TestManager_SetupDisk_EncryptedAlwaysFormat(t *testing.T) {
	// Scenario: format "always" should format even if already initialized.
	ctx := context.Background()
	tmpDir := t.TempDir()
	mountPoint := filepath.Join(tmpDir, "persistent")

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "always",
				EncryptionKey: "key_persistent",
				MountAt:       mountPoint,
				Dirs:          []string{"data"},
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "key"}

	// isLuks -> yes
	mr.on("", nil, "cryptsetup", "isLuks", "/dev/sdb")
	// initialized -> yes
	tok := luksToken{Type: "tundra-init", Keyslots: []any{}, Initialized: "true"}
	tokData, _ := json.Marshal(tok)
	mr.on(string(tokData), nil, "cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
	// But format policy is "always", so we still format.
	mr.on("", nil, "cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "/dev/sdb")
	mr.on("", nil, "cryptsetup", "token", "import", "--token-id", "1", "/dev/sdb")
	mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_persistent")
	mr.on("", nil, "mkfs.ext4", "-F", "/dev/mapper/crypt_disk_persistent")
	mr.on("", nil, "mount", "/dev/mapper/crypt_disk_persistent", mountPoint)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdb")
	err := mgr.SetupDisk(ctx, "persistent")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestManager_SetupDisk_EncryptedNeverFormat(t *testing.T) {
	// Scenario: format "never" but device is not LUKS -> error.
	ctx := context.Background()

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "never",
				EncryptionKey: "key_persistent",
				MountAt:       "/mnt/persistent",
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "key"}

	mr.on("", fmt.Errorf("not LUKS"), "cryptsetup", "isLuks", "/dev/sdb")

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdb")
	err := mgr.SetupDisk(ctx, "persistent")
	if err == nil {
		t.Fatal("expected error when device is not LUKS and format=never")
	}
}

func TestManager_SetupDisk_PlainDisk(t *testing.T) {
	// Scenario: no encryption key, format "always".
	ctx := context.Background()
	tmpDir := t.TempDir()
	mountPoint := filepath.Join(tmpDir, "data")

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"scratch": {
				Strategy: "largest",
				Format:   "always",
				MountAt:  mountPoint,
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "unused"}

	mr.on("", nil, "mkfs.ext4", "-F", "/dev/sdc")
	mr.on("", nil, "mount", "/dev/sdc", mountPoint)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdc")
	err := mgr.SetupDisk(ctx, "scratch")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Check default dirs were created.
	for _, d := range []string{"ssh", "data", "logs"} {
		path := filepath.Join(mountPoint, d)
		if _, err := os.Stat(path); err != nil {
			t.Errorf("expected default dir %q to exist: %v", d, err)
		}
	}
}

func TestManager_SetupDisk_PlainDiskNeverFormat(t *testing.T) {
	// Scenario: no encryption key, format "never" -> just mount.
	ctx := context.Background()
	tmpDir := t.TempDir()
	mountPoint := filepath.Join(tmpDir, "data")

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"scratch": {
				Strategy: "largest",
				Format:   "never",
				MountAt:  mountPoint,
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "unused"}

	mr.on("", nil, "mount", "/dev/sdc", mountPoint)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdc")
	err := mgr.SetupDisk(ctx, "scratch")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Verify mkfs was NOT called.
	for _, c := range mr.calls {
		if len(c.args) > 0 && c.args[0] == "mkfs.ext4" {
			t.Error("mkfs.ext4 should not have been called with format=never")
		}
	}
}

func TestManager_SetupDisk_UnknownDisk(t *testing.T) {
	ctx := context.Background()

	cfg := &Config{
		Disks: map[string]DiskConfig{},
	}

	mgr := NewManager(cfg, &staticKeyProvider{key: ""}, newMockRunner())
	err := mgr.SetupDisk(ctx, "nonexistent")
	if err == nil {
		t.Fatal("expected error for unknown disk")
	}
}

func TestManager_SetupDisk_KeyProviderError(t *testing.T) {
	ctx := context.Background()

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "always",
				EncryptionKey: "key_persistent",
				MountAt:       "/mnt/persistent",
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{err: fmt.Errorf("key not available")}

	mr.on("", fmt.Errorf("not LUKS"), "cryptsetup", "isLuks", "/dev/sdb")

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdb")
	err := mgr.SetupDisk(ctx, "persistent")
	if err == nil {
		t.Fatal("expected error when key provider fails")
	}
}

func TestManager_SetupDisk_UsesPerDiskKeyFile(t *testing.T) {
	ctx := context.Background()
	tmpDir := t.TempDir()
	mountPoint := filepath.Join(tmpDir, "persistent")
	keyPath := filepath.Join(tmpDir, "key.txt")
	if err := os.WriteFile(keyPath, []byte("file-key\n"), 0o600); err != nil {
		t.Fatalf("write key file: %v", err)
	}

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:          "largest",
				Format:            "always",
				EncryptionKeyPath: keyPath,
				MountAt:           mountPoint,
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{err: fmt.Errorf("provider should not be used")}

	mr.on("", fmt.Errorf("not LUKS"), "cryptsetup", "isLuks", "/dev/sdb")
	mr.on("", nil, "cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "/dev/sdb")
	mr.on("", nil, "cryptsetup", "token", "import", "--token-id", "1", "/dev/sdb")
	mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_persistent")
	mr.on("", nil, "mkfs.ext4", "-F", "/dev/mapper/crypt_disk_persistent")
	mr.on("", nil, "mount", "/dev/mapper/crypt_disk_persistent", mountPoint)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdb")
	if err := mgr.SetupDisk(ctx, "persistent"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestManager_SetupDisk_FindDeviceError(t *testing.T) {
	ctx := context.Background()

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "on_initialize",
				EncryptionKey: "key",
				MountAt:       "/mnt/persistent",
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "key"}

	mgr := NewManager(cfg, kp, mr)
	mgr.findDisk = func(_ DiskConfig) (string, error) {
		return "", fmt.Errorf("no disks found")
	}

	err := mgr.SetupDisk(ctx, "persistent")
	if err == nil {
		t.Fatal("expected error when device not found")
	}
}

func TestManager_SetupAll(t *testing.T) {
	ctx := context.Background()
	tmpDir := t.TempDir()
	mp1 := filepath.Join(tmpDir, "disk1")
	mp2 := filepath.Join(tmpDir, "disk2")

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"d1": {
				Strategy: "largest",
				Format:   "always",
				MountAt:  mp1,
			},
			"d2": {
				Strategy: "largest",
				Format:   "always",
				MountAt:  mp2,
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "key"}

	mr.on("", nil, "mkfs.ext4", "-F", "/dev/sdc")
	mr.on("", nil, "mount", "/dev/sdc", mp1)
	mr.on("", nil, "mount", "/dev/sdc", mp2)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdc")
	err := mgr.SetupAll(ctx)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestManager_SetupDisk_OnFailFormat(t *testing.T) {
	// Scenario: format "on_fail", device is LUKS -> should NOT format.
	ctx := context.Background()
	tmpDir := t.TempDir()
	mountPoint := filepath.Join(tmpDir, "persistent")

	cfg := &Config{
		Disks: map[string]DiskConfig{
			"persistent": {
				Strategy:      "largest",
				Format:        "on_fail",
				EncryptionKey: "key_persistent",
				MountAt:       mountPoint,
			},
		},
	}

	mr := newMockRunner()
	kp := &staticKeyProvider{key: "key"}

	// isLuks -> yes
	mr.on("", nil, "cryptsetup", "isLuks", "/dev/sdb")
	// Not initialized (but is LUKS, so on_fail should not format)
	mr.on("", fmt.Errorf("no token"), "cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
	// luksOpen
	mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_persistent")
	// mount
	mr.on("", nil, "mount", "/dev/mapper/crypt_disk_persistent", mountPoint)

	setupMountsFile(t, "")

	mgr := newTestManager(t, cfg, kp, mr, "/dev/sdb")
	err := mgr.SetupDisk(ctx, "persistent")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Verify luksFormat was NOT called.
	for _, c := range mr.calls {
		if len(c.args) > 0 && c.args[0] == "cryptsetup" && len(c.args) > 1 && c.args[1] == "luksFormat" {
			t.Error("luksFormat should not have been called with on_fail when device is already LUKS")
		}
	}
}

func TestShouldFormat(t *testing.T) {
	m := &Manager{}

	tests := []struct {
		policy      string
		isLuks      bool
		initialized bool
		want        bool
	}{
		{"always", false, false, true},
		{"always", true, true, true},
		{"on_initialize", false, false, true},
		{"on_initialize", true, true, false},
		{"on_initialize", true, false, true},
		{"on_fail", false, false, true},
		{"on_fail", true, false, false},
		{"on_fail", true, true, false},
		{"never", false, false, false},
		{"never", true, true, false},
	}

	for _, tt := range tests {
		name := fmt.Sprintf("%s_luks=%v_init=%v", tt.policy, tt.isLuks, tt.initialized)
		t.Run(name, func(t *testing.T) {
			got := m.shouldFormat(tt.policy, tt.isLuks, tt.initialized)
			if got != tt.want {
				t.Errorf("shouldFormat(%q, %v, %v) = %v, want %v",
					tt.policy, tt.isLuks, tt.initialized, got, tt.want)
			}
		})
	}
}
