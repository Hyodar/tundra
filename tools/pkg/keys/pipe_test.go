package keys

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestPipeProvider_DefaultPath(t *testing.T) {
	mt := &mockTPM{}
	cfg := KeyConfig{Strategy: "pipe"}
	p := NewPipeProvider(cfg, mt)

	if p.pipePath != defaultPipePath {
		t.Fatalf("expected default path %q, got %q", defaultPipePath, p.pipePath)
	}
}

func TestPipeProvider_CustomPath(t *testing.T) {
	mt := &mockTPM{}
	cfg := KeyConfig{Strategy: "pipe", PipePath: "/tmp/custom-pipe"}
	p := NewPipeProvider(cfg, mt)

	if p.pipePath != "/tmp/custom-pipe" {
		t.Fatalf("expected custom path, got %q", p.pipePath)
	}
}

func TestPipeProvider_TPMRetrieve(t *testing.T) {
	mt := &mockTPM{
		available:   true,
		retrieveVal: "tpm-pipe-key",
	}
	cfg := KeyConfig{Strategy: "pipe", TPM: true}
	p := NewPipeProvider(cfg, mt)

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key != "tpm-pipe-key" {
		t.Fatalf("expected %q, got %q", "tpm-pipe-key", key)
	}
}

func TestPipeProvider_CachesKey(t *testing.T) {
	mt := &mockTPM{}
	p := &PipeProvider{
		pipePath:  "/tmp/not-used",
		tpmDevice: mt,
		key:       "cached",
	}

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key != "cached" {
		t.Fatalf("expected %q, got %q", "cached", key)
	}
}

func TestPipeProvider_ReadFromPipe(t *testing.T) {
	dir := t.TempDir()
	pipePath := filepath.Join(dir, "test-pipe")

	mt := &mockTPM{available: false}
	cfg := KeyConfig{Strategy: "pipe", PipePath: pipePath}
	p := NewPipeProvider(cfg, mt)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	errCh := make(chan error, 1)
	keyCh := make(chan string, 1)

	go func() {
		key, err := p.Get(ctx)
		if err != nil {
			errCh <- err
			return
		}
		keyCh <- key
	}()

	// Give the goroutine a moment to create the FIFO and start reading.
	time.Sleep(200 * time.Millisecond)

	// Write to the pipe.
	if err := os.WriteFile(pipePath, []byte("pipe-secret\n"), 0600); err != nil {
		t.Fatalf("failed to write to pipe: %v", err)
	}

	select {
	case err := <-errCh:
		t.Fatalf("unexpected error: %v", err)
	case key := <-keyCh:
		if key != "pipe-secret" {
			t.Fatalf("expected %q, got %q", "pipe-secret", key)
		}
	case <-ctx.Done():
		t.Fatal("timed out waiting for pipe read")
	}
}

func TestPipeProvider_ContextCancellation(t *testing.T) {
	dir := t.TempDir()
	pipePath := filepath.Join(dir, "cancel-pipe")

	mt := &mockTPM{available: false}
	cfg := KeyConfig{Strategy: "pipe", PipePath: pipePath}
	p := NewPipeProvider(cfg, mt)

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	_, err := p.Get(ctx)
	if err == nil {
		t.Fatal("expected error from cancelled context")
	}
}

func TestPipeProvider_TPMStoreAfterPipe(t *testing.T) {
	dir := t.TempDir()
	pipePath := filepath.Join(dir, "tpm-store-pipe")

	mt := &mockTPM{
		available:   true,
		retrieveErr: os.ErrNotExist,
	}
	cfg := KeyConfig{Strategy: "pipe", PipePath: pipePath, TPM: true}
	p := NewPipeProvider(cfg, mt)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	keyCh := make(chan string, 1)
	errCh := make(chan error, 1)

	go func() {
		key, err := p.Get(ctx)
		if err != nil {
			errCh <- err
			return
		}
		keyCh <- key
	}()

	time.Sleep(200 * time.Millisecond)

	if err := os.WriteFile(pipePath, []byte("tpm-pipe-val"), 0600); err != nil {
		t.Fatalf("failed to write to pipe: %v", err)
	}

	select {
	case err := <-errCh:
		t.Fatalf("unexpected error: %v", err)
	case key := <-keyCh:
		if key != "tpm-pipe-val" {
			t.Fatalf("expected %q, got %q", "tpm-pipe-val", key)
		}
		if mt.stored != "tpm-pipe-val" {
			t.Fatalf("expected key stored in TPM, got %q", mt.stored)
		}
	case <-ctx.Done():
		t.Fatal("timed out")
	}
}

func TestPipeProvider_Store(t *testing.T) {
	mt := &mockTPM{}
	p := NewPipeProvider(KeyConfig{Strategy: "pipe"}, mt)

	if err := p.Store("manual-key"); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	key, err := p.Get(context.Background())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if key != "manual-key" {
		t.Fatalf("expected %q, got %q", "manual-key", key)
	}
}
