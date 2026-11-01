package disks

import "fmt"

// DiskFinder locates a physical disk device path.
type DiskFinder interface {
	Find() (string, error)
}

// CreateDiskFinder returns a DiskFinder appropriate for the given config.
func CreateDiskFinder(cfg DiskConfig) (DiskFinder, error) {
	switch cfg.Strategy {
	case "largest":
		return &LargestDiskFinder{}, nil
	case "pathglob":
		pattern := cfg.StrategyConfig["pattern"]
		if pattern == "" {
			return nil, fmt.Errorf("create disk finder: pathglob strategy requires a pattern")
		}
		return &PathGlobFinder{Pattern: pattern}, nil
	default:
		return nil, fmt.Errorf("create disk finder: unsupported strategy %q", cfg.Strategy)
	}
}
