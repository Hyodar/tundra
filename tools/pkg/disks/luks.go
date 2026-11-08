package disks

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"

	"github.com/Hyodar/tundra/tools/pkg/exec"
	"github.com/Hyodar/tundra/tools/pkg/owner"
)

// LuksOps provides LUKS2 encryption operations backed by cryptsetup.
type LuksOps struct {
	Runner exec.Runner
}

// NewLuksOps creates a LuksOps that uses the given command runner.
func NewLuksOps(runner exec.Runner) *LuksOps {
	return &LuksOps{Runner: runner}
}

// IsLuksDevice checks whether the device at path is a LUKS device.
func (l *LuksOps) IsLuksDevice(ctx context.Context, path string) (bool, error) {
	_, err := l.Runner.Run(ctx, "cryptsetup", "isLuks", path)
	if err != nil {
		// cryptsetup returns non-zero when the device is not LUKS; that
		// is not an error from our perspective.
		slog.DebugContext(ctx, "device is not LUKS", "path", path, "err", err)
		return false, nil
	}
	return true, nil
}

// luksToken is the JSON structure stored in LUKS header token slots.
type luksToken struct {
	Type        string `json:"type"`
	Keyslots    []any  `json:"keyslots"`
	Initialized string `json:"initialized,omitempty"`
	DiskName    string `json:"disk_name,omitempty"`
}

// InitToken reads LUKS token slot 1: whether it says "initialized": "true", and the
// volume it names ("" when it names none).
func (l *LuksOps) InitToken(ctx context.Context, path string) (bool, string, error) {
	out, err := l.Runner.Run(ctx, "cryptsetup", "token", "export", "--token-id", "1", path)
	if err != nil {
		slog.DebugContext(ctx, "no init token found", "path", path, "err", err)
		return false, "", nil
	}

	var tok luksToken
	if err := json.Unmarshal([]byte(out), &tok); err != nil {
		return false, "", fmt.Errorf("parse init token: %w", err)
	}

	return tok.Initialized == "true", tok.DiskName, nil
}

// keyFromStdin makes cryptsetup take all of stdin as the key: without it a passphrase
// read from stdin stops at its first newline, so "a\nb" and "a" would open the same volume.
const keyFromStdin = "--key-file=-"

// FormatLuks formats the device as LUKS2 with the given passphrase, all of it the key.
func (l *LuksOps) FormatLuks(ctx context.Context, device, passphrase string) error {
	slog.InfoContext(ctx, "formatting LUKS2 device", "device", device)

	_, err := l.Runner.RunWithStdin(ctx, passphrase,
		"cryptsetup", "luksFormat",
		"--type", "luks2",
		"--batch-mode",
		keyFromStdin,
		device,
	)
	if err != nil {
		return fmt.Errorf("format luks: %w", err)
	}

	return nil
}

// OpenLuks opens a LUKS device with the given mapper name and passphrase, all of it the key.
func (l *LuksOps) OpenLuks(ctx context.Context, device, mapperName, passphrase string) error {
	slog.InfoContext(ctx, "opening LUKS device", "device", device, "mapper", mapperName)

	_, err := l.Runner.RunWithStdin(ctx, passphrase,
		"cryptsetup", "luksOpen",
		keyFromStdin,
		device,
		mapperName,
	)
	if err != nil {
		return fmt.Errorf("open luks: %w", err)
	}

	return nil
}

// CloseLuks closes a previously opened LUKS device.
func (l *LuksOps) CloseLuks(ctx context.Context, mapperName string) error {
	slog.InfoContext(ctx, "closing LUKS device", "mapper", mapperName)

	_, err := l.Runner.Run(ctx, "cryptsetup", "luksClose", mapperName)
	if err != nil {
		return fmt.Errorf("close luks: %w", err)
	}

	return nil
}

// StoreInitToken writes an initialization token to LUKS header slot 1.
func (l *LuksOps) StoreInitToken(ctx context.Context, device, name string) error {
	tok := luksToken{
		Type:        "tundra-init",
		Keyslots:    []any{},
		Initialized: "true",
		DiskName:    name,
	}

	data, err := json.Marshal(tok)
	if err != nil {
		return fmt.Errorf("store init token: marshal: %w", err)
	}

	_, err = l.Runner.RunWithStdin(ctx, string(data),
		"cryptsetup", "token", "import",
		"--token-id", "1",
		device,
	)
	if err != nil {
		return fmt.Errorf("store init token: %w", err)
	}

	return nil
}

// ReadOwner is the key the owner token (owner.TokenID) of the LUKS device pins, "" when
// the header has none.
func (l *LuksOps) ReadOwner(ctx context.Context, device string) (string, error) {
	out, err := l.Runner.Run(ctx, "cryptsetup", "token", "export", "--token-id", owner.TokenID, device)
	if err != nil {
		slog.DebugContext(ctx, "no owner token", "device", device, "err", err)
		return "", nil
	}
	return owner.ParseToken([]byte(out))
}

// WriteOwner writes (or replaces) the owner token that pins key.
func (l *LuksOps) WriteOwner(ctx context.Context, device, key string) error {
	tok, err := owner.NewToken(key)
	if err != nil {
		return fmt.Errorf("write owner token: %w", err)
	}
	data, err := json.Marshal(tok)
	if err != nil {
		return fmt.Errorf("write owner token: %w", err)
	}
	if _, err := l.Runner.RunWithStdin(ctx, string(data),
		"cryptsetup", "token", "import", "--token-id", owner.TokenID, "--token-replace", device,
	); err != nil {
		return fmt.Errorf("write owner token: %w", err)
	}
	slog.InfoContext(ctx, "owner token written", "device", device, "owner", tok.Fingerprint)
	return nil
}
