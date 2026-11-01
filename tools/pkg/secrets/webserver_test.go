package secrets

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

// validTestKey is a 68-character base64 string representing a valid ed25519 key.
const validTestKey = "AAAAC3NzaC1lZDI1NTE5AAAAIHOFGHq9VXaKlVQOGSNbiBMXXxGJdpRapNQe2yGJDLGE"

// getFreePort returns a free TCP port on localhost by briefly listening and then closing.
func getFreePort(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("failed to get free port: %v", err)
	}
	addr := l.Addr().String()
	l.Close()
	return addr
}

// waitForServer polls the given address until a TCP connection succeeds or the timeout elapses.
func waitForServer(t *testing.T, addr string, timeout time.Duration) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 50*time.Millisecond)
		if err == nil {
			conn.Close()
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("server at %s did not become ready in %v", addr, timeout)
}

func TestWebServerProvider_WaitForKey_ValidKey(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	addr := getFreePort(t)
	provider := NewWebServerProvider(addr, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	keyCh := make(chan string, 1)
	errCh := make(chan error, 1)

	go func() {
		key, err := provider.WaitForKey(ctx)
		if err != nil {
			errCh <- err
			return
		}
		keyCh <- key
	}()

	waitForServer(t, addr, 3*time.Second)

	resp, err := http.Post("http://"+addr+"/", "text/plain", strings.NewReader(validTestKey))
	if err != nil {
		t.Fatalf("POST failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected status 200, got %d: %s", resp.StatusCode, string(body))
	}

	select {
	case key := <-keyCh:
		if key != validTestKey {
			t.Errorf("expected key %q, got %q", validTestKey, key)
		}
	case err := <-errCh:
		t.Fatalf("WaitForKey returned error: %v", err)
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for key")
	}
}

func TestWebServerProvider_WaitForKey_RejectsGetMethod(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	addr := getFreePort(t)
	provider := NewWebServerProvider(addr, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	go func() {
		_, _ = provider.WaitForKey(ctx)
	}()

	waitForServer(t, addr, 3*time.Second)

	resp, err := http.Get("http://" + addr + "/")
	if err != nil {
		t.Fatalf("GET failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("expected status 405, got %d", resp.StatusCode)
	}
}

func TestWebServerProvider_WaitForKey_RejectsInvalidKey(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	addr := getFreePort(t)
	provider := NewWebServerProvider(addr, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	go func() {
		_, _ = provider.WaitForKey(ctx)
	}()

	waitForServer(t, addr, 3*time.Second)

	resp, err := http.Post("http://"+addr+"/", "text/plain", strings.NewReader("not-a-valid-key"))
	if err != nil {
		t.Fatalf("POST failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("expected status 400, got %d", resp.StatusCode)
	}
}

func TestWebServerProvider_WaitForKey_ContextCancellation(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	addr := getFreePort(t)
	provider := NewWebServerProvider(addr, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancel()

	_, err := provider.WaitForKey(ctx)
	if err == nil {
		t.Fatal("expected error from context cancellation, got nil")
	}

	if !strings.Contains(err.Error(), "context") {
		t.Errorf("expected context-related error, got: %v", err)
	}
}

func TestWebServerProvider_WaitForKey_TrimsWhitespace(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	addr := getFreePort(t)
	provider := NewWebServerProvider(addr, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	keyCh := make(chan string, 1)
	errCh := make(chan error, 1)

	go func() {
		key, err := provider.WaitForKey(ctx)
		if err != nil {
			errCh <- err
			return
		}
		keyCh <- key
	}()

	waitForServer(t, addr, 3*time.Second)

	keyWithWhitespace := "  " + validTestKey + "\n"
	resp, err := http.Post("http://"+addr+"/", "text/plain", strings.NewReader(keyWithWhitespace))
	if err != nil {
		t.Fatalf("POST failed: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected status 200, got %d: %s", resp.StatusCode, string(body))
	}

	select {
	case key := <-keyCh:
		if key != validTestKey {
			t.Errorf("expected key %q, got %q", validTestKey, key)
		}
	case err := <-errCh:
		t.Fatalf("WaitForKey returned error: %v", err)
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for key")
	}
}

func TestWebServerProvider_WaitForKey_InvalidKeyThenValidKey(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	addr := getFreePort(t)
	provider := NewWebServerProvider(addr, logger)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	keyCh := make(chan string, 1)
	errCh := make(chan error, 1)

	go func() {
		key, err := provider.WaitForKey(ctx)
		if err != nil {
			errCh <- err
			return
		}
		keyCh <- key
	}()

	waitForServer(t, addr, 3*time.Second)

	// First, send an invalid key - should be rejected.
	resp, err := http.Post("http://"+addr+"/", "text/plain", strings.NewReader("bad-key"))
	if err != nil {
		t.Fatalf("POST (invalid) failed: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("expected status 400 for invalid key, got %d", resp.StatusCode)
	}

	// Then send a valid key - should be accepted.
	resp, err = http.Post("http://"+addr+"/", "text/plain", strings.NewReader(validTestKey))
	if err != nil {
		t.Fatalf("POST (valid) failed: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		t.Fatalf("expected status 200 for valid key, got %d: %s", resp.StatusCode, string(body))
	}

	select {
	case key := <-keyCh:
		if key != validTestKey {
			t.Errorf("expected key %q, got %q", validTestKey, key)
		}
	case err := <-errCh:
		t.Fatalf("WaitForKey returned error: %v", err)
	case <-time.After(3 * time.Second):
		t.Fatal("timed out waiting for key")
	}
}

func TestEd25519KeyPattern(t *testing.T) {
	tests := []struct {
		name  string
		input string
		valid bool
	}{
		{"valid 68-char base64", validTestKey, true},
		{"too short", "AAAAC3NzaC1lZDI1NTE5AAAAI", false},
		{"too long", validTestKey + "extra", false},
		{"empty", "", false},
		{"contains invalid chars", "AAAAC3NzaC1lZDI1NTE5AAAAIHOFGHq9VXaKlVQOGSNbiBMXXxGJ!pRapNQe2yGJDLGE", false},
		{"68 chars with spaces", "AAAAC3NzaC1lZDI1NTE5AAAAIHOFGHq9VXaKlVQOGSNbiBMXXxGJdp apNQe2yGJDLGE", false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ed25519KeyPattern.MatchString(tt.input)
			if got != tt.valid {
				t.Errorf("ed25519KeyPattern.MatchString(%q) = %v, want %v", tt.input, got, tt.valid)
			}
		})
	}
}
