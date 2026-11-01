package disks

import (
	"bufio"
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"github.com/Hyodar/tundra/tools/pkg/exec"
)

// ProcMountsPath is the path to /proc/mounts. Tests can override it.
var ProcMountsPath = "/proc/mounts"

// FilesystemOps provides filesystem operations (mkfs, mount, umount).
type FilesystemOps struct {
	Runner exec.Runner
}

// NewFilesystemOps creates a FilesystemOps that uses the given command runner.
func NewFilesystemOps(runner exec.Runner) *FilesystemOps {
	return &FilesystemOps{Runner: runner}
}

// CreateFilesystem creates an ext4 filesystem on the given device.
func (f *FilesystemOps) CreateFilesystem(ctx context.Context, device string) error {
	slog.InfoContext(ctx, "creating ext4 filesystem", "device", device)

	_, err := f.Runner.Run(ctx, "mkfs.ext4", "-F", device)
	if err != nil {
		return fmt.Errorf("create filesystem: %w", err)
	}

	return nil
}

// MountDevice mounts device at mountPoint. It creates the mount point
// directory if it does not exist. If the device is already mounted at the
// given point this is a no-op.
func (f *FilesystemOps) MountDevice(ctx context.Context, device, mountPoint string) error {
	mounted, err := IsMounted(mountPoint)
	if err != nil {
		return fmt.Errorf("mount device: %w", err)
	}
	if mounted {
		slog.InfoContext(ctx, "device already mounted", "mountPoint", mountPoint)
		return nil
	}

	if err := os.MkdirAll(mountPoint, 0755); err != nil {
		return fmt.Errorf("mount device: create mount point: %w", err)
	}

	slog.InfoContext(ctx, "mounting device", "device", device, "mountPoint", mountPoint)

	_, err = f.Runner.Run(ctx, "mount", device, mountPoint)
	if err != nil {
		return fmt.Errorf("mount device: %w", err)
	}

	return nil
}

// UnmountDevice unmounts the given mount point.
func (f *FilesystemOps) UnmountDevice(ctx context.Context, mountPoint string) error {
	mounted, err := IsMounted(mountPoint)
	if err != nil {
		return fmt.Errorf("unmount device: %w", err)
	}
	if !mounted {
		slog.InfoContext(ctx, "device not mounted", "mountPoint", mountPoint)
		return nil
	}

	slog.InfoContext(ctx, "unmounting device", "mountPoint", mountPoint)

	_, err = f.Runner.Run(ctx, "umount", mountPoint)
	if err != nil {
		return fmt.Errorf("unmount device: %w", err)
	}

	return nil
}

// IsMounted checks whether a mount point appears in /proc/mounts.
func IsMounted(mountPoint string) (bool, error) {
	file, err := os.Open(ProcMountsPath)
	if err != nil {
		return false, fmt.Errorf("check mounted: %w", err)
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) >= 2 && fields[1] == mountPoint {
			return true, nil
		}
	}

	return false, scanner.Err()
}

// CreateMountDirs creates subdirectories inside mountPoint with 0700
// permissions. Directory names are validated to prevent path traversal.
func CreateMountDirs(mountPoint string, dirs []string) error {
	for _, d := range dirs {
		// Reject path traversal attempts.
		cleaned := filepath.Clean(d)
		if cleaned != d || strings.Contains(cleaned, "..") || filepath.IsAbs(cleaned) {
			return fmt.Errorf("create mount dir: invalid directory name %q", d)
		}
		path := filepath.Join(mountPoint, cleaned)
		if err := os.MkdirAll(path, 0700); err != nil {
			return fmt.Errorf("create mount dir %q: %w", path, err)
		}
	}
	return nil
}
