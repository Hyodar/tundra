package disks

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadConfig(t *testing.T) {
	t.Run("valid config", func(t *testing.T) {
		dir := t.TempDir()
		path := filepath.Join(dir, "config.yaml")
		content := `
disks:
  disk_persistent:
    strategy: "largest"
    format: "on_initialize"
    encryption_key: "key_persistent"
    encryption_key_path: "/run/keys/persistent.key"
    mount_at: "/persistent"
    dirs: ["ssh", "data", "logs"]
  disk_scratch:
    strategy: "pathglob"
    strategy_config:
      pattern: "/dev/sd*"
    format: "always"
    mount_at: "/scratch"
`
		os.WriteFile(path, []byte(content), 0644)

		cfg, err := LoadConfig(path)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		if len(cfg.Disks) != 2 {
			t.Fatalf("expected 2 disks, got %d", len(cfg.Disks))
		}

		d := cfg.Disks["disk_persistent"]
		if d.Strategy != "largest" {
			t.Errorf("strategy = %q, want %q", d.Strategy, "largest")
		}
		if d.Format != "on_initialize" {
			t.Errorf("format = %q, want %q", d.Format, "on_initialize")
		}
		if d.EncryptionKey != "key_persistent" {
			t.Errorf("encryption_key = %q, want %q", d.EncryptionKey, "key_persistent")
		}
		if d.EncryptionKeyPath != "/run/keys/persistent.key" {
			t.Errorf(
				"encryption_key_path = %q, want %q",
				d.EncryptionKeyPath,
				"/run/keys/persistent.key",
			)
		}
		if d.MountAt != "/persistent" {
			t.Errorf("mount_at = %q, want %q", d.MountAt, "/persistent")
		}
		if len(d.Dirs) != 3 {
			t.Errorf("dirs len = %d, want 3", len(d.Dirs))
		}

		d2 := cfg.Disks["disk_scratch"]
		if d2.Strategy != "pathglob" {
			t.Errorf("strategy = %q, want %q", d2.Strategy, "pathglob")
		}
		if d2.StrategyConfig["pattern"] != "/dev/sd*" {
			t.Errorf("pattern = %q, want %q", d2.StrategyConfig["pattern"], "/dev/sd*")
		}
	})

	t.Run("missing file", func(t *testing.T) {
		_, err := LoadConfig("/nonexistent/path.yaml")
		if err == nil {
			t.Fatal("expected error for missing file")
		}
	})

	t.Run("invalid yaml", func(t *testing.T) {
		dir := t.TempDir()
		path := filepath.Join(dir, "bad.yaml")
		os.WriteFile(path, []byte("not: [valid: yaml: {"), 0644)

		_, err := LoadConfig(path)
		if err == nil {
			t.Fatal("expected error for invalid YAML")
		}
	})
}

func TestConfig_Validate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     Config
		wantErr bool
	}{
		{
			name:    "no disks",
			cfg:     Config{Disks: map[string]DiskConfig{}},
			wantErr: true,
		},
		{
			name: "invalid strategy",
			cfg: Config{Disks: map[string]DiskConfig{
				"d": {Strategy: "random", Format: "always", MountAt: "/mnt"},
			}},
			wantErr: true,
		},
		{
			name: "pathglob missing pattern",
			cfg: Config{Disks: map[string]DiskConfig{
				"d": {Strategy: "pathglob", Format: "always", MountAt: "/mnt"},
			}},
			wantErr: true,
		},
		{
			name: "invalid format",
			cfg: Config{Disks: map[string]DiskConfig{
				"d": {Strategy: "largest", Format: "maybe", MountAt: "/mnt"},
			}},
			wantErr: true,
		},
		{
			name: "missing mount_at",
			cfg: Config{Disks: map[string]DiskConfig{
				"d": {Strategy: "largest", Format: "always"},
			}},
			wantErr: true,
		},
		{
			name: "valid largest",
			cfg: Config{Disks: map[string]DiskConfig{
				"d": {Strategy: "largest", Format: "on_initialize", MountAt: "/mnt"},
			}},
			wantErr: false,
		},
		{
			name: "valid pathglob",
			cfg: Config{Disks: map[string]DiskConfig{
				"d": {
					Strategy:       "pathglob",
					StrategyConfig: map[string]string{"pattern": "/dev/sd*"},
					Format:         "always",
					MountAt:        "/mnt",
				},
			}},
			wantErr: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.cfg.Validate()
			if tt.wantErr && err == nil {
				t.Error("expected error")
			}
			if !tt.wantErr && err != nil {
				t.Errorf("unexpected error: %v", err)
			}
		})
	}
}

func TestDiskConfig_EffectiveDirs(t *testing.T) {
	t.Run("custom dirs", func(t *testing.T) {
		d := DiskConfig{Dirs: []string{"a", "b"}}
		got := d.EffectiveDirs()
		if len(got) != 2 || got[0] != "a" || got[1] != "b" {
			t.Errorf("got %v, want [a b]", got)
		}
	})

	t.Run("default dirs", func(t *testing.T) {
		d := DiskConfig{}
		got := d.EffectiveDirs()
		if len(got) != 3 {
			t.Fatalf("got %v, want 3 elements", got)
		}
		if got[0] != "ssh" || got[1] != "data" || got[2] != "logs" {
			t.Errorf("got %v, want [ssh data logs]", got)
		}
	})
}
