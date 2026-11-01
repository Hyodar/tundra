package secrets

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// mockProvider implements KeyProvider for testing.
type mockProvider struct {
	key string
	err error
}

func (m *mockProvider) WaitForKey(ctx context.Context) (string, error) {
	if m.err != nil {
		return "", m.err
	}
	return m.key, nil
}

// mockDiskStore implements DiskStore for testing.
type mockDiskStore struct {
	storedKeys map[string]string
	storeErr   error
	getErr     error
}

func newMockDiskStore() *mockDiskStore {
	return &mockDiskStore{
		storedKeys: make(map[string]string),
	}
}

func (m *mockDiskStore) StoreSSHToken(ctx context.Context, device string, sshKey string) error {
	if m.storeErr != nil {
		return m.storeErr
	}
	m.storedKeys[device] = sshKey
	return nil
}

func (m *mockDiskStore) GetSSHToken(ctx context.Context, device string) (string, error) {
	if m.getErr != nil {
		return "", m.getErr
	}
	key, ok := m.storedKeys[device]
	if !ok {
		return "", errors.New("no key found for device")
	}
	return key, nil
}

func TestManager_Setup_BasicFlow(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")
	keyPath := filepath.Join(tmpDir, "root_key")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir:     sshDir,
		KeyPath: keyPath,
	}
	provider := &mockProvider{key: validTestKey}

	manager := NewManager(cfg, provider, nil, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := manager.Setup(ctx); err != nil {
		t.Fatalf("Setup() returned error: %v", err)
	}

	// Verify authorized_keys was written.
	akPath := filepath.Join(sshDir, "authorized_keys")
	akContent, err := os.ReadFile(akPath)
	if err != nil {
		t.Fatalf("failed to read authorized_keys: %v", err)
	}

	expectedAK := "no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 " + validTestKey + "\n"
	if string(akContent) != expectedAK {
		t.Errorf("authorized_keys content mismatch:\ngot:  %q\nwant: %q", string(akContent), expectedAK)
	}

	// Verify key file was written.
	keyContent, err := os.ReadFile(keyPath)
	if err != nil {
		t.Fatalf("failed to read key file: %v", err)
	}

	expectedKey := validTestKey + "\n"
	if string(keyContent) != expectedKey {
		t.Errorf("key file content mismatch:\ngot:  %q\nwant: %q", string(keyContent), expectedKey)
	}

	// Verify directory permissions.
	info, err := os.Stat(sshDir)
	if err != nil {
		t.Fatalf("failed to stat SSH dir: %v", err)
	}
	if info.Mode().Perm() != 0700 {
		t.Errorf("SSH dir permissions: got %o, want 0700", info.Mode().Perm())
	}

	// Verify file permissions.
	akInfo, err := os.Stat(akPath)
	if err != nil {
		t.Fatalf("failed to stat authorized_keys: %v", err)
	}
	if akInfo.Mode().Perm() != 0600 {
		t.Errorf("authorized_keys permissions: got %o, want 0600", akInfo.Mode().Perm())
	}

	keyInfo, err := os.Stat(keyPath)
	if err != nil {
		t.Fatalf("failed to stat key file: %v", err)
	}
	if keyInfo.Mode().Perm() != 0600 {
		t.Errorf("key file permissions: got %o, want 0600", keyInfo.Mode().Perm())
	}
}

func TestManager_Setup_WithDiskStore_NoStoredKey(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir:     sshDir,
		StoreAt: "test_disk",
	}
	provider := &mockProvider{key: validTestKey}
	store := newMockDiskStore()
	store.getErr = errors.New("no token found")

	manager := NewManager(cfg, provider, store, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := manager.Setup(ctx); err != nil {
		t.Fatalf("Setup() returned error: %v", err)
	}

	// Verify key was stored in disk store.
	storedKey, ok := store.storedKeys["test_disk"]
	if !ok {
		t.Fatal("key was not stored in disk store")
	}
	if storedKey != validTestKey {
		t.Errorf("stored key mismatch: got %q, want %q", storedKey, validTestKey)
	}

	// Verify authorized_keys was written.
	akContent, err := os.ReadFile(filepath.Join(sshDir, "authorized_keys"))
	if err != nil {
		t.Fatalf("failed to read authorized_keys: %v", err)
	}
	if !strings.Contains(string(akContent), validTestKey) {
		t.Error("authorized_keys does not contain the SSH key")
	}
}

