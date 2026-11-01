package disks

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFilesystemOps_CreateFilesystem(t *testing.T) {
	ctx := context.Background()

	t.Run("success", func(t *testing.T) {
		mr := newMockRunner()
		mr.on("", nil, "mkfs.ext4", "-F", "/dev/mapper/crypt_disk_persistent")
		fs := NewFilesystemOps(mr)

		err := fs.CreateFilesystem(ctx, "/dev/mapper/crypt_disk_persistent")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if mr.callCount() != 1 {
			t.Fatalf("expected 1 call, got %d", mr.callCount())
		}
	})

	t.Run("failure", func(t *testing.T) {
		mr := newMockRunner()
		mr.on("", errFake, "mkfs.ext4", "-F", "/dev/sdb")
		fs := NewFilesystemOps(mr)

		err := fs.CreateFilesystem(ctx, "/dev/sdb")
		if err == nil {
			t.Fatal("expected error")
		}
	})
}

func TestFilesystemOps_MountDevice(t *testing.T) {
	ctx := context.Background()

	t.Run("mounts when not mounted", func(t *testing.T) {
		// Create a temporary proc/mounts file.
		tmpDir := t.TempDir()
		mountsPath := filepath.Join(tmpDir, "mounts")
		os.WriteFile(mountsPath, []byte(""), 0644)

		old := ProcMountsPath
		ProcMountsPath = mountsPath
		t.Cleanup(func() { ProcMountsPath = old })

		mountPoint := filepath.Join(tmpDir, "persistent")

		mr := newMockRunner()
		mr.on("", nil, "mount", "/dev/mapper/crypt", mountPoint)
		fs := NewFilesystemOps(mr)

		err := fs.MountDevice(ctx, "/dev/mapper/crypt", mountPoint)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if mr.callCount() != 1 {
			t.Fatalf("expected 1 call, got %d", mr.callCount())
		}
	})

	t.Run("skips when already mounted", func(t *testing.T) {
		tmpDir := t.TempDir()
		mountsPath := filepath.Join(tmpDir, "mounts")
		mountPoint := "/mnt/persistent"
		os.WriteFile(mountsPath, []byte("/dev/mapper/crypt "+mountPoint+" ext4 rw 0 0\n"), 0644)

		old := ProcMountsPath
		ProcMountsPath = mountsPath
		t.Cleanup(func() { ProcMountsPath = old })

		mr := newMockRunner()
		fs := NewFilesystemOps(mr)

		err := fs.MountDevice(ctx, "/dev/mapper/crypt", mountPoint)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if mr.callCount() != 0 {
			t.Errorf("expected no mount calls, got %d", mr.callCount())
		}
	})
}

func TestFilesystemOps_UnmountDevice(t *testing.T) {
	ctx := context.Background()

	t.Run("unmounts when mounted", func(t *testing.T) {
		tmpDir := t.TempDir()
		mountsPath := filepath.Join(tmpDir, "mounts")
		mountPoint := "/mnt/persistent"
		os.WriteFile(mountsPath, []byte("/dev/mapper/crypt "+mountPoint+" ext4 rw 0 0\n"), 0644)

		old := ProcMountsPath
		ProcMountsPath = mountsPath
		t.Cleanup(func() { ProcMountsPath = old })

		mr := newMockRunner()
		mr.on("", nil, "umount", mountPoint)
		fs := NewFilesystemOps(mr)

		err := fs.UnmountDevice(ctx, mountPoint)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if mr.callCount() != 1 {
			t.Fatalf("expected 1 call, got %d", mr.callCount())
		}
	})

	t.Run("skips when not mounted", func(t *testing.T) {
		tmpDir := t.TempDir()
		mountsPath := filepath.Join(tmpDir, "mounts")
		os.WriteFile(mountsPath, []byte(""), 0644)

		old := ProcMountsPath
		ProcMountsPath = mountsPath
		t.Cleanup(func() { ProcMountsPath = old })

		mr := newMockRunner()
		fs := NewFilesystemOps(mr)

		err := fs.UnmountDevice(ctx, "/mnt/persistent")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if mr.callCount() != 0 {
			t.Errorf("expected no umount calls, got %d", mr.callCount())
		}
	})
}

func TestIsMounted(t *testing.T) {
	tmpDir := t.TempDir()
	mountsPath := filepath.Join(tmpDir, "mounts")

	content := strings.Join([]string{
		"sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0",
		"/dev/sda1 / ext4 rw,relatime 0 0",
		"/dev/mapper/crypt /persistent ext4 rw,relatime 0 0",
	}, "\n")
	os.WriteFile(mountsPath, []byte(content), 0644)

	old := ProcMountsPath
	ProcMountsPath = mountsPath
	t.Cleanup(func() { ProcMountsPath = old })

	tests := []struct {
		mountPoint string
		want       bool
	}{
		{"/persistent", true},
		{"/", true},
		{"/nonexistent", false},
	}

	for _, tt := range tests {
		t.Run(tt.mountPoint, func(t *testing.T) {
			got, err := IsMounted(tt.mountPoint)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tt.want {
				t.Errorf("IsMounted(%q) = %v, want %v", tt.mountPoint, got, tt.want)
			}
		})
	}
}

func TestCreateMountDirs(t *testing.T) {
	tmpDir := t.TempDir()

	dirs := []string{"ssh", "data", "logs"}
	err := CreateMountDirs(tmpDir, dirs)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	for _, d := range dirs {
		path := filepath.Join(tmpDir, d)
		info, err := os.Stat(path)
		if err != nil {
			t.Errorf("dir %q not created: %v", d, err)
			continue
		}
		if !info.IsDir() {
			t.Errorf("%q is not a directory", d)
		}
		if info.Mode().Perm() != 0700 {
			t.Errorf("dir %q has mode %o, want 0700", d, info.Mode().Perm())
		}
	}
}
