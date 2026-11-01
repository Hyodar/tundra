package keys

import (
	"context"
	"encoding/base64"
	"errors"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/tpm"
)

// mockTPM implements tpm.TPM for unit tests.
type mockTPM struct {
	available   bool
	stored      string
	retrieveVal string
	retrieveErr error
	storeErr    error
	nvIndex     string
	children    map[string]*mockTPM
}

func (m *mockTPM) Available() bool { return m.available }

func (m *mockTPM) Store(_ context.Context, key string) error {
	if m.storeErr != nil {
		return m.storeErr
	}
	m.stored = key
	return nil
}

func (m *mockTPM) Retrieve(_ context.Context) (string, error) {
	return m.retrieveVal, m.retrieveErr
}

func (m *mockTPM) Clear(_ context.Context) error { return nil }

func (m *mockTPM) WithIndex(index string) tpm.TPM {
	if m.children == nil {
		m.children = make(map[string]*mockTPM)
	}
	if child, ok := m.children[index]; ok {
		return child
	}
	child := &mockTPM{
		available: m.available,
		nvIndex:   index,
		children:  m.children,
	}
	m.children[index] = child
	return child
}

func TestRandomProvider_GeneratesKey(t *testing.T) {
	mt := &mockTPM{available: false}
	cfg := KeyConfig{Strategy: "random", Size: 32}
	p := NewRandomProvider(cfg, mt)

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key == "" {
		t.Fatal("expected non-empty key")
	}

	// Should be valid base64.
	raw, err := base64.StdEncoding.DecodeString(key)
	if err != nil {
		t.Fatalf("key is not valid base64: %v", err)
	}
	if len(raw) != 32 {
		t.Fatalf("expected 32 raw bytes, got %d", len(raw))
	}
}

func TestRandomProvider_DefaultSize(t *testing.T) {
	mt := &mockTPM{available: false}
	cfg := KeyConfig{Strategy: "random"} // size 0 -> default
	p := NewRandomProvider(cfg, mt)

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	raw, err := base64.StdEncoding.DecodeString(key)
	if err != nil {
		t.Fatalf("key is not valid base64: %v", err)
	}
	if len(raw) != defaultSize {
		t.Fatalf("expected %d raw bytes (default), got %d", defaultSize, len(raw))
	}
}

func TestRandomProvider_CachesKey(t *testing.T) {
	mt := &mockTPM{available: false}
	cfg := KeyConfig{Strategy: "random", Size: 16}
	p := NewRandomProvider(cfg, mt)

	key1, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	key2, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if key1 != key2 {
		t.Fatalf("expected cached key to be returned; got %q and %q", key1, key2)
	}
}

func TestRandomProvider_TPMRetrieve(t *testing.T) {
	mt := &mockTPM{
		available:   true,
		retrieveVal: "from-tpm",
	}
	cfg := KeyConfig{Strategy: "random", TPM: true}
	p := NewRandomProvider(cfg, mt)

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key != "from-tpm" {
		t.Fatalf("expected %q, got %q", "from-tpm", key)
	}
}

func TestRandomProvider_TPMRetrieveFailsGeneratesNew(t *testing.T) {
	mt := &mockTPM{
		available:   true,
		retrieveErr: errors.New("nv not found"),
	}
	cfg := KeyConfig{Strategy: "random", TPM: true, Size: 16}
	p := NewRandomProvider(cfg, mt)

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key == "" {
		t.Fatal("expected generated key")
	}
	// Should have stored in TPM.
	if mt.stored != key {
		t.Fatalf("expected key to be stored in TPM, got stored=%q key=%q", mt.stored, key)
	}
}

func TestRandomProvider_TPMStoreError(t *testing.T) {
	mt := &mockTPM{
		available:   true,
		retrieveErr: errors.New("nv not found"),
		storeErr:    errors.New("store failed"),
	}
	cfg := KeyConfig{Strategy: "random", TPM: true, Size: 16}
	p := NewRandomProvider(cfg, mt)

	_, err := p.Get(context.Background())
	if err == nil {
		t.Fatal("expected error when TPM store fails")
	}
}

func TestRandomProvider_Store(t *testing.T) {
	mt := &mockTPM{}
	p := NewRandomProvider(KeyConfig{}, mt)

	if err := p.Store("my-key"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key != "my-key" {
		t.Fatalf("expected %q, got %q", "my-key", key)
	}
}
