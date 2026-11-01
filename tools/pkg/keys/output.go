package keys

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// WriteOutputs materializes generated keys to any configured output_path
// destinations.
func WriteOutputs(cfg *Config, results map[string]string) error {
	names := make([]string, 0, len(cfg.Keys))
	for name := range cfg.Keys {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, name := range names {
		keyCfg := cfg.Keys[name]
		if keyCfg.OutputPath == "" {
			continue
		}

		value, ok := results[name]
		if !ok {
			return fmt.Errorf("write outputs: missing generated key %q", name)
		}

		dir := filepath.Dir(keyCfg.OutputPath)
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return fmt.Errorf("write outputs: create dir for %q: %w", name, err)
		}
		if err := os.WriteFile(keyCfg.OutputPath, []byte(value+"\n"), 0o600); err != nil {
			return fmt.Errorf("write outputs: write %q: %w", name, err)
		}
	}

	return nil
}
