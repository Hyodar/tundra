package secrets

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
)

const (
	// authorizedKeysRestrictions are prepended to every key written to authorized_keys.
	authorizedKeysRestrictions = "no-port-forwarding,no-agent-forwarding,no-X11-forwarding"

	// luksTokenSlot is the LUKS token slot used for SSH key storage.
	luksTokenSlot = 2

	// File permission constants.
	dirPermissions  = 0700
	filePermissions = 0600
)

// KeyProvider is the interface for receiving SSH keys.
type KeyProvider interface {
	WaitForKey(ctx context.Context) (string, error)
}

// DiskStore is the interface for persisting SSH keys in LUKS token slots.
type DiskStore interface {
	StoreSSHToken(ctx context.Context, device string, sshKey string) error
	GetSSHToken(ctx context.Context, device string) (string, error)
}

// Manager handles the SSH key provisioning lifecycle: receiving, persisting,
// and writing SSH keys.
type Manager struct {
	config    SSHConfig
	provider  KeyProvider
	diskStore DiskStore
	logger    *slog.Logger
}

// NewManager creates a new Manager with the given configuration, provider, and
// optional disk store. If logger is nil, the default slog logger is used.
func NewManager(config SSHConfig, provider KeyProvider, diskStore DiskStore, logger *slog.Logger) *Manager {
	if logger == nil {
		logger = slog.Default()
	}
	return &Manager{
		config:    config,
		provider:  provider,
		diskStore: diskStore,
		logger:    logger,
	}
}

// Setup runs the full SSH key provisioning flow:
//  1. If store_at is configured, attempt to retrieve a stored SSH key from LUKS token slot 2.
//  2. If no stored key is found, wait for a new key from the provider (e.g. webserver).
//  3. Write the key to authorized_keys in the SSH directory with restrictions.
//  4. Write the key to key_path if configured.
//  5. If store_at is configured, persist the key to the LUKS token.
func (m *Manager) Setup(ctx context.Context) error {
	var sshKey string
	var fromStore bool

	// Step 1: Try to retrieve a stored key from LUKS if store_at is configured.
	if m.config.StoreAt != "" && m.diskStore != nil {
		m.logger.Info("attempting to retrieve stored SSH key", "device", m.config.StoreAt)
		storedKey, err := m.diskStore.GetSSHToken(ctx, m.config.StoreAt)
		if err != nil {
			m.logger.Warn("no stored SSH key found, will wait for delivery", "error", err)
		} else if storedKey != "" {
			m.logger.Info("retrieved stored SSH key from LUKS token")
			sshKey = storedKey
			fromStore = true
		}
	}

	// Step 2: If no stored key, wait for a new one from the provider.
	if sshKey == "" {
		m.logger.Info("waiting for SSH key from provider")
		key, err := m.provider.WaitForKey(ctx)
		if err != nil {
			return fmt.Errorf("waiting for SSH key: %w", err)
		}
		sshKey = key
		m.logger.Info("received SSH key from provider")
	}

	// Step 3: Write authorized_keys.
	if err := m.writeAuthorizedKeys(sshKey); err != nil {
		return fmt.Errorf("writing authorized_keys: %w", err)
	}

	// Step 4: Write key file if key_path is configured.
	if m.config.KeyPath != "" {
		if err := m.writeKeyFile(sshKey); err != nil {
			return fmt.Errorf("writing key file: %w", err)
		}
	}

	// Step 5: Persist to LUKS token if store_at is configured and the key is new.
	if m.config.StoreAt != "" && m.diskStore != nil && !fromStore {
		m.logger.Info("persisting SSH key to LUKS token", "device", m.config.StoreAt)
		if err := m.diskStore.StoreSSHToken(ctx, m.config.StoreAt, sshKey); err != nil {
			return fmt.Errorf("storing SSH key in LUKS token: %w", err)
		}
		m.logger.Info("SSH key persisted to LUKS token")
	}

	m.logger.Info("SSH key setup complete")
	return nil
}

// writeAuthorizedKeys writes the SSH key to the authorized_keys file with restrictions.
func (m *Manager) writeAuthorizedKeys(key string) error {
	sshDir := m.config.Dir
	if sshDir == "" {
		sshDir = "/root/.ssh"
	}

	if err := os.MkdirAll(sshDir, dirPermissions); err != nil {
		return fmt.Errorf("creating SSH directory %s: %w", sshDir, err)
	}

	authorizedKeysPath := filepath.Join(sshDir, "authorized_keys")
	content := fmt.Sprintf("%s ssh-ed25519 %s\n", authorizedKeysRestrictions, key)

	if err := os.WriteFile(authorizedKeysPath, []byte(content), filePermissions); err != nil {
		return fmt.Errorf("writing %s: %w", authorizedKeysPath, err)
	}

	m.logger.Info("wrote authorized_keys", "path", authorizedKeysPath)
	return nil
}

// writeKeyFile writes the raw SSH key to the configured key path.
func (m *Manager) writeKeyFile(key string) error {
	dir := filepath.Dir(m.config.KeyPath)
	if err := os.MkdirAll(dir, dirPermissions); err != nil {
		return fmt.Errorf("creating directory %s: %w", dir, err)
	}

	if err := os.WriteFile(m.config.KeyPath, []byte(key+"\n"), filePermissions); err != nil {
		return fmt.Errorf("writing key file %s: %w", m.config.KeyPath, err)
	}

	m.logger.Info("wrote key file", "path", m.config.KeyPath)
	return nil
}
