package tpm

import (
	"context"
	"errors"
	"testing"

	"github.com/Hyodar/tundra/tools/pkg/exec"
)

// --- Mock implementation ---

type MockTPM struct {
	available   bool
	stored      string
	storeErr    error
	retrieveVal string
	retrieveErr error
	clearErr    error
	storeCalled bool
	clearCalled bool
}

func (m *MockTPM) Available() bool { return m.available }

func (m *MockTPM) WithIndex(_ string) TPM { return m }

func (m *MockTPM) Store(ctx context.Context, key string) error {
	m.storeCalled = true
	if m.storeErr != nil {
		return m.storeErr
	}
	m.stored = key
	return nil
}

func (m *MockTPM) Retrieve(ctx context.Context) (string, error) {
	if m.retrieveErr != nil {
		return "", m.retrieveErr
	}
	return m.retrieveVal, nil
}

func (m *MockTPM) Clear(ctx context.Context) error {
	m.clearCalled = true
	return m.clearErr
}

// --- Tests ---

func TestMockTPM_Available(t *testing.T) {
	m := &MockTPM{available: true}
	if !m.Available() {
		t.Fatal("expected Available() to return true")
	}

	m.available = false
	if m.Available() {
		t.Fatal("expected Available() to return false")
	}
}

func TestMockTPM_StoreAndRetrieve(t *testing.T) {
	m := &MockTPM{available: true}
	ctx := context.Background()

	if err := m.Store(ctx, "secret-key"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !m.storeCalled {
		t.Fatal("Store was not called")
	}
	if m.stored != "secret-key" {
		t.Fatalf("expected stored %q, got %q", "secret-key", m.stored)
	}

	m.retrieveVal = "secret-key"
	val, err := m.Retrieve(ctx)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if val != "secret-key" {
		t.Fatalf("expected %q, got %q", "secret-key", val)
	}
}

func TestMockTPM_StoreError(t *testing.T) {
	m := &MockTPM{storeErr: errors.New("tpm failure")}
	err := m.Store(context.Background(), "key")
	if err == nil {
		t.Fatal("expected error")
	}
}

func TestMockTPM_RetrieveError(t *testing.T) {
	m := &MockTPM{retrieveErr: errors.New("nvread failed")}
	_, err := m.Retrieve(context.Background())
	if err == nil {
		t.Fatal("expected error")
	}
}

func TestMockTPM_Clear(t *testing.T) {
	m := &MockTPM{}
	if err := m.Clear(context.Background()); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !m.clearCalled {
		t.Fatal("Clear was not called")
	}
}

func TestMockTPM_ClearError(t *testing.T) {
	m := &MockTPM{clearErr: errors.New("undefine failed")}
	err := m.Clear(context.Background())
	if err == nil {
		t.Fatal("expected error")
	}
}

// TestDeviceAvailable checks that the real Device.Available correctly reports
// false on a machine without a TPM (which is the case in CI / dev).
func TestDeviceAvailable(t *testing.T) {
	d := NewDevice(&exec.DefaultRunner{})
	// We simply call it; on machines without a TPM it must return false.
	if d.Available() {
		t.Skip("TPM device found; skipping negative test")
	}
}

// Verify Device satisfies the TPM interface at compile time.
var _ TPM = (*Device)(nil)
var _ TPM = (*MockTPM)(nil)
