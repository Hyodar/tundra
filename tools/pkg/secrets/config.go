package secrets

import (
	"fmt"
	"os"

	"gopkg.in/yaml.v3"
)

// Config holds the top-level configuration for the secret-delivery service.
type Config struct {
	SSH SSHConfig `yaml:"ssh"`
}

// SSHConfig holds the SSH-specific configuration.
type SSHConfig struct {
	// Strategy is the method used to receive SSH keys (e.g. "webserver").
	Strategy string `yaml:"strategy"`

	// StrategyConfig holds strategy-specific settings (e.g. server_url for the webserver strategy).
	StrategyConfig map[string]string `yaml:"strategy_config"`

	// Dir is the SSH directory where authorized_keys will be written.
	// Defaults to "/root/.ssh".
	Dir string `yaml:"dir"`

	// KeyPath is an optional path where the raw SSH key will be written.
	KeyPath string `yaml:"key_path"`

	// StoreAt is the disk device name used for LUKS token persistence.
	// When set, SSH keys are stored in and retrieved from a LUKS token slot.
	StoreAt string `yaml:"store_at"`
}

// LoadConfig reads and parses a YAML configuration file.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("reading config file: %w", err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parsing config file: %w", err)
	}

	return &cfg, nil
}

// Validate checks that all required configuration fields are set and have valid values.
func (c *Config) Validate() error {
	if c.SSH.Strategy == "" {
		return fmt.Errorf("ssh.strategy is required")
	}

	if c.SSH.Strategy != "webserver" {
		return fmt.Errorf("ssh.strategy %q is not supported (supported: webserver)", c.SSH.Strategy)
	}

	if c.SSH.Dir == "" {
		c.SSH.Dir = "/root/.ssh"
	}

	return nil
}
