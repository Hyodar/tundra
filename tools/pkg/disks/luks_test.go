package disks

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// mockRunner records commands and returns pre-configured outputs.
type mockRunner struct {
	calls   []mockCall
	results map[string]mockResult
}

type mockCall struct {
	args      []string
	stdinData string
}

type mockResult struct {
	output string
	err    error
}

func newMockRunner() *mockRunner {
	return &mockRunner{
		results: make(map[string]mockResult),
	}
}

func (m *mockRunner) key(args []string) string {
	return strings.Join(args, " ")
}

func (m *mockRunner) on(output string, err error, args ...string) {
	m.results[m.key(args)] = mockResult{output: output, err: err}
}

func (m *mockRunner) Run(ctx context.Context, args ...string) (string, error) {
	return m.RunWithStdin(ctx, "", args...)
}

func (m *mockRunner) RunWithStdin(_ context.Context, stdinData string, args ...string) (string, error) {
	m.calls = append(m.calls, mockCall{args: args, stdinData: stdinData})
	k := m.key(args)
	if r, ok := m.results[k]; ok {
		return r.output, r.err
	}
	return "", fmt.Errorf("unexpected command: %s", k)
}

func (m *mockRunner) callCount() int {
	return len(m.calls)
}

func (m *mockRunner) lastCall() mockCall {
	if len(m.calls) == 0 {
		return mockCall{}
	}
	return m.calls[len(m.calls)-1]
}

func TestLuksOps_IsLuksDevice(t *testing.T) {
	ctx := context.Background()

	t.Run("device is LUKS", func(t *testing.T) {
		mr := newMockRunner()
		mr.on("", nil, "cryptsetup", "isLuks", "/dev/sdb")
		ops := NewLuksOps(mr)

		ok, err := ops.IsLuksDevice(ctx, "/dev/sdb")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if !ok {
			t.Error("expected true")
		}
	})

	t.Run("device is not LUKS", func(t *testing.T) {
		mr := newMockRunner()
		mr.on("", fmt.Errorf("not LUKS"), "cryptsetup", "isLuks", "/dev/sdb")
		ops := NewLuksOps(mr)

		ok, err := ops.IsLuksDevice(ctx, "/dev/sdb")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if ok {
			t.Error("expected false")
		}
	})
}

func TestLuksOps_InitToken(t *testing.T) {
	ctx := context.Background()

	t.Run("initialized", func(t *testing.T) {
		tok := luksToken{Type: "tundra-init", Keyslots: []any{}, Initialized: "true", DiskName: "test"}
		data, _ := json.Marshal(tok)

		mr := newMockRunner()
		mr.on(string(data), nil, "cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
		ops := NewLuksOps(mr)

		ok, _, err := ops.InitToken(ctx, "/dev/sdb")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if !ok {
			t.Error("expected true")
		}
	})

	t.Run("not initialized", func(t *testing.T) {
		tok := luksToken{Type: "tundra-init", Keyslots: []any{}, Initialized: "false"}
		data, _ := json.Marshal(tok)

		mr := newMockRunner()
		mr.on(string(data), nil, "cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
		ops := NewLuksOps(mr)

		ok, _, err := ops.InitToken(ctx, "/dev/sdb")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if ok {
			t.Error("expected false")
		}
	})

	t.Run("no token", func(t *testing.T) {
		mr := newMockRunner()
		mr.on("", fmt.Errorf("token not found"), "cryptsetup", "token", "export", "--token-id", "1", "/dev/sdb")
		ops := NewLuksOps(mr)

		ok, _, err := ops.InitToken(ctx, "/dev/sdb")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		if ok {
			t.Error("expected false when token is missing")
		}
	})
}

func TestLuksOps_FormatLuks(t *testing.T) {
	ctx := context.Background()

	t.Run("success", func(t *testing.T) {
		mr := newMockRunner()
		mr.on("", nil, "cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "/dev/sdb")
		ops := NewLuksOps(mr)

		err := ops.FormatLuks(ctx, "/dev/sdb", "my-secret-key")
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		if mr.callCount() != 1 {
			t.Fatalf("expected 1 call, got %d", mr.callCount())
		}
		if mr.lastCall().stdinData != "my-secret-key" {
			t.Errorf("stdin = %q, want %q", mr.lastCall().stdinData, "my-secret-key")
		}
	})

	t.Run("failure", func(t *testing.T) {
		mr := newMockRunner()
		mr.on("", fmt.Errorf("device busy"), "cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode", "/dev/sdb")
		ops := NewLuksOps(mr)

		err := ops.FormatLuks(ctx, "/dev/sdb", "key")
		if err == nil {
			t.Fatal("expected error")
		}
	})
}

func TestLuksOps_OpenLuks(t *testing.T) {
	ctx := context.Background()

	mr := newMockRunner()
	mr.on("", nil, "cryptsetup", "luksOpen", "/dev/sdb", "crypt_disk_persistent")
	ops := NewLuksOps(mr)

	err := ops.OpenLuks(ctx, "/dev/sdb", "crypt_disk_persistent", "key")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if mr.lastCall().stdinData != "key" {
		t.Errorf("stdin = %q, want %q", mr.lastCall().stdinData, "key")
	}
}

func TestLuksOps_CloseLuks(t *testing.T) {
	ctx := context.Background()

	mr := newMockRunner()
	mr.on("", nil, "cryptsetup", "luksClose", "crypt_disk_persistent")
	ops := NewLuksOps(mr)

	err := ops.CloseLuks(ctx, "crypt_disk_persistent")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestLuksOps_StoreInitToken(t *testing.T) {
	ctx := context.Background()

	mr := newMockRunner()
	mr.on("", nil, "cryptsetup", "token", "import", "--token-id", "1", "/dev/sdb")
	ops := NewLuksOps(mr)

	err := ops.StoreInitToken(ctx, "/dev/sdb", "persistent")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Verify the JSON written to stdin.
	var tok luksToken
	if err := json.Unmarshal([]byte(mr.lastCall().stdinData), &tok); err != nil {
		t.Fatalf("invalid JSON in stdin: %v", err)
	}
	if tok.Initialized != "true" {
		t.Errorf("initialized = %q, want %q", tok.Initialized, "true")
	}
	if tok.DiskName != "persistent" {
		t.Errorf("disk_name = %q, want %q", tok.DiskName, "persistent")
	}
}
