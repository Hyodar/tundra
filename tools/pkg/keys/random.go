package keys

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"io"
	"log/slog"
	"os"

	"github.com/Hyodar/tundra/tools/pkg/tpm"
)

const defaultSize = 64

// RandomProvider generates a cryptographically random key.
type RandomProvider struct {
	size int
	tpmEnabled bool
	tpmDevice  tpm.TPM
	key  string // cached after first generation
}

// NewRandomProvider creates a RandomProvider from the given key configuration.
func NewRandomProvider(cfg KeyConfig, t tpm.TPM) *RandomProvider {
	size := cfg.Size
	if size <= 0 {
		size = defaultSize
	}
	return &RandomProvider{
		size:       size,
		tpmEnabled: cfg.TPM,
		tpmDevice:  t,
	}
}

// Get returns the random key. On the first call it attempts, in order:
//  1. Retrieve from TPM (if TPM is enabled and available)
//  2. Return cached value
//  3. Generate new random bytes (try /dev/hwrng, fall back to crypto/rand)
//     then base64-encode, optionally store in TPM, and cache.
func (p *RandomProvider) Get(ctx context.Context) (string, error) {
	// 1. Try TPM retrieval.
	if p.tpmEnabled && p.tpmDevice.Available() {
		slog.InfoContext(ctx, "attempting TPM retrieval for random key")
		val, err := p.tpmDevice.Retrieve(ctx)
		if err == nil && val != "" {
			slog.InfoContext(ctx, "retrieved key from TPM")
			p.key = val
			return val, nil
		}
		slog.InfoContext(ctx, "TPM retrieval failed or empty, will generate", "error", err)
	}

	// 2. Return cached.
	if p.key != "" {
		return p.key, nil
	}

	// 3. Generate.
	raw, err := generateRandom(p.size)
	if err != nil {
		return "", fmt.Errorf("generate random key: %w", err)
	}

	encoded := base64.StdEncoding.EncodeToString(raw)
	p.key = encoded

	// Store in TPM if configured.
	if p.tpmEnabled && p.tpmDevice.Available() {
		if err := p.tpmDevice.Store(ctx, encoded); err != nil {
			return "", fmt.Errorf("store key in TPM: %w", err)
		}
		slog.InfoContext(ctx, "stored key in TPM")
	}

	return encoded, nil
}

// Store is a no-op for RandomProvider; storage happens during Get.
func (p *RandomProvider) Store(key string) error {
	p.key = key
	return nil
}

// generateRandom produces size random bytes. It first tries /dev/hwrng for
// hardware entropy and falls back to crypto/rand.
func generateRandom(size int) ([]byte, error) {
	buf := make([]byte, size)

	// Try hardware RNG first.
	if f, err := os.Open("/dev/hwrng"); err == nil {
		defer f.Close()
		if _, err := io.ReadFull(f, buf); err == nil {
			slog.Info("generated random bytes from /dev/hwrng", "size", size)
			return buf, nil
		}
		slog.Info("/dev/hwrng read failed, falling back to crypto/rand")
	}

	// Fallback to crypto/rand.
	if _, err := io.ReadFull(rand.Reader, buf); err != nil {
		return nil, fmt.Errorf("crypto/rand: %w", err)
	}
	slog.Info("generated random bytes from crypto/rand", "size", size)

	return buf, nil
}
