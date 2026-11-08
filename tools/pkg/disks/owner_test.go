package disks

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	tundraexec "github.com/Hyodar/tundra/tools/pkg/exec"
	"github.com/Hyodar/tundra/tools/pkg/owner"
	"golang.org/x/crypto/ssh"
)

func testKey(t *testing.T) string {
	t.Helper()
	pub, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	key, err := ssh.NewPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(ssh.MarshalAuthorizedKey(key)))
}

// luksFile is a LUKS2 header on a plain file, made by the real cryptsetup (which writes
// a file's header without root), so the owner token is read and written as on a disk.
func luksFile(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("cryptsetup"); err != nil {
		t.Skip("no cryptsetup")
	}
	path := filepath.Join(t.TempDir(), "volume.img")
	if err := os.WriteFile(path, make([]byte, 20<<20), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := (&tundraexec.DefaultRunner{}).RunWithStdin(context.Background(), "passphrase",
		"cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode",
		"--pbkdf", "pbkdf2", "--pbkdf-force-iterations", "1000", path)
	if err != nil {
		t.Fatalf("luksFormat: %v", err)
	}
	return path
}

func TestReconcile(t *testing.T) {
	ctx := context.Background()
	a, b := testKey(t), testKey(t)
	cases := []struct {
		name               string
		header, file, from string
		want, wantHeader   string
		fails              string
	}{
		{name: "nothing pinned", want: "", wantHeader: ""},
		{name: "the header's owner becomes the boot's", header: a, want: a, wantHeader: a},
		{name: "a volume without a token takes the boot's", file: a, want: a, wantHeader: a},
		{name: "the same key", header: a, file: a, want: a, wantHeader: a},
		{name: "another key is refused", header: a, file: b, wantHeader: a, fails: "owned by " + owner.Fingerprint(a)},
		{name: "a rotation from the header's key", header: a, file: b, from: a, want: b, wantHeader: b},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			device := luksFile(t)
			ops := NewLuksOps(&tundraexec.DefaultRunner{})
			if c.header != "" {
				if err := ops.WriteOwner(ctx, device, c.header); err != nil {
					t.Fatal(err)
				}
			}
			path := filepath.Join(t.TempDir(), "owner", "data")
			if c.file != "" {
				if err := owner.WriteFile(path, c.file); err != nil {
					t.Fatal(err)
				}
			}
			if c.from != "" {
				if err := owner.WriteFile(path+owner.FromSuffix, c.from); err != nil {
					t.Fatal(err)
				}
			}
			cfg := &Config{Disks: map[string]DiskConfig{"data": {
				Strategy: "pathglob", Format: "on_initialize", EncryptionKeyPath: "/run/k",
				MountAt: "/persistent/data", Owner: path,
			}}}
			mgr := NewManager(cfg, nil, &tundraexec.DefaultRunner{})
			mgr.findDisk = func(DiskConfig) (string, error) { return device, nil }
			mgr.ownerWait = 0
			err := mgr.SyncOwners(ctx)
			if c.fails != "" {
				if err == nil || !strings.Contains(err.Error(), c.fails) {
					t.Fatalf("SyncOwners = %v, want %q", err, c.fails)
				}
			} else if err != nil {
				t.Fatalf("SyncOwners: %v", err)
			}
			header, err := ops.ReadOwner(ctx, device)
			if err != nil || header != c.wantHeader {
				t.Fatalf("header owner = %q, %v; want %q", header, err, c.wantHeader)
			}
			if c.fails != "" {
				return
			}
			if got, _ := owner.ReadFile(path); got != c.want {
				t.Fatalf("boot owner = %q, want %q", got, c.want)
			}
			if got, err := owner.ReadFile(path + owner.HeaderSuffix); err != nil || got != c.wantHeader {
				t.Fatalf("%s names %q (%v), want the header's %q", owner.HeaderSuffix, got, err, c.wantHeader)
			}
			if _, err := os.Stat(path + owner.HeaderSuffix); err != nil {
				t.Fatalf("no %s for a volume with a header: %v", owner.HeaderSuffix, err)
			}
		})
	}
}

