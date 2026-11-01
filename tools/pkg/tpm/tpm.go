// Package tpm provides helpers for storing and retrieving secrets in a TPM2
// non-volatile (NV) index using the tpm2-tools CLI.
package tpm

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"

	"github.com/Hyodar/tundra/tools/pkg/exec"
)

const (
	// NVIndex is the TPM2 NV index used for key storage.
	NVIndex = "0x1500016"

	// deviceTCTI is the TCTI string for the kernel resource-manager device.
	deviceTCTI = "device:/dev/tpmrm0"
)

// TPM defines the interface for TPM operations. This allows test doubles to
// replace the real implementation that shells out to tpm2-tools.
type TPM interface {
	Available() bool
	WithIndex(nvIndex string) TPM
	Store(ctx context.Context, key string) error
	Retrieve(ctx context.Context) (string, error)
	Clear(ctx context.Context) error
}

// Device is the production TPM implementation backed by tpm2-tools.
type Device struct {
	runner  exec.Runner
	nvIndex string
}

// NewDevice returns a Device that uses the given runner for executing commands.
func NewDevice(runner exec.Runner) *Device {
	return &Device{runner: runner, nvIndex: NVIndex}
}

// WithIndex returns a TPM handle that shares the runner but targets a
// different TPM NV index.
func (d *Device) WithIndex(nvIndex string) TPM {
	if nvIndex == "" {
		nvIndex = NVIndex
	}
	return &Device{runner: d.runner, nvIndex: nvIndex}
}

// Available reports whether a TPM device is present by checking for the
// existence of both /dev/tpm0 and /dev/tpmrm0.
func (d *Device) Available() bool {
	for _, path := range []string{"/dev/tpm0", "/dev/tpmrm0"} {
		if _, err := os.Stat(path); err != nil {
			slog.Debug("TPM device not found", "path", path, "error", err)
			return false
		}
	}
	return true
}

// Store writes key into the TPM2 NV index. If the index already exists it is
// first undefined so a fresh definition with the correct size can be created.
func (d *Device) Store(ctx context.Context, key string) error {
	slog.InfoContext(ctx, "storing key in TPM", "nv_index", d.nvIndex)

	// Best-effort undefine in case the index already exists.
	_, _ = d.runner.Run(ctx, "tpm2_nvundefine", "-C", "o", "-T", deviceTCTI, d.nvIndex)

	size := len(key)
	if _, err := d.runner.Run(ctx, "tpm2_nvdefine", "-C", "o", "-T", deviceTCTI,
		"-s", strconv.Itoa(size), "-a", "ownerread|ownerwrite", d.nvIndex); err != nil {
		return fmt.Errorf("tpm nvdefine: %w", err)
	}

	if _, err := d.runner.RunWithStdin(ctx, key, "tpm2_nvwrite", "-C", "o", "-T", deviceTCTI,
		"-i", "-", d.nvIndex); err != nil {
		return fmt.Errorf("tpm nvwrite: %w", err)
	}

	return nil
}

// Retrieve reads the key previously stored in the TPM2 NV index.
func (d *Device) Retrieve(ctx context.Context) (string, error) {
	slog.InfoContext(ctx, "retrieving key from TPM", "nv_index", d.nvIndex)

	out, err := d.runner.Run(ctx, "tpm2_nvread", "-C", "o", "-T", deviceTCTI, d.nvIndex)
	if err != nil {
		return "", fmt.Errorf("tpm nvread: %w", err)
	}

	return strings.TrimRight(out, "\n"), nil
}

// Clear removes the NV index from the TPM.
func (d *Device) Clear(ctx context.Context) error {
	slog.InfoContext(ctx, "clearing TPM NV index", "nv_index", d.nvIndex)

	if _, err := d.runner.Run(ctx, "tpm2_nvundefine", "-C", "o", "-T", deviceTCTI, d.nvIndex); err != nil {
		return fmt.Errorf("tpm nvundefine: %w", err)
	}

	return nil
}
