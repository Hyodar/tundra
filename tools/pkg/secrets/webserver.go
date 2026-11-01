package secrets

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"regexp"
	"strings"
	"time"
)

// ed25519KeyPattern matches a base64-encoded ed25519 public key (68 base64 characters).
var ed25519KeyPattern = regexp.MustCompile(`^[A-Za-z0-9+/]{68}$`)

const (
	defaultServerAddr = "0.0.0.0:8080"

	// Server timeouts for production use.
	serverReadTimeout     = 10 * time.Second
	serverWriteTimeout    = 10 * time.Second
	serverIdleTimeout     = 30 * time.Second
	serverShutdownTimeout = 5 * time.Second

	// Maximum body size for key submissions (256 bytes is more than enough for a base64 ed25519 key).
	maxBodySize = 256
)

// WebServerProvider implements an HTTP server that waits for an SSH key to be
// delivered via a POST request.
type WebServerProvider struct {
	addr   string
	logger *slog.Logger
}

// NewWebServerProvider creates a new WebServerProvider with the given address and logger.
// If addr is empty, the default address (0.0.0.0:8080) is used.
func NewWebServerProvider(addr string, logger *slog.Logger) *WebServerProvider {
	if addr == "" {
		addr = defaultServerAddr
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &WebServerProvider{
		addr:   addr,
		logger: logger,
	}
}

// WaitForKey starts an HTTP server and blocks until a valid SSH key is received
// via a POST request, or the context is cancelled.
func (w *WebServerProvider) WaitForKey(ctx context.Context) (string, error) {
	keyCh := make(chan string, 1)
	errCh := make(chan error, 1)

	mux := http.NewServeMux()
	mux.HandleFunc("/", func(rw http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(rw, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		body, err := io.ReadAll(io.LimitReader(r.Body, maxBodySize))
		if err != nil {
			w.logger.Error("failed to read request body", "error", err)
			http.Error(rw, "failed to read body", http.StatusInternalServerError)
			return
		}

		key := strings.TrimSpace(string(body))

		if !ed25519KeyPattern.MatchString(key) {
			w.logger.Warn("received invalid SSH key", "key_length", len(key))
			http.Error(rw, "invalid key: must be a base64-encoded ed25519 public key (68 characters)", http.StatusBadRequest)
			return
		}

		w.logger.Info("received valid SSH key")
		rw.WriteHeader(http.StatusOK)
		fmt.Fprint(rw, "key accepted")

		// Send the key; non-blocking since the channel is buffered.
		select {
		case keyCh <- key:
		default:
		}
	})

	listener, err := net.Listen("tcp", w.addr)
	if err != nil {
		return "", fmt.Errorf("listening on %s: %w", w.addr, err)
	}

	server := &http.Server{
		Handler:      mux,
		ReadTimeout:  serverReadTimeout,
		WriteTimeout: serverWriteTimeout,
		IdleTimeout:  serverIdleTimeout,
	}

	// Start serving in a background goroutine.
	go func() {
		w.logger.Info("starting SSH key delivery server", "addr", listener.Addr().String())
		if sErr := server.Serve(listener); sErr != nil && !errors.Is(sErr, http.ErrServerClosed) {
			errCh <- fmt.Errorf("http server error: %w", sErr)
		}
	}()

	// Wait for either a key, an error, or context cancellation.
	var key string
	select {
	case key = <-keyCh:
		w.logger.Info("shutting down key delivery server")
	case err := <-errCh:
		return "", err
	case <-ctx.Done():
		w.logger.Info("context cancelled, shutting down key delivery server")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), serverShutdownTimeout)
		defer cancel()
		if sErr := server.Shutdown(shutdownCtx); sErr != nil {
			w.logger.Error("error during server shutdown", "error", sErr)
		}
		return "", fmt.Errorf("waiting for SSH key: %w", ctx.Err())
	}

	// Gracefully shut down the server after receiving the key.
	shutdownCtx, cancel := context.WithTimeout(context.Background(), serverShutdownTimeout)
	defer cancel()
	if sErr := server.Shutdown(shutdownCtx); sErr != nil {
		w.logger.Error("error during server shutdown", "error", sErr)
	}

	return key, nil
}
