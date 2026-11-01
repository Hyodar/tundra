package keys

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/Hyodar/tundra/tools/pkg/tpm"
)

const defaultPipePath = "/tmp/passphrase"

// PipeProvider reads a key from a named pipe (FIFO).
type PipeProvider struct {
	pipePath   string
	tpmEnabled bool
	tpmDevice  tpm.TPM
	key        string // cached after first read
}

// NewPipeProvider creates a PipeProvider from the given key configuration.
func NewPipeProvider(cfg KeyConfig, t tpm.TPM) *PipeProvider {
	path := cfg.PipePath
	if path == "" {
		path = defaultPipePath
	}
	return &PipeProvider{
		pipePath:   path,
		tpmEnabled: cfg.TPM,
		tpmDevice:  t,
	}
}

// Get returns the key. On the first call it attempts, in order:
//  1. Retrieve from TPM (if TPM is enabled and available)
//  2. Return cached value
//  3. Create a named pipe (FIFO) at pipePath and block until data is written
//     to it, then optionally store in TPM and cache.
func (p *PipeProvider) Get(ctx context.Context) (string, error) {
	// 1. Try TPM retrieval.
	if p.tpmEnabled && p.tpmDevice.Available() {
		slog.InfoContext(ctx, "attempting TPM retrieval for pipe key", "pipe", p.pipePath)
		val, err := p.tpmDevice.Retrieve(ctx)
		if err == nil && val != "" {
			slog.InfoContext(ctx, "retrieved key from TPM")
			p.key = val
			return val, nil
		}
		slog.InfoContext(ctx, "TPM retrieval failed or empty, will read from pipe", "error", err)
	}

	// 2. Return cached.
	if p.key != "" {
		return p.key, nil
	}

	// 3. Read from named pipe.
	key, err := p.readFromPipe(ctx)
	if err != nil {
		return "", fmt.Errorf("read from pipe %s: %w", p.pipePath, err)
	}

	p.key = key

	// Store in TPM if configured.
	if p.tpmEnabled && p.tpmDevice.Available() {
		if err := p.tpmDevice.Store(ctx, key); err != nil {
			return "", fmt.Errorf("store pipe key in TPM: %w", err)
		}
		slog.InfoContext(ctx, "stored pipe key in TPM")
	}

	return key, nil
}

// Store is a no-op for PipeProvider; storage happens during Get.
func (p *PipeProvider) Store(key string) error {
	p.key = key
	return nil
}

// readFromPipe creates the named pipe if it doesn't exist, then opens and reads it.
// The open will block until a writer connects, which is the expected behaviour.
func (p *PipeProvider) readFromPipe(ctx context.Context) (string, error) {
	// Only remove if the existing file is a named pipe (FIFO), to avoid
	// accidentally deleting unrelated files.
	if info, err := os.Lstat(p.pipePath); err == nil {
		if info.Mode()&os.ModeNamedPipe != 0 {
			_ = os.Remove(p.pipePath)
		} else {
			return "", fmt.Errorf("pipe path %s exists and is not a FIFO", p.pipePath)
		}
	}

	if err := makePipeDir(p.pipePath); err != nil {
		return "", fmt.Errorf("create directory for pipe %s: %w", p.pipePath, err)
	}

	slog.InfoContext(ctx, "creating named pipe", "path", p.pipePath)
	if err := syscall.Mkfifo(p.pipePath, 0600); err != nil {
		return "", fmt.Errorf("mkfifo %s: %w", p.pipePath, err)
	}
	defer os.Remove(p.pipePath)

	slog.InfoContext(ctx, "waiting for data on pipe", "path", p.pipePath)

	// Use a channel so we can respect context cancellation while blocking on
	// the FIFO open.
	type result struct {
		data string
		err  error
	}
	ch := make(chan result, 1)

	go func() {
		data, err := os.ReadFile(p.pipePath)
		if err != nil {
			ch <- result{err: err}
			return
		}
		ch <- result{data: strings.TrimRight(string(data), "\n")}
	}()

	select {
	case <-ctx.Done():
		return "", ctx.Err()
	case r := <-ch:
		if r.err != nil {
			return "", r.err
		}
		return r.data, nil
	}
}

// makePipeDir creates the pipe's directory (mode 0700) and its missing parents (mode
// 0711, so a sibling such as /run/tundravm/credentials stays reachable). An existing
// directory is left as it is.
func makePipeDir(pipePath string) error {
	dir := filepath.Dir(pipePath)
	if err := os.MkdirAll(filepath.Dir(dir), 0o711); err != nil {
		return err
	}
	if err := os.Mkdir(dir, 0o700); err != nil && !errors.Is(err, fs.ErrExist) {
		return err
	}
	return nil
}
