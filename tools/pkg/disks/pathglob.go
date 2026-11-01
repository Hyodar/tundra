package disks

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// BootDevicePath is the device that should never be returned by a glob
// finder. Tests can override it.
var BootDevicePath = "/dev/sda"

// PathGlobFinder finds disk devices matching a filepath glob pattern.
type PathGlobFinder struct {
	Pattern string
}

var _ DiskFinder = (*PathGlobFinder)(nil)

// Find returns the first matching block device that is not the boot device.
func (f *PathGlobFinder) Find() (string, error) {
	matches, err := filepath.Glob(f.Pattern)
	if err != nil {
		return "", fmt.Errorf("pathglob finder: %w", err)
	}

	for _, m := range matches {
		// Must be under /dev/.
		if !strings.HasPrefix(m, "/dev/") {
			continue
		}

		// Exclude the boot device.
		if m == BootDevicePath {
			continue
		}

		// Must be a block device (not a character device).
		info, err := os.Stat(m)
		if err != nil {
			continue
		}

		// Regular files and directories are not block devices.
		// On Linux, block devices have ModeDevice set but NOT ModeCharDevice.
		if info.Mode()&os.ModeDevice == 0 {
			continue
		}
		if info.Mode()&os.ModeCharDevice != 0 {
			continue
		}

		return m, nil
	}

	return "", fmt.Errorf("pathglob finder: no matching block device for pattern %q", f.Pattern)
}