func TestManager_Setup_WithDiskStore_HasStoredKey(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir:     sshDir,
		StoreAt: "test_disk",
	}

	// Provider should NOT be called since we have a stored key.
	provider := &mockProvider{err: errors.New("should not be called")}
	store := newMockDiskStore()
	store.storedKeys["test_disk"] = validTestKey

	manager := NewManager(cfg, provider, store, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := manager.Setup(ctx); err != nil {
		t.Fatalf("Setup() returned error: %v", err)
	}

	// Verify authorized_keys was written with stored key.
	akContent, err := os.ReadFile(filepath.Join(sshDir, "authorized_keys"))
	if err != nil {
		t.Fatalf("failed to read authorized_keys: %v", err)
	}
	if !strings.Contains(string(akContent), validTestKey) {
		t.Error("authorized_keys does not contain the stored SSH key")
	}
}

func TestManager_Setup_WithDiskStore_DoesNotReStoreExistingKey(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir:     sshDir,
		StoreAt: "test_disk",
	}

	provider := &mockProvider{err: errors.New("should not be called")}
	store := newMockDiskStore()
	store.storedKeys["test_disk"] = validTestKey
	// Set storeErr to verify that StoreSSHToken is not called.
	store.storeErr = errors.New("should not be called")

	manager := NewManager(cfg, provider, store, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Should succeed because it doesn't try to store an already-stored key.
	if err := manager.Setup(ctx); err != nil {
		t.Fatalf("Setup() returned error: %v", err)
	}
}

func TestManager_Setup_ProviderError(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir: sshDir,
	}
	provider := &mockProvider{err: errors.New("connection refused")}

	manager := NewManager(cfg, provider, nil, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := manager.Setup(ctx)
	if err == nil {
		t.Fatal("expected error from Setup(), got nil")
	}
	if !strings.Contains(err.Error(), "connection refused") {
		t.Errorf("expected error containing 'connection refused', got: %v", err)
	}
}

func TestManager_Setup_DiskStoreError(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir:     sshDir,
		StoreAt: "test_disk",
	}
	provider := &mockProvider{key: validTestKey}
	store := newMockDiskStore()
	store.getErr = errors.New("no token found")
	store.storeErr = errors.New("disk write failed")

	manager := NewManager(cfg, provider, store, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	err := manager.Setup(ctx)
	if err == nil {
		t.Fatal("expected error from Setup(), got nil")
	}
	if !strings.Contains(err.Error(), "disk write failed") {
		t.Errorf("expected error containing 'disk write failed', got: %v", err)
	}
}

func TestManager_Setup_NoKeyPath(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir: sshDir,
		// KeyPath is intentionally empty.
	}
	provider := &mockProvider{key: validTestKey}

	manager := NewManager(cfg, provider, nil, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := manager.Setup(ctx); err != nil {
		t.Fatalf("Setup() returned error: %v", err)
	}

	// Verify authorized_keys was written.
	akContent, err := os.ReadFile(filepath.Join(sshDir, "authorized_keys"))
	if err != nil {
		t.Fatalf("failed to read authorized_keys: %v", err)
	}
	if !strings.Contains(string(akContent), validTestKey) {
		t.Error("authorized_keys does not contain the SSH key")
	}

	// Verify no key file was created in the temp dir root.
	entries, err := os.ReadDir(tmpDir)
	if err != nil {
		t.Fatalf("failed to read temp dir: %v", err)
	}
	for _, entry := range entries {
		if entry.Name() != ".ssh" {
			t.Errorf("unexpected file in temp dir: %s", entry.Name())
		}
	}
}

func TestManager_Setup_AuthorizedKeysFormat(t *testing.T) {
	tmpDir := t.TempDir()
	sshDir := filepath.Join(tmpDir, ".ssh")

	logger := slog.New(slog.DiscardHandler)
	cfg := SSHConfig{
		Dir: sshDir,
	}
	provider := &mockProvider{key: validTestKey}

	manager := NewManager(cfg, provider, nil, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := manager.Setup(ctx); err != nil {
		t.Fatalf("Setup() returned error: %v", err)
	}

	akContent, err := os.ReadFile(filepath.Join(sshDir, "authorized_keys"))
	if err != nil {
		t.Fatalf("failed to read authorized_keys: %v", err)
	}

	content := string(akContent)

	// Check that restrictions are present.
	if !strings.Contains(content, "no-port-forwarding") {
		t.Error("authorized_keys missing no-port-forwarding restriction")
	}
	if !strings.Contains(content, "no-agent-forwarding") {
		t.Error("authorized_keys missing no-agent-forwarding restriction")
	}
	if !strings.Contains(content, "no-X11-forwarding") {
		t.Error("authorized_keys missing no-X11-forwarding restriction")
	}
	if !strings.Contains(content, "ssh-ed25519") {
		t.Error("authorized_keys missing ssh-ed25519 key type")
	}

	// Verify exact format.
	expected := "no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 " + validTestKey + "\n"
	if content != expected {
		t.Errorf("authorized_keys format mismatch:\ngot:  %q\nwant: %q", content, expected)
	}
}
