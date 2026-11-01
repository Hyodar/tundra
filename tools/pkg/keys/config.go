// Package keys implements cryptographic key generation and management for TDX
// VM initialization.
package keys

import (
	"fmt"
	"os"
	"strconv"

	"gopkg.in/yaml.v3"
)

// Config is the top-level configuration file structure.
type Config struct {
	Keys map[string]KeyConfig `yaml:"keys"`
}

// KeyConfig describes how a single key should be generated and stored.
type KeyConfig struct {
	Strategy   string `yaml:"strategy"`    // "random" or "pipe"
	TPM        bool   `yaml:"tpm"`         // whether to persist in TPM
	TPMIndex   string `yaml:"tpm_index"`   // TPM NV index override for this key
	Size       int    `yaml:"size"`        // byte count for random strategy (default 64)
	PipePath   string `yaml:"pipe_path"`   // FIFO path for pipe strategy
	OutputPath string `yaml:"output_path"` // optional file destination for the key
}

// LoadConfig reads and parses a YAML configuration file.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read config %s: %w", path, err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config %s: %w", path, err)
	}

	return &cfg, nil
}

// Validate checks that the configuration is well-formed.
func (c *Config) Validate() error {
	if len(c.Keys) == 0 {
		return fmt.Errorf("no keys defined in config")
	}

	outputPaths := make(map[string]string)
	tpmIndices := make(map[string]string)

	for name, kc := range c.Keys {
		switch kc.Strategy {
		case "random":
			if kc.Size < 0 {
				return fmt.Errorf("key %q: size must be non-negative", name)
			}
		case "pipe":
			// pipe_path can be empty; the provider will use a default.
		default:
			return fmt.Errorf("key %q: unknown strategy %q (want \"random\" or \"pipe\")", name, kc.Strategy)
		}

		if kc.OutputPath != "" {
			if other, ok := outputPaths[kc.OutputPath]; ok {
				return fmt.Errorf(
					"key %q: output_path %q already used by key %q",
					name,
					kc.OutputPath,
					other,
				)
			}
			outputPaths[kc.OutputPath] = name
		}

		if kc.TPMIndex != "" {
			if !kc.TPM {
				return fmt.Errorf("key %q: tpm_index requires tpm=true", name)
			}
			normalized, err := canonicalTPMIndex(kc.TPMIndex)
			if err != nil {
				return fmt.Errorf("key %q: %w", name, err)
			}
			if other, ok := tpmIndices[normalized]; ok {
				return fmt.Errorf(
					"key %q: tpm_index %q already used by key %q",
					name,
					normalized,
					other,
				)
			}
			tpmIndices[normalized] = name
		}
	}

	return nil
}

func canonicalTPMIndex(value string) (string, error) {
	parsed, err := strconv.ParseUint(value, 0, 32)
	if err != nil {
		return "", fmt.Errorf("tpm_index %q must be hex like 0x1500016", value)
	}
	return fmt.Sprintf("0x%x", parsed), nil
}
