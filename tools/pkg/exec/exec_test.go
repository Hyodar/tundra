package exec

import (
	"context"
	"strings"
	"testing"
	"time"
)

func TestDefaultRunner_Run_Success(t *testing.T) {
	r := &DefaultRunner{}
	out, err := r.Run(context.Background(), "echo", "hello")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != "hello" {
		t.Fatalf("expected %q, got %q", "hello", out)
	}
}

func TestDefaultRunner_Run_CommandNotFound(t *testing.T) {
	r := &DefaultRunner{}
	_, err := r.Run(context.Background(), "this-command-does-not-exist-xyz")
	if err == nil {
		t.Fatal("expected error for missing command")
	}
}

func TestDefaultRunner_Run_NonZeroExit(t *testing.T) {
	r := &DefaultRunner{}
	_, err := r.Run(context.Background(), "false")
	if err == nil {
		t.Fatal("expected error for non-zero exit")
	}
}

func TestDefaultRunner_Run_ContextCancelled(t *testing.T) {
	r := &DefaultRunner{}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()

	_, err := r.Run(ctx, "sleep", "10")
	if err == nil {
		t.Fatal("expected error for cancelled context")
	}
}

func TestDefaultRunner_Run_CombinedOutput(t *testing.T) {
	r := &DefaultRunner{}
	out, err := r.Run(context.Background(), "sh", "-c", "echo out; echo err >&2")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !strings.Contains(out, "out") || !strings.Contains(out, "err") {
		t.Fatalf("expected combined output, got %q", out)
	}
}

func TestDefaultRunner_RunWithStdin(t *testing.T) {
	r := &DefaultRunner{}
	out, err := r.RunWithStdin(context.Background(), "hello stdin", "cat")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != "hello stdin" {
		t.Fatalf("expected %q, got %q", "hello stdin", out)
	}
}

func TestDefaultRunner_Run_NoArgs(t *testing.T) {
	r := &DefaultRunner{}
	_, err := r.Run(context.Background())
	if err == nil {
		t.Fatal("expected error for no args")
	}
}
