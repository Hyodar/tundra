package keys

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/tpm"
)

func TestLoadConfig(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")

	content := `keys:
  key_persistent:
    strategy: "random"
    tpm: true
    size: 64
  key_pipe:
    strategy: "pipe"
    tpm: false
    pipe_path: "/tmp/passphrase"
`
	if err := os.WriteFile(cfgPath, []byte(content), 0644); err != nil {
		t.Fatalf("write config: %v", err)
	}

	cfg, err := LoadConfig(cfgPath)
	if err != nil {
		t.Fatalf("load config: %v", err)
	}

	if len(cfg.Keys) != 2 {
		t.Fatalf("expected 2 keys, got %d", len(cfg.Keys))
	}

	kp, ok := cfg.Keys["key_persistent"]
	if !ok {
		t.Fatal("missing key_persistent")
	}
	if kp.Strategy != "random" || !kp.TPM || kp.Size != 64 {
		t.Fatalf("unexpected key_persistent config: %+v", kp)
	}

	kpipe, ok := cfg.Keys["key_pipe"]
	if !ok {
		t.Fatal("missing key_pipe")
	}
	if kpipe.Strategy != "pipe" || kpipe.TPM || kpipe.PipePath != "/tmp/passphrase" {
		t.Fatalf("unexpected key_pipe config: %+v", kpipe)
	}
}

func TestLoadConfig_FileNotFound(t *testing.T) {
	_, err := LoadConfig("/nonexistent/path.yaml")
	if err == nil {
		t.Fatal("expected error for missing file")
	}
}

func TestLoadConfig_InvalidYAML(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "bad.yaml")

	if err := os.WriteFile(cfgPath, []byte("{{{{"), 0644); err != nil {
		t.Fatal(err)
	}

	_, err := LoadConfig(cfgPath)
	if err == nil {
		t.Fatal("expected error for invalid YAML")
	}
}

func TestConfig_Validate(t *testing.T) {
	tests := []struct {
		name    string
		cfg     Config
		wantErr bool
	}{
		{
			name:    "empty keys",
			cfg:     Config{Keys: map[string]KeyConfig{}},
			wantErr: true,
		},
		{
			name: "valid random",
			cfg: Config{Keys: map[string]KeyConfig{
				"k": {Strategy: "random", Size: 32},
			}},
			wantErr: false,
		},
		{
			name: "valid pipe",
			cfg: Config{Keys: map[string]KeyConfig{
				"k": {Strategy: "pipe"},
			}},
			wantErr: false,
		},
		{
			name: "unknown strategy",
			cfg: Config{Keys: map[string]KeyConfig{
				"k": {Strategy: "magic"},
			}},
			wantErr: true,
		},
		{
			name: "negative size",
			cfg: Config{Keys: map[string]KeyConfig{
				"k": {Strategy: "random", Size: -1},
			}},
			wantErr: true,
		},
		{
			name: "duplicate output path",
			cfg: Config{Keys: map[string]KeyConfig{
				"a": {Strategy: "random", OutputPath: "/tmp/key"},
				"b": {Strategy: "pipe", OutputPath: "/tmp/key"},
			}},
			wantErr: true,
		},
		{
			name: "tpm index without tpm",
			cfg: Config{Keys: map[string]KeyConfig{
				"k": {Strategy: "random", TPMIndex: "0x1500016"},
			}},
			wantErr: true,
		},
		{
			name: "duplicate tpm index",
			cfg: Config{Keys: map[string]KeyConfig{
				"a": {Strategy: "random", TPM: true, TPMIndex: "0x1500016"},
				"b": {Strategy: "pipe", TPM: true, TPMIndex: "0x1500016"},
			}},
			wantErr: true,
		},
		{
			name: "duplicate tpm index after normalization",
			cfg: Config{Keys: map[string]KeyConfig{
				"a": {Strategy: "random", TPM: true, TPMIndex: "0X1500016"},
				"b": {Strategy: "pipe", TPM: true, TPMIndex: "0x01500016"},
			}},
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.cfg.Validate()
			if (err != nil) != tt.wantErr {
				t.Fatalf("Validate() error = %v, wantErr = %v", err, tt.wantErr)
			}
		})
	}
}

func TestManager_GenerateAll(t *testing.T) {
	mt := &mockTPM{available: false}
	cfg := &Config{
		Keys: map[string]KeyConfig{
			"key1": {Strategy: "random", Size: 16},
			"key2": {Strategy: "random", Size: 32},
		},
	}

	mgr := NewManager(cfg, mt)
	results, err := mgr.GenerateAll(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(results) != 2 {
		t.Fatalf("expected 2 results, got %d", len(results))
	}

	for name, key := range results {
		if key == "" {
			t.Fatalf("key %q is empty", name)
		}
	}
}

func TestManager_AssignsDistinctDefaultTPMIndices(t *testing.T) {
	mt := &mockTPM{available: true}
	cfg := &Config{
		Keys: map[string]KeyConfig{
			"alpha": {Strategy: "random", TPM: true, Size: 16},
			"beta":  {Strategy: "random", TPM: true, Size: 16},
		},
	}

	mgr := NewManager(cfg, mt)
	if _, err := mgr.GenerateAll(context.Background()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(mt.children) != 2 {
		t.Fatalf("expected 2 TPM children, got %d", len(mt.children))
	}
	if _, ok := mt.children[tpm.NVIndex]; !ok {
		t.Fatalf("expected default TPM index %q to be used", tpm.NVIndex)
	}
	if _, ok := mt.children["0x1500017"]; !ok {
		t.Fatal("expected second TPM index 0x1500017 to be used")
	}
}

func TestWriteOutputs(t *testing.T) {
	dir := t.TempDir()
	pathA := filepath.Join(dir, "a", "key.txt")
	pathB := filepath.Join(dir, "b", "key.txt")
	cfg := &Config{
		Keys: map[string]KeyConfig{
			"a": {Strategy: "random", OutputPath: pathA},
			"b": {Strategy: "random", OutputPath: pathB},
			"c": {Strategy: "random"},
		},
	}

	err := WriteOutputs(
		cfg,
		map[string]string{
			"a": "alpha",
			"b": "beta",
			"c": "gamma",
		},
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	dataA, err := os.ReadFile(pathA)
	if err != nil {
		t.Fatalf("read output A: %v", err)
	}
	if string(dataA) != "alpha\n" {
		t.Fatalf("unexpected output A: %q", string(dataA))
	}

	dataB, err := os.ReadFile(pathB)
	if err != nil {
		t.Fatalf("read output B: %v", err)
	}
	if string(dataB) != "beta\n" {
		t.Fatalf("unexpected output B: %q", string(dataB))
	}
}

func TestCreateProvider_Random(t *testing.T) {
	mt := &mockTPM{}
	p := CreateProvider(KeyConfig{Strategy: "random"}, mt)
	if _, ok := p.(*RandomProvider); !ok {
		t.Fatalf("expected *RandomProvider, got %T", p)
	}
}

func TestCreateProvider_Pipe(t *testing.T) {
	mt := &mockTPM{}
	p := CreateProvider(KeyConfig{Strategy: "pipe"}, mt)
	if _, ok := p.(*PipeProvider); !ok {
		t.Fatalf("expected *PipeProvider, got %T", p)
	}
}

func TestCreateProvider_DefaultsToRandom(t *testing.T) {
	mt := &mockTPM{}
	p := CreateProvider(KeyConfig{Strategy: "unknown"}, mt)
	if _, ok := p.(*RandomProvider); !ok {
		t.Fatalf("expected *RandomProvider for unknown strategy, got %T", p)
	}
}
