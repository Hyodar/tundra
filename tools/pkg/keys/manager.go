package keys

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"strconv"

	"github.com/Hyodar/tundra/tools/pkg/tpm"
)

// Provider is the interface for key generation strategies.
type Provider interface {
	// Get returns the key, generating or retrieving it as needed.
	Get(ctx context.Context) (string, error)
	// Store persists the key (e.g. into TPM).
	Store(key string) error
}

// Manager maps key names to their providers and orchestrates generation.
type Manager struct {
	providers map[string]Provider
}

// NewManager creates a Manager from a Config, wiring up providers.
func NewManager(cfg *Config, t tpm.TPM) *Manager {
	providers := make(map[string]Provider, len(cfg.Keys))
	for name, kc := range resolveKeyConfigs(cfg) {
		providers[name] = CreateProvider(kc, t)
	}
	return &Manager{providers: providers}
}

// CreateProvider dispatches to the appropriate Provider implementation based on
// the key configuration strategy.
func CreateProvider(cfg KeyConfig, t tpm.TPM) Provider {
	t = configuredTPM(t, cfg.TPMIndex)
	switch cfg.Strategy {
	case "pipe":
		return NewPipeProvider(cfg, t)
	default:
		return NewRandomProvider(cfg, t)
	}
}

func resolveKeyConfigs(cfg *Config) map[string]KeyConfig {
	resolved := make(map[string]KeyConfig, len(cfg.Keys))
	usedIndices := make(map[string]struct{})
	names := make([]string, 0, len(cfg.Keys))
	for name := range cfg.Keys {
		names = append(names, name)
	}
	sort.Strings(names)

	for _, name := range names {
		kc := cfg.Keys[name]
		if kc.TPMIndex != "" {
			kc.TPMIndex = mustCanonicalTPMIndex(kc.TPMIndex)
			usedIndices[kc.TPMIndex] = struct{}{}
		}
		resolved[name] = kc
	}

	nextIndex := mustParseNVIndex(tpm.NVIndex)
	for _, name := range names {
		kc := resolved[name]
		if !kc.TPM || kc.TPMIndex != "" {
			continue
		}

		for {
			candidate := fmt.Sprintf("0x%x", nextIndex)
			nextIndex++
			if _, exists := usedIndices[candidate]; exists {
				continue
			}
			kc.TPMIndex = candidate
			usedIndices[candidate] = struct{}{}
			resolved[name] = kc
			break
		}
	}

	return resolved
}

func mustParseNVIndex(value string) uint64 {
	parsed, err := strconv.ParseUint(value, 0, 32)
	if err != nil {
		panic(fmt.Sprintf("invalid TPM NV index constant %q: %v", value, err))
	}
	return parsed
}

func mustCanonicalTPMIndex(value string) string {
	normalized, err := canonicalTPMIndex(value)
	if err != nil {
		panic(fmt.Sprintf("invalid TPM NV index %q: %v", value, err))
	}
	return normalized
}

func configuredTPM(base tpm.TPM, nvIndex string) tpm.TPM {
	if nvIndex == "" {
		return base
	}
	return base.WithIndex(nvIndex)
}

// GenerateAll generates (or retrieves) every key managed by this Manager. It
// returns a map of key-name to key-value, or the first error encountered.
func (m *Manager) GenerateAll(ctx context.Context) (map[string]string, error) {
	results := make(map[string]string, len(m.providers))

	for name, prov := range m.providers {
		slog.InfoContext(ctx, "processing key", "name", name)

		key, err := prov.Get(ctx)
		if err != nil {
			return nil, fmt.Errorf("key %q: %w", name, err)
		}

		results[name] = key
		slog.InfoContext(ctx, "key ready", "name", name, "length", len(key))
	}

	return results, nil
}
