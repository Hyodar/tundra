package disks

import (
	"fmt"
	"os"
	"slices"

	"gopkg.in/yaml.v3"
)

// Config holds the top-level disk-setup configuration.
type Config struct {
	Disks map[string]DiskConfig `yaml:"disks"`
}

// DiskConfig describes one managed disk.
type DiskConfig struct {
	// Strategy is the disk-finding strategy: "largest" or "pathglob".
	Strategy string `yaml:"strategy"`

	// StrategyConfig carries strategy-specific parameters such as
	// "pattern" for pathglob.
	StrategyConfig map[string]string `yaml:"strategy_config"`

	// Format policy: "always", "on_initialize", "on_fail", "never".
	Format string `yaml:"format"`

	// EncryptionKey references a key name provided via KeyProvider.
	// When empty the disk is treated as a plain (unencrypted) disk.
	EncryptionKey string `yaml:"encryption_key"`

	// EncryptionKeyPath points at a file containing the disk key. When set,
	// it takes precedence over the default key provider and enables multi-key
	// CLI usage in a single setup run.
	EncryptionKeyPath string `yaml:"encryption_key_path"`

	// MountAt is the mount point path.
	MountAt string `yaml:"mount_at"`

	// Dirs lists subdirectories to create inside the mount point.
	// Defaults to ["ssh", "data", "logs"].
	Dirs []string `yaml:"dirs"`
}

// validStrategies lists the supported disk-finding strategies.
var validStrategies = []string{"largest", "pathglob"}

// validFormats lists the supported format policies.
var validFormats = []string{"always", "on_initialize", "on_fail", "never"}

// LoadConfig reads and parses a YAML configuration file.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("load config: %w", err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}

	return &cfg, nil
}

// Validate checks that the configuration is well-formed.
func (c *Config) Validate() error {
	if len(c.Disks) == 0 {
		return fmt.Errorf("validate config: no disks configured")
	}

	for name, d := range c.Disks {
		if !slices.Contains(validStrategies, d.Strategy) {
			return fmt.Errorf("validate config: disk %q: unsupported strategy %q", name, d.Strategy)
		}

		if d.Strategy == "pathglob" {
			if d.StrategyConfig == nil || d.StrategyConfig["pattern"] == "" {
				return fmt.Errorf("validate config: disk %q: pathglob strategy requires a pattern in strategy_config", name)
			}
		}

		if !slices.Contains(validFormats, d.Format) {
			return fmt.Errorf("validate config: disk %q: unsupported format policy %q", name, d.Format)
		}

		if d.MountAt == "" {
			return fmt.Errorf("validate config: disk %q: mount_at is required", name)
		}
	}

	return nil
}

// EffectiveDirs returns the directories to create for this disk config,
// falling back to the default list when none are specified.
func (d *DiskConfig) EffectiveDirs() []string {
	if len(d.Dirs) > 0 {
		return d.Dirs
	}
	return []string{"ssh", "data", "logs"}
}
