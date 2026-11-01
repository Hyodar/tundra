// Package exec provides a thin wrapper around os/exec for running system
// commands. It exposes an interface so callers can substitute a mock
// implementation in tests.
package exec

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os/exec"
	"strings"
)

// Runner executes system commands. The default implementation delegates to
// os/exec; tests can supply a mock.
type Runner interface {
	// Run executes the command described by args and returns combined
	// stdout output. Stderr is logged. If the command exits with a
	// non-zero status an error is returned.
	Run(ctx context.Context, args ...string) (string, error)

	// RunWithStdin is like Run but pipes stdinData to the process's stdin.
	RunWithStdin(ctx context.Context, stdinData string, args ...string) (string, error)
}

// DefaultRunner implements Runner using os/exec.
type DefaultRunner struct{}

var _ Runner = (*DefaultRunner)(nil)

func (r *DefaultRunner) Run(ctx context.Context, args ...string) (string, error) {
	return r.RunWithStdin(ctx, "", args...)
}

func (r *DefaultRunner) RunWithStdin(ctx context.Context, stdinData string, args ...string) (string, error) {
	if len(args) == 0 {
		return "", fmt.Errorf("exec: no command provided")
	}

	slog.DebugContext(ctx, "exec", "cmd", strings.Join(args, " "))

	cmd := exec.CommandContext(ctx, args[0], args[1:]...)

	if stdinData != "" {
		cmd.Stdin = io.NopCloser(strings.NewReader(stdinData))
	}

	out, err := cmd.CombinedOutput()
	output := strings.TrimSpace(string(out))

	if err != nil {
		return output, fmt.Errorf("exec %s: %w: %s", args[0], err, output)
	}

	return output, nil
}