// TestFormatNeedsOwner: an owned volume is created only once a signed delivery named its
// owner, and opens only for the owner its header pins.
func TestFormatNeedsOwner(t *testing.T) {
	ctx := context.Background()
	dir := t.TempDir()
	keyFile := filepath.Join(dir, "key")
	if err := os.WriteFile(keyFile, []byte("k\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name  string
		luks  bool
		fails string
	}{
		{"create", false, "is not created"},
		{"open", true, "stays closed"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			cfg := &Config{Disks: map[string]DiskConfig{"data": {
				Strategy: "pathglob", Format: "on_initialize", EncryptionKeyPath: keyFile,
				MountAt: filepath.Join(dir, "mnt"), Owner: filepath.Join(dir, "owner", "data"),
			}}}
			mr := newMockRunner()
			if c.luks {
				mr.on("", nil, "cryptsetup", "isLuks", "/dev/sdb")
				mr.on(`{"type":"tundra-init","keyslots":[],"initialized":"true"}`, nil,
					"cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
				mr.on("", fmt.Errorf("no token"), "cryptsetup", "token", "export", "--token-id", "2", "/dev/sdb")
			} else {
				mr.on("", fmt.Errorf("not LUKS"), "cryptsetup", "isLuks", "/dev/sdb")
			}
			err := newTestManager(t, cfg, nil, mr, "/dev/sdb").SetupDisk(ctx, "data")
			if err == nil || !strings.Contains(err.Error(), c.fails) {
				t.Fatalf("SetupDisk = %v, want %q", err, c.fails)
			}
			for _, call := range mr.calls {
				if call.args[1] == "luksFormat" || call.args[1] == "luksOpen" {
					t.Fatalf("ran %v without an owner", call.args)
				}
			}
		})
	}
}

// TestVolumeCopy: after the unlock, the header's owner must be the one the encrypted volume
// keeps, which confirms it for the boot; a host that strips the token and pins its own key
// gets the volume closed again, and so does a mount that fails.
func TestVolumeCopy(t *testing.T) {
	ctx := context.Background()
	a, b := testKey(t), testKey(t)
	tokenOf := func(key string) string {
		tok, err := owner.NewToken(key)
		if err != nil {
			t.Fatal(err)
		}
		data, _ := json.Marshal(tok)
		return string(data)
	}
	cases := []struct {
		name               string
		header, boot, copy string
		want               string
		fails              string
	}{
		{name: "the copy matches", header: a, boot: a, copy: a, want: a},
		{name: "the header stripped and re-pinned by another key", boot: b, copy: a, want: a, fails: owner.MismatchMessage("data")},
		{name: "the first unlock without a copy writes it", header: a, boot: a, want: a},
		{name: "a mount that fails", header: a, boot: a, copy: a, want: a, fails: "mount failed"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			dir := t.TempDir()
			mount := filepath.Join(dir, "mnt")
			keyFile := filepath.Join(dir, "key")
			ownerFile := filepath.Join(dir, "owner", "data")
			if err := os.WriteFile(keyFile, []byte("k\n"), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := owner.WriteFile(ownerFile, c.boot); err != nil {
				t.Fatal(err)
			}
			copyPath := filepath.Join(mount, owner.VolumeCopy)
			if c.copy != "" {
				if err := owner.WriteCopy(copyPath, c.copy); err != nil {
					t.Fatal(err)
				}
			}
			setupMountsFile(t, "")
			mr := newMockRunner()
			mr.on("", nil, "cryptsetup", "isLuks", "/dev/sdb")
			mr.on(`{"type":"tundra-init","keyslots":[],"initialized":"true"}`, nil,
				"cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
			if c.header != "" {
				mr.on(tokenOf(c.header), nil, "cryptsetup", "token", "export", "--token-id", "2", "/dev/sdb")
			} else {
				mr.on("", fmt.Errorf("no token"), "cryptsetup", "token", "export", "--token-id", "2", "/dev/sdb")
			}
			mr.on("", nil, "cryptsetup", "token", "import", "--token-id", "2", "--token-replace", "/dev/sdb")
			mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_data")
			if c.fails == "mount failed" {
				mr.on("", fmt.Errorf("mount failed"), "mount", "/dev/mapper/crypt_disk_data", mount)
			} else {
				mr.on("", nil, "mount", "/dev/mapper/crypt_disk_data", mount)
			}
			mr.on("", nil, "cryptsetup", "luksClose", "crypt_disk_data")
			cfg := &Config{Disks: map[string]DiskConfig{"data": {
				Strategy: "pathglob", Format: "on_initialize", EncryptionKeyPath: keyFile,
				MountAt: mount, Owner: ownerFile,
			}}}
			err := newTestManager(t, cfg, nil, mr, "/dev/sdb").SetupDisk(ctx, "data")
			mismatch, _ := os.ReadFile(ownerFile + owner.MismatchSuffix)
			confirmed, _ := owner.ReadFile(ownerFile + owner.ConfirmedSuffix)
			if c.fails != "" {
				if err == nil || !strings.Contains(err.Error(), c.fails) {
					t.Fatalf("SetupDisk = %v, want %q", err, c.fails)
				}
				if c.fails == owner.MismatchMessage("data") && strings.TrimSpace(string(mismatch)) != c.fails {
					t.Fatalf("the mismatch file says %q", mismatch)
				}
				if mr.lastCall().args[1] != "luksClose" || confirmed != "" {
					t.Fatalf("the volume was not closed (last ran %v) or confirmed %q", mr.lastCall().args, confirmed)
				}
			} else if err != nil || len(mismatch) != 0 || confirmed != c.want {
				t.Fatalf("SetupDisk = %v, mismatch %q, confirmed %q", err, mismatch, confirmed)
			}
			if got, _ := owner.ReadFile(copyPath); got != c.want {
				t.Fatalf("the volume's copy names %q, want %q", got, c.want)
			}
		})
	}
}

// TestWatchOwners: a pin the endpoint writes after boot, then a rotation, reach the header.
func TestWatchOwners(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	a, b := testKey(t), testKey(t)
	device := luksFile(t)
	path := filepath.Join(t.TempDir(), "owner", "data")
	cfg := &Config{Disks: map[string]DiskConfig{"data": {
		Strategy: "pathglob", Format: "on_initialize", EncryptionKeyPath: "/run/k",
		MountAt: "/persistent/data", Owner: path,
	}}}
	mgr := NewManager(cfg, nil, &tundraexec.DefaultRunner{})
	mgr.findDisk = func(DiskConfig) (string, error) { return device, nil }
	mgr.ownerWait = 0
	ready := make(chan struct{})
	done := make(chan error, 1)
	go func() { done <- mgr.WatchOwners(ctx, 10*time.Millisecond, func() { close(ready) }) }()
	<-ready
	ops := NewLuksOps(&tundraexec.DefaultRunner{})
	steps := []struct {
		name  string
		write func() error
		want  string
	}{
		{"a pin", func() error { return owner.WriteFile(path, a) }, a},
		{"a rotation", func() error {
			if err := owner.WriteFile(path+owner.FromSuffix, a); err != nil {
				return err
			}
			return owner.WriteFile(path, b)
		}, b},
	}
	for _, step := range steps {
		if err := step.write(); err != nil {
			t.Fatal(err)
		}
		deadline := time.Now().Add(10 * time.Second)
		for {
			got, _ := ops.ReadOwner(ctx, device)
			if got == step.want {
				break
			}
			if time.Now().After(deadline) {
				t.Fatalf("%s: the header names %q", step.name, got)
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}
